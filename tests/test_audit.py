from pathlib import Path
from unittest import mock

import pytest

from gitlab_sync.audit import Audit, MissingTokenError, build_audit
from gitlab_sync.audit.api import AuditApi
from gitlab_sync.audit.repositories import compare_repositories, in_namespaces
from gitlab_sync.config import AuditConfig, Config
from gitlab_sync.gitlab_api import ApiError, endpoint_kind
from gitlab_sync.log import TreeLog
from gitlab_sync.perf import PerfTracker
from gitlab_sync.results import FindingKind, RunResult


class FakeAuditApi:
    def __init__(self, host, active=(), archived=(), **data):
        self.host = host
        self.active = set(active)
        self.archived = set(archived)
        self.data = data
        self.calls = []

    def project_paths(self, archived):
        self.calls.append(("projects", archived))
        return self.archived if archived else self.active

    def __getattr__(self, name):
        def method(*args):
            self.calls.append((name, *args))
            value = self.data.get(name, [])
            if isinstance(value, Exception):
                raise value
            return value

        return method


def test_in_namespaces():
    assert in_namespaces("team/a", [])
    assert in_namespaces("team/a/b", ["team"])
    assert in_namespaces("team", ["team/"])
    assert not in_namespaces("teamx/a", ["team"])


def test_compare_repositories():
    old = FakeAuditApi("old.example.com", active={"a/one", "a/two", "b/x"}, archived={"a/gone", "a/three"})
    new = FakeAuditApi("new.example.com", active={"a/one", "a/three"}, archived={"a/two", "c/new"})
    findings = compare_repositories(old, new, ["a"], "https")
    assert [(f.kind, f.subject, f.message, f.link) for f in findings] == [
        (
            FindingKind.MISSING_REPO,
            "a/gone",
            "exists on the old instance (archived) but not on the new one",
            "https://old.example.com/a/gone",
        ),
        (
            FindingKind.ARCHIVE_STATUS,
            "a/three",
            "archived on the old instance, active on the new one",
            "https://new.example.com/a/three/edit",
        ),
        (
            FindingKind.ARCHIVE_STATUS,
            "a/two",
            "active on the old instance, archived on the new one",
            "https://new.example.com/a/two/edit",
        ),
    ]


def audit_cfg(tmp_path, **switches):
    return Config(
        root=tmp_path,
        new_host="new.example.com",
        old_host="old.example.com",
        audit=AuditConfig(**switches),
        exclude_branches=["wip/*"],
    )


@pytest.fixture
def log(tmp_path):
    with TreeLog(tmp_path / "audit.log", console=False) as tree_log:
        yield tree_log


def test_check_project_records_findings_and_continues_after_api_errors(tmp_path, log):
    old = FakeAuditApi("old", branch_names=["main", "wip/x"], environments=ApiError("down"))
    new = FakeAuditApi(
        "new",
        protected_branches=[{"name": "*", "push_access_levels": [{"access_level": 0}]}],
    )
    audit = Audit(audit_cfg(tmp_path, permissions=True, project_objects=True), old, new)
    result = RunResult()
    audit.check_project(Path("/w/grp/repo"), "git@new.example.com:grp/repo.git", log, 2, result)

    (finding,) = result.findings
    assert finding.kind is FindingKind.PUSH_PERMISSION
    assert "affects main" in finding.message
    assert (
        finding.link
        == "https://new.example.com/grp/repo/-/settings/repository#js-protected-branches-settings"
    )
    assert result.errors == ["Project object comparison of grp/repo failed: down"]


def test_project_objects_are_compared_with_links_to_the_new_instance(tmp_path, log):
    old = FakeAuditApi("old", feature_flags=[{"name": "f", "active": True, "strategies": []}])
    new = FakeAuditApi("new")
    audit = Audit(audit_cfg(tmp_path, project_objects=True), old, new)
    result = RunResult()
    audit.check_project(Path("/w"), "https://new.example.com/grp/repo.git", log, 2, result)
    (finding,) = result.findings
    assert finding.link == "https://new.example.com/grp/repo/-/feature_flags"
    assert ("branch_names", "grp/repo") not in old.calls


def test_disabled_checks_make_no_requests(tmp_path, log):
    old, new = FakeAuditApi("old"), FakeAuditApi("new")
    audit = Audit(audit_cfg(tmp_path), old, new)
    audit.check_repositories(log, RunResult())
    audit.check_project(Path("/w"), "git@new.example.com:grp/repo.git", log, 2, RunResult())
    assert old.calls == new.calls == []


def test_build_audit(tmp_path, monkeypatch):
    assert build_audit(audit_cfg(tmp_path), PerfTracker()) is None
    monkeypatch.delenv("GITLAB_OLD_TOKEN", raising=False)
    monkeypatch.setenv("GITLAB_NEW_TOKEN", "n")
    with pytest.raises(MissingTokenError):
        build_audit(audit_cfg(tmp_path, repositories=True), PerfTracker())
    monkeypatch.setenv("GITLAB_OLD_TOKEN", "o")
    audit = build_audit(audit_cfg(tmp_path, repositories=True), PerfTracker())
    assert (audit.old.host, audit.new.host) == ("old.example.com", "new.example.com")


def test_audit_api_requests():
    client = mock.Mock(host="h")
    client.get_all.return_value = [{"path_with_namespace": "a/b"}]
    api = AuditApi(client)
    assert api.project_paths(archived=True) == {"a/b"}
    client.get_all.assert_called_with("/projects", {"simple": "true", "archived": "true"})
    api.protected_branches("grp/sub/repo")
    client.get_all.assert_called_with("/projects/grp%2Fsub%2Frepo/protected_branches")


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/projects", "projects"),
        ("/user", "user"),
        ("/projects/a%2Fb/protected_branches", "protected_branches"),
        ("/projects/a%2Fb/pipeline_schedules/3", "pipeline_schedules"),
        ("/projects/a%2Fb/repository/commits/abc/refs", "commit_refs"),
    ],
)
def test_endpoint_kind(path, kind):
    assert endpoint_kind(path) == kind
