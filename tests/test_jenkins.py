import subprocess

import pytest

from gitlab_sync.config import JenkinsRollbackConfig
from gitlab_sync.reports import write_rollback_instructions
from gitlab_sync.sync.jenkins import is_jenkins_commit
from tests.conftest import git


@pytest.fixture
def jenkins(world):
    world.cfg.jenkins_rollback = JenkinsRollbackConfig(enabled=True, committers=["jenkins*"])
    return world


def bash_block(text: str, heading: str) -> str:
    return text.split(heading, 1)[1].split("```bash\n", 1)[1].split("```", 1)[0]


def test_committer_matching():
    assert is_jenkins_commit("Jenkins", "ci@example.com", ["jenkins*"])
    assert is_jenkins_commit("Build Bot", "JENKINS@example.com", ["jenkins@*"])
    assert not is_jenkins_commit("alice", "alice@example.com", ["jenkins*"])


def test_conflict_caused_by_jenkins_creates_instructions_and_changes_nothing(jenkins):
    jenkins.push_commits(jenkins.new, "main", ("a", "j1", "jenkins"), ("b2", "x", "jenkins"))
    before = git(jenkins.new, "rev-parse", "main")
    _, result = jenkins.sync()
    assert git(jenkins.new, "rev-parse", "main") == before
    assert git(jenkins.repo, "rev-parse", "main") == before
    assert len(result.rollbacks) == 1
    item = result.rollbacks[0]
    assert [c.committer.split(" <")[0] for c in item.commits] == ["jenkins", "jenkins"]
    assert item.new_sha == before

    path = write_rollback_instructions(jenkins.cfg, result.rollbacks)
    text = path.read_text()
    assert path.name == "jenkins_rollback.md"
    assert "protected_branches/main?allow_force_push=true" in text
    assert "protected_branches/main?allow_force_push=false" in text
    assert f"--force-with-lease=refs/heads/main:{before}" in text


def test_generated_git_block_works_when_pasted(jenkins):
    jenkins.push_commits(jenkins.new, "main", ("a", "j1", "jenkins"), ("b2", "x", "jenkins"))
    _, result = jenkins.sync()
    text = write_rollback_instructions(jenkins.cfg, result.rollbacks).read_text()
    run = subprocess.run(["bash", "-c", bash_block(text, "**B)")], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert git(jenkins.new, "show", "main:a") == "2"
    assert git(jenkins.new, "ls-tree", "main", "b2") == ""


def test_git_block_aborts_if_the_branch_moved(jenkins):
    jenkins.push_commits(jenkins.new, "main", ("a", "j1", "jenkins"))
    _, result = jenkins.sync()
    text = write_rollback_instructions(jenkins.cfg, result.rollbacks).read_text()
    jenkins.push_commits(jenkins.new, "main", ("late", "x", "alice"))
    moved = git(jenkins.new, "rev-parse", "main")
    run = subprocess.run(["bash", "-c", bash_block(text, "**B)")], capture_output=True, text=True)
    assert run.returncode != 0
    assert git(jenkins.new, "rev-parse", "main") == moved


@pytest.mark.parametrize(
    "commits",
    [
        [("a", "j1", "jenkins"), ("h", "x", "alice")],
        [("a", "alice-change", "alice"), ("j", "x", "jenkins")],
    ],
)
def test_no_instructions_when_a_human_commit_is_involved(jenkins, commits):
    jenkins.push_commits(jenkins.new, "main", *commits)
    _, result = jenkins.sync()
    assert any("Branch main" in c for c in result.conflicts)
    assert result.rollbacks == []


def test_only_configured_branches_and_only_when_enabled(world):
    world.push_commits(world.new, "main", ("a", "j1", "jenkins"))
    _, result = world.sync()
    assert result.rollbacks == []
    world.cfg.jenkins_rollback = JenkinsRollbackConfig(
        enabled=True, committers=["jenkins*"], branches=["release"]
    )
    _, result = world.sync()
    assert result.rollbacks == []


def test_max_commits_limit(jenkins):
    jenkins.cfg.jenkins_rollback.max_commits = 1
    jenkins.push_commits(jenkins.new, "main", ("a", "j1", "jenkins"), ("b2", "x", "jenkins"))
    _, result = jenkins.sync()
    assert result.rollbacks == []


def test_instructions_are_also_collected_in_dry_run(jenkins):
    jenkins.push_commits(jenkins.new, "main", ("a", "j1", "jenkins"))
    before = git(jenkins.new, "rev-parse", "main")
    _, result = jenkins.sync(dry_run=True)
    assert len(result.rollbacks) == 1
    assert git(jenkins.new, "rev-parse", "main") == before
    assert "dry run" in write_rollback_instructions(jenkins.cfg, result.rollbacks).read_text()
