#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path


REQUIRED_UI_FILES = (
    "ui_prototype/index.html",
    "ui_prototype/board.html",
    "ui_prototype/board.css",
    "ui_prototype/favicon.png",
    "ui_prototype/reorder.js",
    "ui_prototype/review_filters.js",
    "ui_prototype/publish_summary.js",
    "ui_prototype/publish_guard.js",
    "ui_prototype/app.bundle.js",
    "ui_prototype/vendor/react.production.min.js",
    "ui_prototype/vendor/react-dom.production.min.js",
    "ui_prototype/vendor/fonts/fonts.css",
    "ui_prototype/vendor/fonts/PretendardVariable.woff2",
    "ui_prototype/vendor/fonts/JetBrainsMonoVariable.woff2",
    "ui_prototype/vendor/fonts/CaveatVariable.woff2",
    "ui_prototype/vendor/fonts/OFL-Pretendard.txt",
    "ui_prototype/vendor/fonts/OFL-JetBrainsMono.txt",
    "ui_prototype/vendor/fonts/OFL-Caveat.txt",
)

REQUIRED_RUNTIME_SOURCE_FILES = (
    "scripts/render_hwp_with_rhwp_core.mjs",
    "assets/app_icon.png",
)

REMOTE_ASSET_HOSTS = (
    "https://fonts.googleapis.com",
    "https://fonts.gstatic.com",
    "https://cdn.jsdelivr.net",
    "https://unpkg.com",
    "https://cdnjs.cloudflare.com",
)

FORBIDDEN_FRONTEND_FILES = (
    "ui_prototype/app.js",
    "ui_prototype/prototype_data.js",
    "ui_prototype/generated_session.js",
    "ui_prototype/vendor/babel.min.js",
    "ui_prototype/vendor/babel.min.js.map",
    "build_ui_prototype_data.py",
    # A second, unreferenced design system (warm paper palette, teal accent)
    # that conflicted with the shipped theme. Deleted on purpose.
    "ui_prototype/styles.css",
)

PACKAGING_MANIFESTS = (
    "ClassInEDBMVP.spec",
    "package_macos_app.sh",
    "package_mvp.ps1",
)

FRONTEND_BUNDLE_SOURCE_FILES = (
    "ui_prototype/art.jsx",
    "ui_prototype/tweaks-panel.jsx",
    "ui_prototype/app.jsx",
    "scripts/build_frontend_bundle.mjs",
    "scripts/vendor/babel.min.js",
)
SOURCE_DIGEST_RE = re.compile(r"Source SHA256:\s*([0-9a-f]{64})")
BUNDLE_CACHE_BUST_RE = re.compile(r"^app\.bundle\.js\?v=frontend-bundle-([0-9a-f]{64})$")
STYLE_CACHE_BUST_RE = re.compile(r"^board\.css\?v=board-css-([0-9a-f]{64})$")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _contains_path(text: str, path: str) -> bool:
    return path in text or path.replace("/", "\\") in text


def frontend_bundle_source_digest(project_root: Path) -> str | None:
    root = project_root.resolve()
    digest = hashlib.sha256()
    for rel_path in FRONTEND_BUNDLE_SOURCE_FILES:
        source_path = root / rel_path
        if not source_path.is_file():
            return None
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def style_source_digest(project_root: Path) -> str | None:
    style_path = project_root.resolve() / "ui_prototype" / "board.css"
    if not style_path.is_file():
        return None
    return hashlib.sha256(style_path.read_bytes()).hexdigest()


def style_cache_bust_digest(link_href: str) -> str | None:
    match = STYLE_CACHE_BUST_RE.fullmatch(link_href)
    if match is None:
        return None
    return match.group(1)


def bundle_cache_bust_digest(script_src: str) -> str | None:
    match = BUNDLE_CACHE_BUST_RE.fullmatch(script_src)
    if match is None:
        return None
    return match.group(1)


def collect_deterministic_bundle_errors(project_root: Path) -> list[str]:
    """Rebuild in memory through Node and reject any tracked bundle drift."""

    root = project_root.resolve()
    node = shutil.which("node")
    if node is None:
        return ["Node.js is required to deterministically verify app.bundle.js"]
    builder = root / "scripts" / "build_frontend_bundle.mjs"
    if not builder.is_file():
        return ["missing deterministic frontend builder: scripts/build_frontend_bundle.mjs"]
    try:
        completed = subprocess.run(
            [node, str(builder), "--check"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"could not run deterministic frontend bundle check: {exc}"]
    if completed.returncode == 0:
        return []
    detail = (completed.stderr or completed.stdout or "unknown Node.js failure").strip()
    return [f"deterministic frontend bundle check failed: {detail[-2_000:]}"]


def collect_errors(project_root: Path) -> list[str]:
    root = project_root.resolve()
    errors: list[str] = []
    expected_digest = frontend_bundle_source_digest(root)

    for rel_path in REQUIRED_UI_FILES:
        if not (root / rel_path).is_file():
            errors.append(f"missing required frontend asset: {rel_path}")
    for rel_path in REQUIRED_RUNTIME_SOURCE_FILES:
        if not (root / rel_path).is_file():
            errors.append(f"missing required runtime asset: {rel_path}")

    for rel_path in FORBIDDEN_FRONTEND_FILES:
        if (root / rel_path).exists():
            errors.append(f"legacy frontend artifact should not exist: {rel_path}")

    board_path = root / "ui_prototype" / "board.html"
    if board_path.is_file():
        board_html = _read(board_path)
        script_srcs = re.findall(r"<script\s+src=\"([^\"]+)\"", board_html)
        bundle_srcs = [src for src in script_srcs if src.startswith("app.bundle.js?v=frontend-bundle-")]
        if len(bundle_srcs) != 1:
            errors.append("board.html must load exactly one cache-busted app.bundle.js")
        elif (board_digest := bundle_cache_bust_digest(bundle_srcs[0])) is None:
            errors.append("board.html app.bundle.js cache bust must use the 64-character source digest")
        elif expected_digest is not None and board_digest != expected_digest:
            errors.append("board.html app.bundle.js cache bust is stale; rebuild with scripts/build_frontend_bundle.mjs")
        forbidden_tokens = (
            "app.js?v=",
            "prototype_data.js",
            "generated_session.js",
            "vendor/babel.min.js",
            'type="text/babel"',
        )
        for token in forbidden_tokens:
            if token in board_html:
                errors.append(f"board.html still references legacy runtime token: {token}")
        style_hrefs = [
            href
            for href in re.findall(r"<link[^>]*href=\"([^\"]+)\"", board_html)
            if href.startswith("board.css")
        ]
        expected_style_digest = style_source_digest(root)
        if len(style_hrefs) != 1:
            errors.append("board.html must load exactly one cache-busted board.css")
        elif (style_digest := style_cache_bust_digest(style_hrefs[0])) is None:
            errors.append("board.html board.css cache bust must use the 64-character source digest")
        elif expected_style_digest is not None and style_digest != expected_style_digest:
            errors.append("board.html board.css cache bust is stale; rebuild with scripts/build_frontend_bundle.mjs")
        if "<style>" in board_html:
            errors.append("board.html must keep its theme in board.css, not an inline <style> block")

        for remote_host in REMOTE_ASSET_HOSTS:
            if remote_host in board_html:
                errors.append(
                    "board.html must not load remote assets; the packaged app runs offline: "
                    f"{remote_host}"
                )
    else:
        errors.append("missing ui_prototype/board.html")

    bundle_path = root / "ui_prototype" / "app.bundle.js"
    if bundle_path.is_file():
        bundle = _read(bundle_path)
        if "Generated by scripts/build_frontend_bundle.mjs" not in bundle:
            errors.append("app.bundle.js is missing the generated bundle banner")
        if "/* app.jsx */" not in bundle:
            errors.append("app.bundle.js does not include the app.jsx section")
        digest_match = SOURCE_DIGEST_RE.search(bundle)
        if expected_digest is None:
            errors.append("could not calculate app.bundle.js source digest because a bundle input is missing")
        elif digest_match is None:
            errors.append("app.bundle.js is missing the source digest; rebuild with scripts/build_frontend_bundle.mjs")
        elif digest_match.group(1) != expected_digest:
            errors.append("app.bundle.js source digest is stale; rebuild with scripts/build_frontend_bundle.mjs")

    for manifest_name in PACKAGING_MANIFESTS:
        manifest_path = root / manifest_name
        if not manifest_path.is_file():
            errors.append(f"missing packaging manifest: {manifest_name}")
            continue
        manifest = _read(manifest_path)
        for rel_path in REQUIRED_UI_FILES:
            if not _contains_path(manifest, rel_path):
                errors.append(f"{manifest_name} does not include {rel_path}")
        for rel_path in REQUIRED_RUNTIME_SOURCE_FILES:
            if not _contains_path(manifest, rel_path):
                errors.append(f"{manifest_name} does not include runtime asset {rel_path}")
        for rel_path in FORBIDDEN_FRONTEND_FILES:
            if _contains_path(manifest, rel_path):
                errors.append(f"{manifest_name} still references legacy artifact {rel_path}")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify frontend assets before packaging.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)

    errors = collect_errors(args.root)
    errors.extend(collect_deterministic_bundle_errors(args.root))
    if errors:
        for error in errors:
            print(f"[frontend-package] ERROR: {error}", file=sys.stderr)
        return 1
    print("[frontend-package] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
