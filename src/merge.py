"""
Merge logic: compare working copy against original and apply selected changes.
"""

import difflib
import fnmatch
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Change:
    relative: str
    change_type: str  # 'new', 'modified', 'deleted'
    working_path: Path | None
    original_path: Path | None


class GitignoreMatcher:
    """Minimal gitignore parser using fnmatch (covers common patterns)."""

    def __init__(self, root: Path):
        self.root = root
        self.rules: list[tuple[str, bool, bool, bool]] = []
        gitignore = root / ".gitignore"
        if gitignore.exists():
            for line in gitignore.read_text().splitlines():
                line = line.rstrip()
                if not line or line.startswith("#"):
                    continue
                negation = line.startswith("!")
                if negation:
                    line = line[1:]
                dir_only = line.endswith("/")
                pat = line.rstrip("/")
                anchored = pat.startswith("/")
                if anchored:
                    pat = pat[1:]
                self.rules.append((pat, dir_only, anchored, negation))

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        ignored = False
        for pat, dir_only, anchored, negation in self.rules:
            if dir_only and not is_dir:
                continue
            matches = False
            if anchored:
                if fnmatch.fnmatch(rel_path, pat):
                    matches = True
                if is_dir and fnmatch.fnmatch(rel_path + "/", pat):
                    matches = True
            else:
                parts = rel_path.split("/")
                for i in range(len(parts)):
                    sub = "/".join(parts[i:])
                    if fnmatch.fnmatch(sub, pat):
                        matches = True
                        break
                    if fnmatch.fnmatch(parts[i], pat):
                        matches = True
                        break
            if matches:
                ignored = not negation
        return ignored


def _should_skip(rel: Path) -> bool:
    return any(part == "__pycache__" for part in rel.parts)


def _collect_rel_paths(root: Path) -> set[str]:
    result: set[str] = set()
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if _should_skip(rel):
            continue
        result.add(str(rel))
    return result


def detect_changes(working_dir: Path, original_dir: Path) -> list[Change]:
    working_paths = _collect_rel_paths(working_dir)
    original_paths = _collect_rel_paths(original_dir)

    changes: list[Change] = []
    all_paths = sorted(set(working_paths) | set(original_paths), key=lambda s: s.lower())

    for rel_str in all_paths:
        rel = Path(rel_str)
        working = working_dir / rel
        original = original_dir / rel
        in_working = rel_str in working_paths
        in_original = rel_str in original_paths

        if in_working and not in_original:
            changes.append(Change(rel_str, "new", working, None))
        elif not in_working and in_original:
            changes.append(Change(rel_str, "deleted", None, original))
        elif in_working and in_original:
            if working.is_file() and original.is_file():
                if working.read_bytes() != original.read_bytes():
                    changes.append(Change(rel_str, "modified", working, original))
            elif working.is_dir() != original.is_dir():
                changes.append(Change(rel_str, "modified", working, original))

    return changes


def _depth(rel: str) -> int:
    return len(Path(rel).parts)


def apply_changes(changes: list[Change], selected_rel_paths: set[str]):
    selected = [c for c in changes if c.relative in selected_rel_paths]

    non_del = [c for c in selected if c.change_type != "deleted"]
    non_del.sort(key=lambda c: _depth(c.relative))
    del_c = [c for c in selected if c.change_type == "deleted"]
    del_c.sort(key=lambda c: _depth(c.relative), reverse=True)
    ordered = non_del + del_c

    deleted_dirs: set[str] = set()

    for change in ordered:
        if change.change_type in ("new", "modified"):
            src = change.working_path
            dst = change.original_path
            if src is None or dst is None:
                continue
            if src.is_dir():
                if dst.exists() and not dst.is_dir():
                    dst.unlink()
                if not dst.exists():
                    dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists() and dst.is_dir():
                    shutil.rmtree(dst)
                shutil.copy2(str(src), str(dst))
        elif change.change_type == "deleted":
            orig = change.original_path
            if orig is None or not orig.exists():
                continue
            rel_str = change.relative
            if any(rel_str.startswith(d + "/") or rel_str == d for d in deleted_dirs):
                continue
            if orig.is_dir():
                shutil.rmtree(orig)
                deleted_dirs.add(rel_str)
            else:
                orig.unlink()


def make_diff(change: Change) -> str:
    """Return a unified diff string for a modified change."""
    working = change.working_path
    original = change.original_path
    if working is None or original is None or not working.is_file() or not original.is_file():
        return "Unable to compute diff."
    try:
        old = original.read_text()
        new = working.read_text()
    except UnicodeDecodeError:
        return "Binary file — unable to show diff."
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{change.relative}",
            tofile=f"b/{change.relative}",
        )
    )
