from datetime import datetime
from pathlib import Path

from gitlab_sync.config import AuditConfig, Config
from gitlab_sync.findings import write_findings
from gitlab_sync.results import (
    BranchConflict,
    Finding,
    FindingKind,
    JenkinsCommit,
    ProtectedPush,
    RollbackInstruction,
    RunResult,
)
from tests.conftest import git

STARTED = datetime(2026, 10, 23, 9, 30)


def cfg(tmp_path, **kwargs):
    return Config(
        root=tmp_path / "work",
        new_host="gitlab.example.com",
        old_host="git.example.com",
        log_file=tmp_path / "sync.log",
        **kwargs,
    )


def test_empty_run_lists_every_category(tmp_path):
    path, count = write_findings(cfg(tmp_path), RunResult(repos=3), STARTED)
    text = path.read_text()
    assert path.name == "findings.md"
    assert count == 0
    assert "Run started 2026-10-23 09:30:00, mode: live. 3 repos checked, 0 findings." in text
    assert "* Merge conflicts: 0" in text
    assert (
        "* Repositories missing on the new instance: Not checked, `audit.repositories` is disabled." in text
    )
    assert "## Tag differences (0)\n\nNone." in text


def test_document_collects_all_findings(tmp_path):
    repo = tmp_path / "work" / "grp" / "repo"
    result = RunResult(repos=1)
    result.findings += [
        Finding(
            FindingKind.MISSING_REPO,
            "grp/gone",
            "exists on the old instance (active) but not on the new one",
            "https://git.example.com/grp/gone",
        ),
        Finding(FindingKind.TAG, "grp/repo", "Tag v1 differs"),
    ]
    result.branch_conflicts.append(BranchConflict(repo, "main", "a" * 40, "b" * 40, 3, True))
    result.protected_pushes.append(ProtectedPush(repo, "main", "git@gitlab.example.com:grp/repo.git"))
    result.rollbacks.append(
        RollbackInstruction(
            repo, "main", "", "b" * 40, "c" * 40, [JenkinsCommit("d" * 40, "jenkins", "x")], []
        )
    )
    result.errors.append("boom")
    result.warnings.append("hmm")
    path, count = write_findings(cfg(tmp_path, audit=AuditConfig(repositories=True)), result, STARTED)
    text = path.read_text()
    assert count == 7
    assert (
        "* **grp/gone**: exists on the old instance (active) but not on the new one "
        "([open](https://git.example.com/grp/gone))"
    ) in text
    assert (
        "* **grp/repo**: branch main, 3 commits from git-old cannot be merged "
        "(git-old aaaaaaaaaa, origin bbbbbbbbbb), caused by Jenkins commits"
    ) in text
    assert (
        "push to branch main was rejected ([settings](https://gitlab.example.com/grp/repo"
        "/-/settings/repository#js-protected-branches-settings))"
    ) in text
    assert "1 Jenkins commits block the merge, steps in jenkins_rollback.md" in text
    assert "## Errors (1)\n\n* boom" in text
    assert "## Other warnings (1)\n\n* hmm" in text
    order = [line for line in text.splitlines() if line.startswith("## ")]
    assert order[1].startswith("## Repositories missing") and order[-1].startswith("## Other warnings")


def test_dry_run_explains_missing_push_results(tmp_path):
    path, _ = write_findings(cfg(tmp_path, dry_run=True), RunResult(), STARTED)
    text = path.read_text()
    assert "mode: dry run, nothing was pushed" in text
    assert "No pushes in a dry run, see push permission problems instead." in text


def test_findings_file_can_be_disabled(tmp_path):
    assert write_findings(cfg(tmp_path, findings_file=""), RunResult(), STARTED) is None


class StubAudit:
    def check_repositories(self, log, result):
        result.findings.append(Finding(FindingKind.MISSING_REPO, "grp/gone", "missing"))

    def check_project(self, repo, new_url, log, depth, result):
        result.findings.append(Finding(FindingKind.FEATURE_FLAG, "grp/repo", "flag differs"))


def test_dry_run_writes_the_complete_document(world):
    from gitlab_sync.runner import run

    world.cfg.dry_run = True
    world.cfg.audit = AuditConfig(repositories=True, project_objects=True)
    before = git(world.new, "for-each-ref")
    code = run(world.cfg, [], console=False, old_url_override=lambda repo: str(world.old), audit=StubAudit())
    assert code == 1
    assert git(world.new, "for-each-ref") == before
    text = (world.cfg.log_file.parent / "findings.md").read_text()
    assert "* **grp/gone**: missing" in text
    assert "* **grp/repo**: flag differs" in text
    assert "branch diverge, 1 commits from git-old cannot be merged" in text
    assert "Findings: 3 written to" in world.cfg.log_file.read_text()


def test_audit_runs_even_when_the_precheck_skips_the_repo(world):
    result_holder = []

    class Recorder(StubAudit):
        def check_project(self, repo, new_url, log, depth, result):
            result_holder.append(Path(repo))

    world.sync(precheck=lambda url: False, audit=Recorder())
    assert result_holder == [world.repo]
