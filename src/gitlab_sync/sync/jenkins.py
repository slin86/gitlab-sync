"""Find out whether a merge conflict is caused by Jenkins commits on the new instance.

The analysis only probes merges locally. It never changes the remote; the caller writes
instructions that a developer runs by hand.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from gitlab_sync.results import JenkinsCommit
from gitlab_sync.sync.context import RepoContext


@dataclass(frozen=True)
class RollbackPlan:
    commits: list[JenkinsCommit]
    target_sha: str
    tags: list[str]


def is_jenkins_commit(committer_name: str, committer_email: str, patterns: list[str]) -> bool:
    """Case insensitive fnmatch of the committer name or email against the configured patterns."""
    values = (committer_name.strip().lower(), committer_email.strip().lower())
    return any(fnmatch.fnmatchcase(value, pattern.lower()) for pattern in patterns for value in values)


def find_rollback(ctx: RepoContext, branch: str) -> RollbackPlan | None:
    """Drop the newest commits of the new branch one by one while their committer is Jenkins.

    Returns the plan as soon as a probe merge succeeds. Returns None if rollback is disabled for the
    branch, a non Jenkins commit is in the way or more than max_commits would be needed. The caller
    resets the local branch afterwards.
    """
    settings = ctx.cfg.jenkins_rollback
    if not settings.enabled or branch not in settings.branches or not settings.committers:
        return None
    git, depth = ctx.git, ctx.depth + 1
    base = git.output_or_none("merge-base", ctx.new_ref(branch), ctx.old_ref(branch))
    if base is None:
        return None

    dropped: list[JenkinsCommit] = []
    for sha in git.run("rev-list", "--first-parent", f"{base}..{ctx.new_ref(branch)}").splitlines():
        name, _, email = git.run("show", "-s", "--format=%cn%x00%ce", sha).partition("\0")
        committer = f"{name} <{email}>"
        if not is_jenkins_commit(name, email, settings.committers):
            ctx.log.info(
                depth, f"No Jenkins rollback possible: commit {sha[:10]} ({committer}) is not from Jenkins"
            )
            return None
        if len(dropped) >= settings.max_commits:
            ctx.log.info(
                depth, f"No Jenkins rollback possible: more than {settings.max_commits} Jenkins commits"
            )
            return None
        dropped.append(JenkinsCommit(sha, committer, git.run("show", "-s", "--format=%s", sha)))
        target = git.run("rev-parse", f"{sha}^")
        git.checkout_reset(branch, target)
        if ctx.try_merge(branch):
            return RollbackPlan(dropped, target, _tags_on(ctx, dropped))
    return None


def _tags_on(ctx: RepoContext, commits: list[JenkinsCommit]) -> list[str]:
    tags = set()
    for commit in commits:
        tags.update(ctx.git.run("tag", "--points-at", commit.sha).splitlines())
    return sorted(tags)
