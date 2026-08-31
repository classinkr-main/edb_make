#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_PATTERNS = (
    "build",
    "dist*",
    "tmp_validation_*",
)
EDB_EXPORT_PATTERNS = (
    "generated_edb_pair*",
)
LEGACY_UI_FILE_PATHS = (
    Path("ui_prototype") / "generated_session.js",
    Path("ui_prototype") / "prototype_data.js",
)
PROTECTED_ROOT_NAMES = frozenset({".app_runtime", ".git", ".venv"})


@dataclass(frozen=True)
class CleanupCandidate:
    path: Path
    category: str


class CleanupSafetyError(ValueError):
    """Raised when a cleanup target cannot be proven safe to remove."""


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _safe_root_child(root: Path, path: Path) -> bool:
    try:
        resolved_root = root.resolve()
        resolved_path = path.resolve(strict=False)
    except OSError:
        return False
    return resolved_path != resolved_root and resolved_path.parent == resolved_root


def _is_legacy_ui_file(root: Path, path: Path) -> bool:
    try:
        relative_path = path.resolve(strict=False).relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return relative_path in LEGACY_UI_FILE_PATHS


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.fspath(left.resolve(strict=False))) == os.path.normcase(
        os.fspath(right.resolve(strict=False))
    )


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", os.fspath(root), *args],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise CleanupSafetyError(f"Git is required for cleanup safety checks: {exc}") from exc


def validate_repository_root(root: Path) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise CleanupSafetyError(f"cleanup root is not accessible: {root}: {exc}") from exc
    if not resolved_root.is_dir():
        raise CleanupSafetyError(f"cleanup root is not a directory: {resolved_root}")

    result = _run_git(resolved_root, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CleanupSafetyError(f"cleanup root is not a Git worktree: {resolved_root}: {detail}")
    git_root = Path(result.stdout.decode("utf-8", errors="replace").strip())
    if not _same_path(resolved_root, git_root):
        raise CleanupSafetyError(
            f"cleanup root must be the Git worktree root ({git_root}), got: {resolved_root}"
        )
    return resolved_root


def _allowed_candidate(root: Path, candidate: CleanupCandidate) -> bool:
    path = candidate.path
    if candidate.category == "legacy-ui":
        return (path.is_file() or path.is_symlink()) and _is_legacy_ui_file(root, path)
    if not _safe_root_child(root, path) or path.name in PROTECTED_ROOT_NAMES:
        return False
    if candidate.category == "packaging":
        return _matches(path.name, DEFAULT_PATTERNS)
    if candidate.category == "edb-export":
        return _matches(path.name, EDB_EXPORT_PATTERNS)
    return False


def _git_relative_path(root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve()).as_posix()
    except (OSError, ValueError) as exc:
        raise CleanupSafetyError(f"cleanup target escapes repository root: {path}") from exc


def validate_cleanup_candidate(root: Path, candidate: CleanupCandidate) -> None:
    root = validate_repository_root(root)
    path = candidate.path
    if not _allowed_candidate(root, candidate):
        raise CleanupSafetyError(
            f"cleanup target is not in the {candidate.category!r} allowlist: {path}"
        )

    relative_path = _git_relative_path(root, path)
    tracked = _run_git(root, "ls-files", "-z", "--", relative_path)
    if tracked.returncode != 0:
        detail = tracked.stderr.decode("utf-8", errors="replace").strip()
        raise CleanupSafetyError(f"could not inspect tracked files for {relative_path}: {detail}")
    if tracked.stdout:
        raise CleanupSafetyError(f"cleanup target contains Git-tracked content: {relative_path}")

    ignored = _run_git(root, "check-ignore", "-q", "--", relative_path)
    if ignored.returncode == 1:
        raise CleanupSafetyError(
            f"cleanup target is not ignored by Git and may be untracked source: {relative_path}"
        )
    if ignored.returncode != 0:
        detail = ignored.stderr.decode("utf-8", errors="replace").strip()
        raise CleanupSafetyError(f"could not verify ignored target {relative_path}: {detail}")


def _covered_by_existing_candidate(path: Path, candidates: list[CleanupCandidate]) -> bool:
    try:
        resolved_path = path.resolve(strict=False)
    except OSError:
        return False
    for candidate in candidates:
        try:
            resolved_candidate = candidate.path.resolve(strict=False)
        except OSError:
            continue
        if resolved_path == resolved_candidate or resolved_candidate in resolved_path.parents:
            return True
    return False


def collect_cleanup_candidates(
    root: Path,
    *,
    include_edb_exports: bool = False,
    include_runtime: bool = False,
) -> list[CleanupCandidate]:
    if include_runtime:
        raise CleanupSafetyError(
            ".app_runtime is protected user state and cannot be removed by this cleanup tool"
        )
    root = root.resolve()
    candidates: list[CleanupCandidate] = []
    categories: list[tuple[str, tuple[str, ...]]] = [("packaging", DEFAULT_PATTERNS)]
    if include_edb_exports:
        categories.append(("edb-export", EDB_EXPORT_PATTERNS))
    for child in root.iterdir():
        if not _safe_root_child(root, child):
            continue
        for category, patterns in categories:
            if _matches(child.name, patterns):
                candidates.append(CleanupCandidate(path=child, category=category))
                break

    for relative_path in LEGACY_UI_FILE_PATHS:
        path = root / relative_path
        if (
            _is_legacy_ui_file(root, path)
            and not _covered_by_existing_candidate(path, candidates)
            and (path.is_file() or path.is_symlink())
        ):
            candidates.append(CleanupCandidate(path=path, category="legacy-ui"))

    return sorted(candidates, key=lambda candidate: (candidate.category, candidate.path.name))


def path_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        try:
            return path.lstat().st_size
        except OSError:
            return 0

    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [name for name in dirnames if not (Path(dirpath) / name).is_symlink()]
        for name in filenames:
            candidate = Path(dirpath) / name
            try:
                total += candidate.lstat().st_size
            except OSError:
                continue
    return total


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def remove_candidate(root: Path, candidate: CleanupCandidate) -> None:
    validate_cleanup_candidate(root, candidate)
    path = candidate.path
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Remove ignored local packaging artifacts that can be mistaken for the "
            "current app build."
        )
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--yes", action="store_true", help="Actually remove files. Without this, only prints a dry run.")
    parser.add_argument(
        "--include-edb-exports",
        action="store_true",
        help="Also remove generated_edb_pair* export folders.",
    )
    parser.add_argument(
        "--include-runtime",
        action="store_true",
        help="Deprecated safety guard; .app_runtime user state is never removed.",
    )
    args = parser.parse_args(argv)

    try:
        root = validate_repository_root(args.root)
    except CleanupSafetyError as exc:
        print(f"[cleanup] ERROR: {exc}", file=sys.stderr)
        return 2
    try:
        candidates = collect_cleanup_candidates(
            root,
            include_edb_exports=args.include_edb_exports,
            include_runtime=args.include_runtime,
        )
    except CleanupSafetyError as exc:
        print(f"[cleanup] ERROR: {exc}", file=sys.stderr)
        return 2
    if not candidates:
        print("[cleanup] no stale local artifacts found")
        return 0

    try:
        for candidate in candidates:
            validate_cleanup_candidate(root, candidate)
    except CleanupSafetyError as exc:
        print(f"[cleanup] ERROR: preflight refused cleanup: {exc}", file=sys.stderr)
        return 2

    mode = "APPLY" if args.yes else "DRY RUN"
    print(f"[cleanup] mode={mode}; root={root}")
    action = "removing" if args.yes else "would remove"
    total_size = 0
    for candidate in candidates:
        size = path_size(candidate.path)
        total_size += size
        rel_path = candidate.path.relative_to(root)
        print(f"[cleanup] {action} {candidate.category}: {rel_path} ({format_size(size)})")
        if args.yes:
            try:
                remove_candidate(root, candidate)
            except (OSError, CleanupSafetyError) as exc:
                print(f"[cleanup] ERROR: failed to remove {rel_path}: {exc}", file=sys.stderr)
                return 1

    if args.yes:
        print(f"[cleanup] removed {len(candidates)} artifact(s), {format_size(total_size)}")
    else:
        print(f"[cleanup] dry run only; rerun with --yes to remove {len(candidates)} artifact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
