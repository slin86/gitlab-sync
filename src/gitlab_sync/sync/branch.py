"""Sync of a single branch from the old remote into the new remote."""

from __future__ import annotations

from gitlab_sync.git import GitError
from gitlab_sync.results import BranchConflict, FindingKind, ProtectedPush, RollbackInstruction
from gitlab_sync.sync.context import RepoContext
from gitlab_sync.sync.jenkins import find_rollback

PROTECTED_BRANCH_MESSAGE = "not allowed to push code to protected branches"


def sync_branch(ctx: RepoContext, branch: str) -> None:
    """Create, fast forward or merge one branch and push it. Conflicts are recorded, not resolved."""
    git, depth = ctx.git, ctx.depth
    new_ref, old_ref = ctx.new_ref(branch), ctx.old_ref(branch)
    old_sha = git.run("rev-parse", old_ref)
    new_sha = git.resolve(new_ref)

    if new_sha is None:
        _create_branch(ctx, branch)
        return
    if new_sha == old_sha or git.is_ancestor(old_sha, new_sha):
        ctx.log.info(depth, f"Branch {branch}: up to date")
        ctx.result.branches_current += 1
        return

    count = git.count(f"{new_ref}..{old_ref}")
    local_sha = git.resolve(f"refs/heads/{branch}")
    if local_sha and local_sha != new_sha and not git.is_ancestor(local_sha, new_sha):
        ctx.finding(
            depth,
            FindingKind.LOCAL_BRANCH,
            f"Local branch {branch} has commits that are not on {ctx.new}, skipped",
        )
        return

    ctx.log.info(depth, f"Checking out branch {branch}")
    git.checkout_reset(branch, new_ref)
    if git.is_ancestor(new_sha, old_sha):
        mode = "fast forward"
        git.run("merge", "--ff-only", old_ref)
    elif ctx.try_merge(branch):
        mode = "merge"
    else:
        _record_conflict(ctx, branch, old_sha, new_sha, count)
        return

    ctx.log.info(depth, f"Syncing {count} commits from {ctx.old} to {ctx.new} ({mode})")
    pushed = _push(ctx, branch)
    if pushed:
        ctx.result.branches_updated += 1
        ctx.result.commits += count
    if ctx.dry_run or not pushed:
        git.checkout_reset(branch, new_ref)


def _create_branch(ctx: RepoContext, branch: str) -> None:
    old_ref = ctx.old_ref(branch)
    count = ctx.git.count(old_ref)
    ctx.log.info(ctx.depth, f"Branch {branch}: new on {ctx.new} ({count} commits)")
    if not ctx.dry_run:
        ctx.git.checkout_reset(branch, old_ref, "--no-track")
        ctx.created_branches.append(branch)
    if _push(ctx, branch):
        if not ctx.dry_run:
            ctx.created_branches.remove(branch)
        ctx.result.branches_new += 1
        ctx.result.commits += count


def _push(ctx: RepoContext, branch: str) -> bool:
    depth = ctx.depth + 1
    if ctx.dry_run:
        ctx.log.info(depth, f"[DRY RUN] Skipping push of {branch} to {ctx.new}")
        return True
    try:
        ctx.git.run("push", "-u", ctx.new, f"refs/heads/{branch}:refs/heads/{branch}")
    except GitError as exc:
        ctx.fail(depth, f"Push of {branch} failed: {exc}")
        if PROTECTED_BRANCH_MESSAGE in str(exc):
            ctx.result.protected_pushes.append(ProtectedPush(ctx.path, branch, ctx.new_url()))
            ctx.log.info(
                depth,
                f"Branch {branch} is protected on the new instance, link added to "
                f"{ctx.cfg.protected_branches_file}",
            )
        return False
    ctx.log.info(depth, f"Pushed {branch} to {ctx.new}")
    return True


def _record_conflict(ctx: RepoContext, branch: str, old_sha: str, new_sha: str, count: int) -> None:
    new_ref = ctx.new_ref(branch)
    ctx.git.checkout_reset(branch, new_ref)
    plan = find_rollback(ctx, branch)
    ctx.git.checkout_reset(branch, new_ref)

    ctx.result.branch_conflicts.append(
        BranchConflict(ctx.path, branch, old_sha, new_sha, count, plan is not None)
    )
    message = f"Branch {branch}: merge of {ctx.old}/{branch} failed ({count} commits), skipped"
    if plan:
        ctx.result.rollbacks.append(
            RollbackInstruction(
                ctx.path, branch, ctx.new_url(), new_sha, plan.target_sha, plan.commits, plan.tags
            )
        )
        message += (
            f"; {len(plan.commits)} Jenkins commits block the merge, rollback instructions "
            f"go to {ctx.cfg.jenkins_rollback.instructions_file}"
        )
    ctx.conflict(ctx.depth, message)
