from __future__ import annotations

import unittest

import numpy as np

import image_ops


def _sample_rgb(dtype) -> np.ndarray:
    generator = np.random.default_rng(20260915)
    return generator.integers(0, 256, size=(37, 53, 3)).astype(dtype)


class TestChannelHelpers(unittest.TestCase):
    def test_saturation_matches_axis_reduction_exactly(self) -> None:
        for dtype in (np.float32, np.float64, np.uint8, np.int16):
            with self.subTest(dtype=dtype):
                rgb = _sample_rgb(dtype)
                expected = rgb.max(axis=2) - rgb.min(axis=2)
                actual = image_ops.channel_saturation(rgb)
                np.testing.assert_array_equal(actual, expected)
                self.assertEqual(actual.dtype, expected.dtype)

    def test_channel_extrema_matches_axis_reduction_exactly(self) -> None:
        rgb = _sample_rgb(np.float32)
        highest, lowest = image_ops.channel_extrema(rgb)
        np.testing.assert_array_equal(highest, rgb.max(axis=2))
        np.testing.assert_array_equal(lowest, rgb.min(axis=2))

    def test_luminance_matches_inline_expression_exactly(self) -> None:
        rgb = _sample_rgb(np.float32)
        expected = (0.299 * rgb[..., 0]) + (0.587 * rgb[..., 1]) + (0.114 * rgb[..., 2])
        np.testing.assert_array_equal(image_ops.channel_luminance(rgb), expected)

    def test_distance_matches_sum_of_squares_exactly(self) -> None:
        rgb = _sample_rgb(np.float32)
        for color in ((0, 0, 0), (255, 255, 255), (31, 64, 197)):
            with self.subTest(color=color):
                background = np.asarray(color, dtype=np.float32)
                expected = np.sqrt(np.sum((rgb - background) ** 2, axis=2))
                actual = image_ops.channel_distance(rgb, color)
                np.testing.assert_array_equal(actual, expected)
                self.assertEqual(actual.dtype, expected.dtype)

    def test_magnitude_matches_linalg_norm_exactly(self) -> None:
        for dtype in (np.float32, np.float64):
            with self.subTest(dtype=dtype):
                rgb = _sample_rgb(dtype) / dtype(255.0)
                expected = np.linalg.norm(rgb, axis=2)
                actual = image_ops.channel_magnitude(rgb)
                np.testing.assert_array_equal(actual, expected)
                self.assertEqual(actual.dtype, expected.dtype)

    def test_helpers_accept_non_contiguous_region_views(self) -> None:
        rgb = _sample_rgb(np.float32)
        strip = rgb[:, 7:19, :]
        np.testing.assert_array_equal(
            image_ops.channel_saturation(strip),
            strip.max(axis=2) - strip.min(axis=2),
        )
        np.testing.assert_array_equal(
            image_ops.channel_distance(strip, (255, 255, 255)),
            np.sqrt(np.sum((strip - np.float32(255)) ** 2, axis=2)),
        )


class TestSeededComponents(unittest.TestCase):
    def _normalized(self, components):
        return sorted(tuple(int(value) for value in component) for component in components)

    def test_opencv_path_matches_python_flood_fill_on_random_masks(self) -> None:
        if image_ops.cv2 is None:  # pragma: no cover - OpenCV is a release dependency
            self.skipTest("OpenCV is not installed")
        generator = np.random.default_rng(4242)
        for trial in range(12):
            with self.subTest(trial=trial):
                mask = generator.random((48, 31)) < 0.34
                seeds = np.zeros_like(mask)
                seeds[:, :4] = True
                seeds[-3:, :] = True
                self.assertEqual(
                    self._normalized(image_ops.seeded_components(mask, seeds)),
                    self._normalized(image_ops._python_seeded_components(mask, seeds)),
                )

    def test_components_are_four_connected_and_seed_limited(self) -> None:
        mask = np.zeros((6, 6), dtype=bool)
        mask[1:4, 0:2] = True  # touches the seed band
        mask[0, 4] = True  # diagonal-only neighbour of the isolated blob below
        mask[1, 5] = True
        mask[5, 5] = True  # far from any seed column
        seeds = np.zeros_like(mask)
        seeds[:, 0] = True

        components = image_ops.seeded_components(mask, seeds)
        self.assertEqual(self._normalized(components), [(0, 1, 1, 3, 6)])
        self.assertEqual(
            self._normalized(components),
            self._normalized(image_ops._python_seeded_components(mask, seeds)),
        )

    def test_nested_list_masks_use_the_python_fallback(self) -> None:
        mask = [[True, True, False], [False, True, False], [False, False, True]]
        seeds = [[True, False, False], [False, False, False], [False, False, False]]
        self.assertEqual(
            self._normalized(image_ops.seeded_components(mask, seeds)),
            [(0, 0, 1, 1, 3)],
        )

    def test_empty_mask_returns_no_components(self) -> None:
        mask = np.zeros((5, 5), dtype=bool)
        self.assertEqual(image_ops.seeded_components(mask, np.ones_like(mask)), [])


if __name__ == "__main__":
    unittest.main()
