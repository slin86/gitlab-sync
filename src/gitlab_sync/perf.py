"""Timing of git commands and API requests, per command type and per repo.

Only the command category, the duration and the repo are stored. Arguments and URLs are never
recorded because they can contain credentials.
"""

from __future__ import annotations

import csv
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

NETWORK_PREFIXES = ("api ", "git fetch ", "git push ", "git ls-remote")
CSV_COLUMNS = [
    "run_start",
    "repo",
    "status",
    "total_s",
    "network_s",
    "fetch_new_s",
    "fetch_old_s",
    "push_s",
    "api_s",
    "git_other_s",
]


def format_duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}m"
    if seconds >= 60:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{seconds:.1f}s"


@dataclass
class Timing:
    count: int = 0
    total: float = 0.0
    longest: float = 0.0

    def add(self, seconds: float) -> None:
        self.count += 1
        self.total += seconds
        self.longest = max(self.longest, seconds)


@dataclass(frozen=True)
class SlowCall:
    seconds: float
    category: str
    repo: str | None


@dataclass
class RepoTiming:
    total: float = 0.0
    status: str = ""
    categories: dict[str, Timing] = field(default_factory=dict)


def is_network(category: str) -> bool:
    return category.startswith(NETWORK_PREFIXES)


def network_seconds(timings: dict[str, Timing]) -> float:
    return sum(t.total for category, t in timings.items() if is_network(category))


def grouped_seconds(timings: dict[str, Timing], new_remote: str, old_remote: str) -> dict[str, float]:
    """Seconds per CSV column group: fetch from each remote, push, API and everything else."""
    groups = dict.fromkeys(("fetch_new_s", "fetch_old_s", "push_s", "api_s", "git_other_s"), 0.0)
    for category, timing in timings.items():
        if category == f"git fetch {new_remote}":
            groups["fetch_new_s"] += timing.total
        elif category == f"git fetch {old_remote}":
            groups["fetch_old_s"] += timing.total
        elif category.startswith("git push "):
            groups["push_s"] += timing.total
        elif category.startswith("api "):
            groups["api_s"] += timing.total
        else:
            groups["git_other_s"] += timing.total
    return groups


class PerfTracker:
    """Collects timings. on_slow is called for every single call at or above slow_threshold."""

    def __init__(self, slow_threshold: float = 30.0, on_slow: Callable[[SlowCall], None] | None = None):
        self.slow_threshold = slow_threshold
        self.on_slow = on_slow
        self.categories: dict[str, Timing] = {}
        self.repos: dict[str, RepoTiming] = {}
        self.slow_calls: list[SlowCall] = []
        self.current_repo: str | None = None

    def record(self, category: str, seconds: float) -> None:
        self.categories.setdefault(category, Timing()).add(seconds)
        if self.current_repo is not None:
            repo = self.repos.setdefault(self.current_repo, RepoTiming())
            repo.categories.setdefault(category, Timing()).add(seconds)
        if self.slow_threshold > 0 and seconds >= self.slow_threshold:
            call = SlowCall(seconds, category, self.current_repo)
            self.slow_calls.append(call)
            if self.on_slow:
                self.on_slow(call)

    @contextmanager
    def measure(self, category: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(category, time.perf_counter() - start)

    @contextmanager
    def repo(self, name: str) -> Iterator[RepoTiming]:
        """Attribute all calls inside the block to repo name and record its wall time."""
        timing = self.repos.setdefault(name, RepoTiming())
        self.current_repo = name
        start = time.perf_counter()
        try:
            yield timing
        finally:
            timing.total = time.perf_counter() - start
            self.current_repo = None

    def append_csv(self, path: Path, started: datetime, new_remote: str, old_remote: str) -> None:
        """Append one row per repo. The header is written when the file is created."""
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists()
        with open(path, "a", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            if write_header:
                writer.writerow(CSV_COLUMNS)
            for name, repo in self.repos.items():
                groups = grouped_seconds(repo.categories, new_remote, old_remote)
                writer.writerow(
                    [
                        f"{started:%Y-%m-%d %H:%M:%S}",
                        name,
                        repo.status,
                        f"{repo.total:.2f}",
                        f"{network_seconds(repo.categories):.2f}",
                    ]
                    + [f"{groups[column]:.2f}" for column in CSV_COLUMNS[5:]]
                )
