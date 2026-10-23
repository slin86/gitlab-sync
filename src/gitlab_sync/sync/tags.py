"""Transfer of tags from the old remote to the new remote."""

from __future__ import annotations

from gitlab_sync.git import GitError
from gitlab_sync.sync.context import RepoContext

OLD_TAGS_NAMESPACE = "refs/gitlab_sync/old_tags"
PUSH_CHUNK = 200


def sync_tags(ctx: RepoContext) -> None:
    """Push tags that are missing on the new remote.

    A tag with the same name but a different target is overwritten when overwrite_tags is set,
    otherwise it is only reported. The old tags are fetched into a private namespace so they never
    clash with local tags, and that namespace is removed afterwards.
    """
    ctx.git.run("fetch", ctx.old, "--no-tags", "--prune", f"+refs/tags/*:{OLD_TAGS_NAMESPACE}/*")
    try:
        _sync_fetched_tags(ctx)
    finally:
        ctx.git.delete_refs(f"{OLD_TAGS_NAMESPACE}/")


def _sync_fetched_tags(ctx: RepoContext) -> None:
    git, depth = ctx.git, ctx.depth
    targets: dict[str, str] = {}
    previous: dict[str, str | None] = {}
    listing = git.run("for-each-ref", "--format=%(refname) %(objectname)", f"{OLD_TAGS_NAMESPACE}/")
    for line in listing.splitlines():
        ref, sha = line.split()
        tag = ref[len(OLD_TAGS_NAMESPACE) + 1 :]
        local = git.resolve(f"refs/tags/{tag}")
        if local == sha:
            continue
        if local is not None and not ctx.cfg.overwrite_tags:
            ctx.warn(depth, f"Tag {tag} differs between {ctx.old} and {ctx.new}, not overwritten")
            continue
        targets[tag] = sha
        previous[tag] = local

    if not targets:
        ctx.log.info(depth, "Tags: nothing to sync")
        return
    changed = sorted(tag for tag in targets if previous[tag] is not None)
    created = len(targets) - len(changed)
    ctx.log.info(depth, f"Tags: {created} new, {len(changed)} changed, {ctx.old} to {ctx.new}")
    suffix = " [DRY RUN]" if ctx.dry_run else ""
    for tag in changed:
        ctx.log.info(
            depth + 1, f"Tag {tag}: overwriting {previous[tag][:10]} with {targets[tag][:10]}{suffix}"
        )
    if ctx.dry_run:
        ctx.log.info(
            depth + 1, f"[DRY RUN] Skipping push of {len(targets)} tags: " + ", ".join(sorted(targets))
        )
        return

    for tag, sha in targets.items():
        git.run("update-ref", f"refs/tags/{tag}", sha)
    names = sorted(targets)
    for start in range(0, len(names), PUSH_CHUNK):
        _push_chunk(ctx, names[start : start + PUSH_CHUNK], previous)
    ctx.log.info(depth + 1, f"Pushed {len(names)} tags ({len(changed)} overwritten)")


def _push_chunk(ctx: RepoContext, chunk: list[str], previous: dict[str, str | None]) -> None:
    # A leading '+' forces the update of a changed tag. New tags are pushed without force.
    refspecs = [f"{'+' if previous[tag] else ''}refs/tags/{tag}:refs/tags/{tag}" for tag in chunk]
    try:
        ctx.git.run("push", ctx.new, *refspecs)
    except GitError:
        for tag in chunk:
            if previous[tag] is None:
                ctx.git.run("update-ref", "-d", f"refs/tags/{tag}", check=False)
            else:
                ctx.git.run("update-ref", f"refs/tags/{tag}", previous[tag], check=False)
        raise
    ctx.result.tags_overwritten += sum(1 for tag in chunk if previous[tag] is not None)
    ctx.result.tags_pushed += sum(1 for tag in chunk if previous[tag] is None)
