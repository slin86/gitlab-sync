"""Run the sync over all repositories below the configured root."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from gitlab_sync.config import Config
from gitlab_sync.discovery import find_repos
from gitlab_sync.log import TreeLog
from gitlab_sync.perf import PerfTracker, SlowCall, format_duration, network_seconds
from gitlab_sync.precheck import Precheck, build_precheck
from gitlab_sync.reports import (
    write_branch_conflicts,
    write_protected_branches,
    write_rollback_instructions,
)
from gitlab_sync.results import RepoOutcome, RunResult
from gitlab_sync.summary import log_perf_report, log_summary
from gitlab_sync.sync import sync_repo

EXIT_ROOT_MISSING = 2

OldUrlOverride = Callable[[Path], "str | None"]


class FolderTree:
    """Logs a folder line whenever the walk enters a folder that was not logged yet."""

    def __init__(self, log: TreeLog):
        self.log = log
        self.current: list[str] = []

    def enter(self, folders: list[str]) -> int:
        """Log the new folder levels and return the depth for the repo line."""
        common = 0
        while common < min(len(self.current), len(folders)) and self.current[common] == folders[common]:
            common += 1
        for depth in range(common, len(folders)):
            self.log.info(depth + 1, f"Syncing folder {folders[depth]}")
        self.current = folders
        return len(folders) + 1


def run(
    cfg: Config,
    only: list[str],
    *,
    console: bool = True,
    old_url_override: OldUrlOverride | None = None,
    precheck: Precheck | None = None,
) -> int:
    """Sync every selected repo and write the summary, the report files and the timings.

    Returns 0 on success, 1 if any repo had errors or conflicts and 2 if the root is missing.
    """
    started = datetime.now()
    start = time.perf_counter()
    with TreeLog(cfg.log_file, console=console) as log:
        perf = PerfTracker(cfg.slow_command_seconds, on_slow=_slow_call_logger(log))
        precheck = precheck or build_precheck(cfg, perf)
        mode = "dry run, no push" if cfg.dry_run else "live"
        log.info(0, f"gitlab_sync started at {started:%Y-%m-%d %H:%M:%S}, mode: {mode}")
        if not cfg.root.is_dir():
            log.error(0, f"Root directory not found: {cfg.root}")
            return EXIT_ROOT_MISSING

        repos = find_repos(cfg.root, cfg.include, cfg.exclude, only)
        log.info(0, f"Syncing folder {cfg.root} ({len(repos)} repos)")
        result = RunResult()
        tree = FolderTree(log)
        for repo in repos:
            rel = repo.relative_to(cfg.root)
            depth = tree.enter(list(rel.parts[:-1]))
            log.info(depth, f"Repo {repo.name}")
            result.repos += 1
            with perf.repo(rel.as_posix()) as timing:
                old_url = old_url_override(repo) if old_url_override else None
                outcome = sync_repo(
                    repo, cfg, log, depth, result, perf=perf, old_url=old_url, precheck=precheck
                )
            timing.status = outcome.value
            log.info(
                depth + 1,
                f"Took {format_duration(timing.total)} "
                f"(network {format_duration(network_seconds(timing.categories))})",
            )
            if outcome is not RepoOutcome.PROBLEM:
                result.repos_ok += 1

        files = {
            "conflicts": write_branch_conflicts(cfg, result.branch_conflicts),
            "protected": write_protected_branches(cfg, result.protected_pushes),
            "rollbacks": write_rollback_instructions(cfg, result.rollbacks),
        }
        log_summary(log, result, files)
        log_perf_report(log, perf, cfg, time.perf_counter() - start)
        perf_path = cfg.output_path(cfg.perf_file)
        if perf_path and perf.repos:
            perf.append_csv(perf_path, started, cfg.new_remote, cfg.old_remote)
            log.info(0, f"Performance per repo written to {perf_path}")
        log.info(0, f"Finished in {format_duration(time.perf_counter() - start)}")
        return result.exit_code


def _slow_call_logger(log: TreeLog) -> Callable[[SlowCall], None]:
    def report(call: SlowCall) -> None:
        where = f" ({call.repo})" if call.repo else ""
        log.warning(0, f"Slow command: {call.category} took {format_duration(call.seconds)}{where}")

    return report
