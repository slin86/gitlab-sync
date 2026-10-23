"""Cheap API based check whether the old instance has anything the new one is missing.

Lets the sync skip a repo without any fetch or checkout.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable

from gitlab_sync.config import Config
from gitlab_sync.gitlab_api import ApiError, GitLabClient
from gitlab_sync.perf import PerfTracker
from gitlab_sync.urls import project_path

Precheck = Callable[[str], bool]


def old_has_changes(
    project: str, old: GitLabClient, new: GitLabClient, *, exclude_branches: list[str], overwrite_tags: bool
) -> bool:
    """True if a branch or tag on the old instance is missing or ahead on the new one.

    Usually needs one branch request per instance. A branch whose head differs costs one more
    request to see whether the new branch already contains the old head. Tags are only fetched when
    all branches match.
    """
    old_heads = old.branch_heads(project)
    if old_heads is None:
        raise ApiError(f"project not found on {old.host}")
    new_heads = new.branch_heads(project)
    if new_heads is None:
        raise ApiError(f"project not found on {new.host}")

    for name, sha in old_heads.items():
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in exclude_branches):
            continue
        if name not in new_heads:
            return True
        if new_heads[name] == sha:
            continue
        containing = new.branches_containing(project, sha)
        if containing is None or name not in containing:
            return True

    new_tags = new.tag_targets(project)
    for name, target in old.tag_targets(project).items():
        if name not in new_tags:
            return True
        if overwrite_tags and new_tags[name] != target:
            return True
    return False


def build_precheck(cfg: Config, perf: PerfTracker) -> Precheck | None:
    """Return a precheck for remote URLs, or None if it is disabled in the config."""
    settings = cfg.api_precheck
    if not settings.enabled:
        return None

    def precheck(new_url: str) -> bool:
        new_token = os.environ.get(settings.new_token_env)
        old_token = os.environ.get(settings.old_token_env)
        if not new_token or not old_token:
            raise ApiError(f"token missing, set {settings.new_token_env} and {settings.old_token_env}")
        old = GitLabClient(cfg.old_host, old_token, settings.scheme, settings.timeout, perf)
        new = GitLabClient(cfg.new_host, new_token, settings.scheme, settings.timeout, perf)
        return old_has_changes(
            project_path(new_url),
            old,
            new,
            exclude_branches=cfg.exclude_branches,
            overwrite_tags=cfg.overwrite_tags,
        )

    return precheck
