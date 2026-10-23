from gitlab_sync.config import JenkinsRollbackConfig
from gitlab_sync.reports import (
    protected_branches_url,
    write_branch_conflicts,
    write_protected_branches,
)
from gitlab_sync.results import ProtectedPush
from tests.conftest import commit, git

PAGE = "/-/settings/repository#js-protected-branches-settings"


def test_conflict_file_has_one_line_per_branch_without_duplicates(world):
    _, result = world.sync()
    assert [c.branch for c in result.branch_conflicts] == ["diverge"]
    path = write_branch_conflicts(world.cfg, result.branch_conflicts)
    lines = path.read_text().splitlines()
    assert path.name == "branch_conflicts.txt"
    assert len(lines) == 1
    assert " | grp/repo | diverge | git-old=" in lines[0]
    assert lines[0].endswith(" | 1 commits")

    _, second = world.sync()
    write_branch_conflicts(world.cfg, second.branch_conflicts)
    assert len(path.read_text().splitlines()) == 1


def test_conflict_file_gets_a_new_line_when_the_state_changes(world):
    _, result = world.sync()
    path = write_branch_conflicts(world.cfg, result.branch_conflicts)
    work = world.clone(world.old)
    git(work, "checkout", "-q", "diverge")
    commit(work, "a", "even newer", "old again")
    git(work, "push", "-q", "origin", "diverge")
    _, second = world.sync()
    write_branch_conflicts(world.cfg, second.branch_conflicts)
    assert len(path.read_text().splitlines()) == 2


def test_conflict_file_marks_jenkins_rollbacks(world):
    world.cfg.jenkins_rollback = JenkinsRollbackConfig(enabled=True, committers=["jenkins*"])
    world.push_commits(world.new, "main", ("a", "j1", "jenkins"))
    _, result = world.sync()
    lines = write_branch_conflicts(world.cfg, result.branch_conflicts).read_text().splitlines()
    main = next(line for line in lines if " | main | " in line)
    diverge = next(line for line in lines if " | diverge | " in line)
    assert main.endswith("Jenkins rollback instructions")
    assert "Jenkins" not in diverge


def test_report_files_are_not_created_without_entries(world):
    assert write_branch_conflicts(world.cfg, []) is None
    assert write_protected_branches(world.cfg, []) is None


def test_protected_branches_url(world):
    for url in ("git@gitlab.example.com:grp/sub/repo.git", "https://gitlab.example.com/grp/sub/repo.git"):
        assert protected_branches_url(world.cfg, url) == f"https://gitlab.example.com/grp/sub/repo{PAGE}"


def test_protected_branches_file_lists_links_without_duplicates(world):
    url = "git@gitlab.example.com:grp/repo.git"
    pushes = [ProtectedPush(world.repo, "main", url), ProtectedPush(world.repo, "release", url)]
    path = write_protected_branches(world.cfg, pushes)
    text = path.read_text()
    assert f"* [grp/repo](https://gitlab.example.com/grp/repo{PAGE}) branch main" in text
    assert " branch release" in text

    write_protected_branches(world.cfg, pushes)
    assert path.read_text() == text
    assert text.count("# Repos with a protected branch") == 1

    write_protected_branches(world.cfg, [*pushes, ProtectedPush(world.repo, "dev", url)])
    assert len(path.read_text().splitlines()) == len(text.splitlines()) + 1


def test_protected_branches_file_falls_back_to_the_remote_url(world):
    path = write_protected_branches(world.cfg, [ProtectedPush(world.repo, "main", str(world.new))])
    assert f"]({world.new})" in path.read_text()
