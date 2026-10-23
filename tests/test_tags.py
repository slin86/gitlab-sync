from gitlab_sync.results import FindingKind
from tests.conftest import git


def test_changed_tag_is_only_reported_by_default(world):
    old_v1 = world.move_old_tag()
    new_v1 = git(world.new, "rev-parse", "v1")
    _, result = world.sync()
    assert git(world.new, "rev-parse", "v1") == new_v1 != old_v1
    (finding,) = result.findings
    assert finding.kind is FindingKind.TAG
    assert finding.subject == "grp/repo"
    assert finding.message.startswith(f"Tag v1 differs between git-old ({old_v1[:10]}) and origin")


def test_changed_tag_is_overwritten_when_enabled(world):
    old_v1 = world.move_old_tag()
    world.cfg.overwrite_tags = True
    _, result = world.sync()
    assert git(world.new, "rev-parse", "v1") == old_v1
    assert git(world.repo, "rev-parse", "v1") == old_v1
    assert result.tags_overwritten == 1

    _, second = world.sync()
    assert second.tags_overwritten == 0


def test_dry_run_does_not_overwrite_tags(world):
    world.move_old_tag()
    new_v1 = git(world.new, "rev-parse", "v1")
    world.cfg.overwrite_tags = True
    world.sync(dry_run=True)
    assert git(world.new, "rev-parse", "v1") == new_v1


def test_temporary_tag_namespace_is_removed(world):
    world.sync()
    assert git(world.repo, "for-each-ref", "refs/gitlab_sync/") == ""
