"""Sync of one repository: prepare remotes, sync every branch, then the tags."""

from __future__ import annotations

import fnmatch
from pathlib import Path

from gitlab_sync.audit import Audit
from gitlab_sync.config import Config
from gitlab_sync.git import Git, GitError
from gitlab_sync.gitlab_api import ApiError
from gitlab_sync.log import TreeLog
from gitlab_sync.perf import PerfTracker
from gitlab_sync.precheck import Precheck
from gitlab_sync.results import RepoOutcome, RunResult
from gitlab_sync.sync.branch import sync_branch
from gitlab_sync.sync.context import RepoContext
from gitlab_sync.sync.tags import sync_tags
from gitlab_sync.urls import UrlError, derive_old_url


def sync_repo(
    path: Path,
    cfg: Config,
    log: TreeLog,
    depth: int,
    result: RunResult,
    *,
    perf: PerfTracker | None = None,
    old_url: str | None = None,
    precheck: Precheck | None = None,
    audit: Audit | None = None,
) -> RepoOutcome:
    """Sync one checkout. old_url overrides the URL derived from origin, mainly for tests.

    The audit runs first, so permission problems show up before any push is tried, and it also runs
    when the precheck skips the git part.
    """
    git = Git(path, cfg.git_timeout, perf)
    ctx = RepoContext(path, cfg, git, log, result, depth + 1)
    problems_before = result.problem_count
    original_branch = git.output_or_none("symbolic-ref", "--short", "-q", "HEAD")
    original_sha = git.resolve("HEAD")

    new_url = git.remote_url(cfg.new_remote)
    if not new_url:
        ctx.fail(ctx.depth, f"Remote {cfg.new_remote} not found, repo skipped")
        return RepoOutcome.PROBLEM
    try:
        expected_old_url = old_url or derive_old_url(new_url, cfg.new_host, cfg.old_host)
    except UrlError as exc:
        ctx.fail(ctx.depth, f"{exc}, repo skipped")
        return RepoOutcome.PROBLEM
    if audit:
        audit.check_project(path, new_url, log, ctx.depth, result)
    if precheck and not _old_has_changes(ctx, precheck, new_url):
        result.repos_skipped += 1
        return RepoOutcome.SKIPPED if result.problem_count == problems_before else RepoOutcome.PROBLEM

    try:
        _prepare(ctx, new_url, expected_old_url)
        _sync_branches(ctx)
        _sync_tags(ctx)
    except GitError as exc:
        ctx.fail(ctx.depth, str(exc))
    finally:
        _restore(ctx, original_branch, original_sha)
    return RepoOutcome.OK if result.problem_count == problems_before else RepoOutcome.PROBLEM


def _old_has_changes(ctx: RepoContext, precheck: Precheck, new_url: str) -> bool:
    try:
        changed = precheck(new_url)
    except (ApiError, UrlError) as exc:
        ctx.log.warning(ctx.depth, f"API precheck failed ({exc}), running full sync")
        return True
    if changed:
        ctx.log.info(ctx.depth, f"API precheck: changes found on {ctx.cfg.old_host}")
    else:
        ctx.log.info(ctx.depth, f"API precheck: nothing new on {ctx.cfg.old_host}, skipped")
    return changed


def _prepare(ctx: RepoContext, new_url: str, old_url: str) -> None:
    git, depth = ctx.git, ctx.depth
    if git.run("status", "--porcelain", check=False):
        ctx.log.warning(depth, "Local changes found, discarding them")
        git.run("reset", "--hard", check=False)
        git.run("clean", "-fdq", check=False)

    ctx.log.info(depth, f"Fetching {ctx.new} ({new_url})")
    git.run("fetch", ctx.new, "--prune")
    git.run("fetch", ctx.new, "--tags", "--force")

    current = git.remote_url(ctx.old)
    if current is None:
        ctx.log.info(depth, f"Adding remote {ctx.old}: {old_url}")
        git.run("remote", "add", ctx.old, old_url)
    elif current != old_url:
        ctx.log.warning(depth, f"Remote {ctx.old} pointed to {current}, changing it to {old_url}")
        git.run("remote", "set-url", ctx.old, old_url)
    else:
        ctx.log.info(depth, f"Remote {ctx.old} already present ({old_url})")

    ctx.log.info(depth, f"Fetching {ctx.old}")
    git.run("fetch", ctx.old, "--prune", "--no-tags")


def _sync_branches(ctx: RepoContext) -> None:
    old_branches = ctx.git.remote_branches(ctx.old)
    only_new = set(ctx.git.remote_branches(ctx.new)) - set(old_branches)
    if not old_branches:
        ctx.log.info(ctx.depth, f"No branches on {ctx.old}, nothing to sync")
    if only_new:
        ctx.log.info(ctx.depth, f"{len(only_new)} branches exist only on {ctx.new} and stay unchanged")
    for branch in old_branches:
        if any(fnmatch.fnmatchcase(branch, pattern) for pattern in ctx.cfg.exclude_branches):
            ctx.log.info(ctx.depth + 1, f"Branch {branch}: skipped (exclude_branches)")
            continue
        try:
            sync_branch(ctx, branch)
        except GitError as exc:
            ctx.fail(ctx.depth + 1, f"Branch {branch}: {exc}")


def _sync_tags(ctx: RepoContext) -> None:
    try:
        sync_tags(ctx)
    except GitError as exc:
        ctx.fail(ctx.depth + 1, f"Tags: {exc}")


def _restore(ctx: RepoContext, branch: str | None, sha: str | None) -> None:
    """Check out what was checked out before and drop local branches whose first push failed."""
    if ctx.cfg.restore_branch:
        if branch:
            ctx.git.run("checkout", "-f", branch, check=False)
        elif sha:
            ctx.git.run("checkout", "-f", "--detach", sha, check=False)
    for created in ctx.created_branches:
        ctx.git.run("branch", "-D", created, check=False)
