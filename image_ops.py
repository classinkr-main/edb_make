#!/usr/bin/env python3
"""Vectorized pixel-channel helpers shared by the image pipelines.

NumPy reduces along the trailing 3-length channel axis one element at a time,
so ``rgb.max(axis=2)`` walks the array with a stride of 3 and costs about ten
times a pairwise ``np.maximum`` chain on the same pixels. Every helper here
keeps the arithmetic order of the expression it replaces, so the results stay
bit-identical to the naive form while the work drops to contiguous passes.
"""

from __future__ import annotations

from typing import Any

try:  # pragma: no cover - exercised through the callers
    import numpy as np
except ImportError:  # pragma: no cover - numpy is optional at runtime
    np = None  # type: ignore[assignment]


try:  # pragma: no cover - exercised through the callers
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - OpenCV is optional at runtime
    cv2 = None  # type: ignore[assignment]


def _require_numpy() -> Any:
    if np is None:  # pragma: no cover - guarded by callers
        raise RuntimeError("numpy is required for image_ops helpers")
    return np


def channel_extrema(rgb: Any) -> tuple[Any, Any]:
    """Return ``(max, min)`` over the channel axis of an ``H x W x 3`` array."""
    _require_numpy()
    red = rgb[..., 0]
    green = rgb[..., 1]
    blue = rgb[..., 2]
    highest = np.maximum(np.maximum(red, green), blue)
    lowest = np.minimum(np.minimum(red, green), blue)
    return highest, lowest


def channel_saturation(rgb: Any) -> Any:
    """Per-pixel ``max(channel) - min(channel)``; matches ``rgb.max(axis=2) - rgb.min(axis=2)``."""
    highest, lowest = channel_extrema(rgb)
    return highest - lowest


def channel_luminance(rgb: Any) -> Any:
    """Rec.601 luminance; matches ``0.299 * r + 0.587 * g + 0.114 * b`` exactly."""
    _require_numpy()
    return (0.299 * rgb[..., 0]) + (0.587 * rgb[..., 1]) + (0.114 * rgb[..., 2])


def channel_magnitude(rgb: Any) -> Any:
    """Euclidean channel magnitude; matches ``np.linalg.norm(rgb, axis=2)``."""
    numpy_module = _require_numpy()
    red = rgb[..., 0]
    green = rgb[..., 1]
    blue = rgb[..., 2]
    return numpy_module.sqrt(red * red + green * green + blue * blue)


def channel_distance(rgb: Any, color: Any) -> Any:
    """Euclidean channel distance; matches ``np.sqrt(np.sum((rgb - color) ** 2, axis=2))``."""
    numpy_module = _require_numpy()
    reference = numpy_module.asarray(color, dtype=rgb.dtype)
    red = rgb[..., 0] - reference[0]
    green = rgb[..., 1] - reference[1]
    blue = rgb[..., 2] - reference[2]
    return numpy_module.sqrt(red * red + green * green + blue * blue)


def _python_seeded_components(mask: Any, seeds: Any) -> list[tuple[int, int, int, int, int]]:
    """Flood-fill fallback used when OpenCV is unavailable.

    Indexes with ``mask[y][x]`` so the same code serves NumPy arrays and plain
    nested lists.
    """
    height = len(mask)
    width = len(mask[0]) if height else 0
    visited = [bytearray(width) for _ in range(height)]
    components: list[tuple[int, int, int, int, int]] = []
    for seed_y in range(height):
        seed_row = seeds[seed_y]
        mask_row = mask[seed_y]
        visited_row = visited[seed_y]
        for seed_x in range(width):
            if not seed_row[seed_x] or not mask_row[seed_x] or visited_row[seed_x]:
                continue
            stack = [(seed_x, seed_y)]
            visited_row[seed_x] = 1
            min_x = max_x = seed_x
            min_y = max_y = seed_y
            count = 0
            while stack:
                current_x, current_y = stack.pop()
                count += 1
                if current_x < min_x:
                    min_x = current_x
                elif current_x > max_x:
                    max_x = current_x
                if current_y < min_y:
                    min_y = current_y
                elif current_y > max_y:
                    max_y = current_y
                for next_x, next_y in (
                    (current_x - 1, current_y),
                    (current_x + 1, current_y),
                    (current_x, current_y - 1),
                    (current_x, current_y + 1),
                ):
                    if next_x < 0 or next_x >= width or next_y < 0 or next_y >= height:
                        continue
                    if visited[next_y][next_x] or not mask[next_y][next_x]:
                        continue
                    visited[next_y][next_x] = 1
                    stack.append((next_x, next_y))
            components.append((min_x, min_y, max_x, max_y, count))
    return components


def seeded_components(mask: Any, seeds: Any) -> list[tuple[int, int, int, int, int]]:
    """4-connected components of ``mask`` that contain at least one ``seeds`` pixel.

    Returns ``(min_x, min_y, max_x, max_y, pixel_count)`` per component in the
    mask's own coordinate space, in unspecified order. OpenCV labels the whole
    region in one C pass; the Python flood fill is only a fallback for
    installations without OpenCV.
    """
    if np is None or cv2 is None or not hasattr(mask, "shape"):
        return _python_seeded_components(mask, seeds)

    binary = np.ascontiguousarray(mask, dtype=np.uint8)
    if binary.size == 0:
        return []
    _, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=4)
    seeded_labels = np.unique(labels[np.asarray(seeds, dtype=bool) & mask])
    components: list[tuple[int, int, int, int, int]] = []
    for label in seeded_labels.tolist():
        if label == 0:
            continue
        left, top, component_width, component_height, area = stats[label]
        components.append(
            (
                int(left),
                int(top),
                int(left) + int(component_width) - 1,
                int(top) + int(component_height) - 1,
                int(area),
            )
        )
    return components
