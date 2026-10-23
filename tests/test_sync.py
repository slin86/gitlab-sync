from gitlab_sync.gitlab_api import ApiError
from gitlab_sync.results import FindingKind, RepoOutcome
from tests.conftest import PROTECTED_REJECTION, commit, git


def test_dry_run_pushes_nothing_and_restores_the_checkout(world):
    before = git(world.new, "for-each-ref")
    outcome, result = world.sync(dry_run=True)
    assert git(world.new, "for-each-ref") == before
    assert git(world.repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert "feature" not in git(world.repo, "branch", "--list").split()
    assert outcome is RepoOutcome.PROBLEM
    assert len(result.conflicts) == 1


def test_live_sync_creates_fast_forwards_and_skips_conflicts(world):
    outcome, result = world.sync()
    assert outcome is RepoOutcome.PROBLEM
    assert len(result.conflicts) == 1
    assert result.errors == []
    assert "feature" in world.heads(world.new)
    assert git(world.new, "rev-parse", "main") == git(world.old, "rev-parse", "main")
    assert git(world.new, "show", "diverge:a") == "new"
    assert "v2" in git(world.new, "tag").split()
    assert git(world.repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    _, second = world.sync()
    assert second.commits == 0
    assert len(second.conflicts) == 1


def test_merges_non_conflicting_divergence(world):
    world.push_commits(world.new, "main", ("n", "n", "dev"))
    world.sync()
    git(world.new, "show", "main:n")
    git(world.new, "show", "main:b")


def test_excluded_branches_are_not_synced(world):
    world.cfg.exclude_branches = ["feat*"]
    world.sync()
    assert "feature" not in world.heads(world.new)
    assert git(world.new, "rev-parse", "main") == git(world.old, "rev-parse", "main")


def test_local_branch_with_unpublished_commits_is_skipped(world):
    commit(world.repo, "local", "x", "unpublished")
    before = git(world.new, "rev-parse", "main")
    _, result = world.sync()
    assert git(world.new, "rev-parse", "main") == before
    assert [f.kind for f in result.findings] == [FindingKind.LOCAL_BRANCH]
    assert result.warnings == []


def test_precheck_without_changes_skips_the_repo_entirely(world):
    before = git(world.new, "for-each-ref")
    outcome, result = world.sync(precheck=lambda url: False)
    assert outcome is RepoOutcome.SKIPPED
    assert result.repos_skipped == 1
    assert git(world.new, "for-each-ref") == before
    assert "git-old" not in git(world.repo, "remote").split()


def test_failing_precheck_falls_back_to_full_sync(world):
    def failing(url):
        raise ApiError("boom")

    _, result = world.sync(precheck=failing)
    assert result.repos_skipped == 0
    assert "feature" in world.heads(world.new)


def test_precheck_with_changes_runs_full_sync(world):
    world.sync(precheck=lambda url: True)
    assert "feature" in world.heads(world.new)


def test_protected_branch_rejection_is_recorded(world):
    world.reject_pushes(PROTECTED_REJECTION)
    outcome, result = world.sync()
    assert outcome is RepoOutcome.PROBLEM
    assert result.errors
    assert {p.branch for p in result.protected_pushes} >= {"feature"}
    assert all(p.repo == world.repo for p in result.protected_pushes)
    assert "feature" not in git(world.repo, "branch", "--list").split()


def test_other_push_errors_are_not_recorded_as_protected(world):
    world.reject_pushes("some other failure")
    _, result = world.sync()
    assert result.errors
    assert result.protected_pushes == []


def test_dry_run_never_records_protected_pushes(world):
    world.reject_pushes(PROTECTED_REJECTION)
    _, result = world.sync(dry_run=True)
    assert result.protected_pushes == []
