"""End of run summary and performance report in the log."""

from __future__ import annotations

from pathlib import Path

from gitlab_sync.config import Config
from gitlab_sync.log import TreeLog
from gitlab_sync.perf import PerfTracker, format_duration, grouped_seconds, network_seconds
from gitlab_sync.results import RunResult

MAX_SLOW_CALLS_SHOWN = 20


def log_summary(log: TreeLog, result: RunResult, files: dict[str, Path | None]) -> None:
    log.info(0, "Summary")
    log.info(
        0,
        f"Repos: {result.repos} ({result.repos_ok} without problems, "
        f"{result.repos_skipped} skipped by API precheck)",
    )
    log.info(
        0,
        f"Branches: {result.branches_new} new, {result.branches_updated} updated, "
        f"{result.branches_current} up to date; commits synced: {result.commits}; "
        f"tags: {result.tags_pushed} new, {result.tags_overwritten} overwritten",
    )
    labels = {
        "conflicts": f"Branch conflicts: {len(result.branch_conflicts)} written to",
        "protected": f"Protected branches: {len(result.protected_pushes)} rejected pushes, links in",
        "rollbacks": f"Jenkins rollback: instructions for {len(result.rollbacks)} branches in",
    }
    for key, label in labels.items():
        if files.get(key):
            log.info(0, f"{label} {files[key]}")
    for title, items in (
        ("Conflicts", result.conflicts),
        ("Errors", result.errors),
        ("Warnings", result.warnings),
    ):
        if items:
            log.info(0, f"{title} ({len(items)}):")
            for item in items:
                log.info(1, item)


def log_perf_report(log: TreeLog, perf: PerfTracker, cfg: Config, total: float) -> None:
    measured = sum(t.total for t in perf.categories.values())
    network = network_seconds(perf.categories)
    share = f" ({network / measured:.0%})" if measured else ""
    log.info(0, "Performance")
    log.info(
        0,
        f"Total {format_duration(total)}; git and API calls {format_duration(measured)}, "
        f"network (fetch, push, API) {format_duration(network)}{share}",
    )
    if perf.categories:
        log.info(0, "Time per command type, sorted by total:")
        for category, t in sorted(perf.categories.items(), key=lambda kv: -kv[1].total):
            log.info(
                1,
                f"{category:<34} n={t.count:<5} total={format_duration(t.total):<8} "
                f"avg={format_duration(t.total / t.count):<7} max={format_duration(t.longest)}",
            )
    if perf.repos and cfg.perf_top > 0:
        slowest = sorted(perf.repos.items(), key=lambda kv: -kv[1].total)[: cfg.perf_top]
        log.info(0, f"Slowest repos (top {len(slowest)}):")
        for name, repo in slowest:
            groups = grouped_seconds(repo.categories, cfg.new_remote, cfg.old_remote)
            log.info(
                1,
                f"{name}: {format_duration(repo.total)} "
                f"(network {format_duration(network_seconds(repo.categories))}: "
                f"fetch {cfg.new_remote} {format_duration(groups['fetch_new_s'])}, "
                f"fetch {cfg.old_remote} {format_duration(groups['fetch_old_s'])}, "
                f"push {format_duration(groups['push_s'])}, "
                f"API {format_duration(groups['api_s'])})",
            )
    if perf.slow_calls:
        log.info(
            0, f"Commands slower than {format_duration(cfg.slow_command_seconds)} ({len(perf.slow_calls)}):"
        )
        slowest_calls = sorted(perf.slow_calls, key=lambda c: -c.seconds)[:MAX_SLOW_CALLS_SHOWN]
        for call in slowest_calls:
            log.info(1, f"{format_duration(call.seconds)}  {call.category}  {call.repo or ''}")
