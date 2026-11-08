"""Context gathering: tree map, ripgrep search, and file slices.

All reads are confined to the workspace root (guard against path traversal and
symlinks that escape it).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .config import config
from .models import FileSlice


class ContextError(RuntimeError):
    pass


def _resolve_within_root(workspace_path: str, rel_path: str) -> Path:
    root = Path(workspace_path).resolve()
    candidate = (root / rel_path).resolve()
    # Reject symlinks that escape the workspace.
    if not _is_within(candidate, root):
        raise ContextError(f"Path escapes workspace: {rel_path}")
    return candidate


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _walk_limited(root: Path, max_depth: int = 2) -> List[Path]:
    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = Path(dirpath).relative_to(root).as_posix().count(os.sep)
        if depth >= max_depth:
            dirnames[:] = []  # don't recurse deeper
        # prune VCS / dependency dirs
        dirnames[:] = [
            d for d in dirnames if d not in {".git", "node_modules", ".venv", "venv"}
        ]
        for f in filenames:
            fp = Path(dirpath) / f
            if fp.is_symlink() and not _is_within(fp.resolve(), root):
                continue
            files.append(fp)
    return sorted(files)


def repo_map(workspace_path: str, max_depth: int = 2) -> str:
    """Return a tree map (depth-limited) of the workspace."""
    root = Path(workspace_path).resolve()
    if not root.exists():
        raise ContextError(f"Workspace does not exist: {workspace_path}")
    lines: List[str] = [root.name + "/"]
    for fp in _walk_limited(root, max_depth=max_depth):
        rel = fp.relative_to(root)
        depth = rel.as_posix().count(os.sep)
        if depth > max_depth:
            continue
        indent = "  " * (depth + 1)
        marker = "/" if fp.is_dir() else ""
        lines.append(f"{indent}{rel.name}{marker}")
    return "\n".join(lines)


def ripgrep_search(workspace_path: str, query: str, max_bytes: int) -> tuple:
    """Run ripgrep; fall back to a Python substring search. Returns (matches, truncated)."""
    root = Path(workspace_path).resolve()
    matches: List[str] = []
    tool = shutil.which("rg")

    if tool:
        try:
            proc = subprocess.run(
                [tool, "--no-heading", "--line-number", "--column", query, str(root)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            raw = proc.stdout or ""
            for line in raw.splitlines():
                matches.append(line)
        except Exception:
            tool = None  # fall through to python fallback

    if not tool:
        # Python fallback: case-insensitive substring search over text files.
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".venv", "venv"}]
            for fname in filenames:
                fp = Path(dirpath) / fname
                try:
                    text = fp.read_text(encoding="utf-8", errors="replace")
                except (OSError, UnicodeDecodeError):
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if query.lower() in line.lower():
                        matches.append(f"{fp}:{i}:1:{line}")
                        if len(matches) > 200:
                            break
            if len(matches) > 200:
                break

    # Truncate by bytes.
    out = "\n".join(matches)
    truncated = len(out.encode("utf-8")) > max_bytes
    if truncated:
        out = out.encode("utf-8")[:max_bytes].decode("utf-8", errors="replace") + "\n...[truncated]..."
    return matches, truncated


def file_slices(workspace_path: str, paths: list, max_bytes: int) -> tuple:
    """Read file slices with line numbers. Returns (slices, truncated)."""
    root = Path(workspace_path).resolve()
    slices: List[FileSlice] = []
    total_bytes = 0
    truncated = False

    for rel in paths:
        try:
            fp = _resolve_within_root(workspace_path, rel)
        except ContextError:
            continue
        if not fp.is_file():
            continue
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        content = "\n".join(f"{i+1:4d}\t{line}" for i, line in enumerate(lines))
        b = content.encode("utf-8")
        if total_bytes + len(b) > max_bytes:
            truncated = True
            break
        total_bytes += len(b)
        slices.append(
            FileSlice(path=rel, start_line=1, end_line=len(lines), content=content)
        )

    return slices, truncated
