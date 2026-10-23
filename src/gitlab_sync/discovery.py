"""Find git repositories below the configured root."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path


def find_repos(root: Path, include: list[str], exclude: list[str], only: list[str]) -> list[Path]:
    """Walk root and return every directory that contains .git, in sorted order.

    The walk does not descend into a repo or into hidden directories. Patterns are fnmatch globs on
    the path relative to root, where '*' also matches '/'.
    """
    repos: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if ".git" in dirnames or ".git" in filenames:
            dirnames[:] = []
            repos.append(Path(dirpath))
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
    return [repo for repo in repos if _selected(repo.relative_to(root).as_posix(), include, exclude, only)]


def _selected(rel: str, include: list[str], exclude: list[str], only: list[str]) -> bool:
    if _matches(rel, exclude):
        return False
    if include and not _matches(rel, include):
        return False
    return not only or _matches(rel, only)


def _matches(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in patterns)
