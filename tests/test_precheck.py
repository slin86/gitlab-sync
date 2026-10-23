import json
from unittest import mock

import pytest

from gitlab_sync.config import ApiPrecheckConfig, Config
from gitlab_sync.gitlab_api import ApiError, GitLabClient
from gitlab_sync.perf import PerfTracker
from gitlab_sync.precheck import build_precheck, old_has_changes


class FakeClient:
    def __init__(self, host, heads, tags=None, containing=None):
        self.host = host
        self.heads = heads
        self.tags = tags or {}
        self.containing = containing or {}
        self.calls = []

    def branch_heads(self, project):
        self.calls.append("branches")
        return self.heads

    def tag_targets(self, project):
        self.calls.append("tags")
        return self.tags

    def branches_containing(self, project, sha):
        self.calls.append("refs")
        return self.containing.get(sha)


def check(old, new, exclude=(), overwrite_tags=False):
    return old_has_changes(
        "grp/repo", old, new, exclude_branches=list(exclude), overwrite_tags=overwrite_tags
    )


def test_identical_heads_and_tags_mean_no_changes():
    old = FakeClient("old", {"main": "a"}, {"v1": "t"})
    new = FakeClient("new", {"main": "a", "other": "x"}, {"v1": "t"})
    assert check(old, new) is False
    assert old.calls == ["branches", "tags"]


def test_missing_branch_is_a_change_and_skips_the_tag_requests():
    old = FakeClient("old", {"main": "a", "feature": "b"})
    new = FakeClient("new", {"main": "a"})
    assert check(old, new) is True
    assert "tags" not in old.calls + new.calls


@pytest.mark.parametrize(
    ("containing", "expected"),
    [
        ({"a": {"main"}}, False),
        ({"a": {"other"}}, True),
        ({}, True),
    ],
)
def test_differing_head_asks_whether_new_branch_contains_old_head(containing, expected):
    old = FakeClient("old", {"main": "a"})
    new = FakeClient("new", {"main": "b"}, containing=containing)
    assert check(old, new) is expected


def test_tags():
    assert check(FakeClient("o", {}, {"v1": "x"}), FakeClient("n", {}, {})) is True
    assert check(FakeClient("o", {}, {"v1": "x"}), FakeClient("n", {}, {"v1": "y"})) is False
    assert (
        check(FakeClient("o", {}, {"v1": "x"}), FakeClient("n", {}, {"v1": "y"}), overwrite_tags=True) is True
    )


def test_excluded_branches_are_ignored():
    assert (
        check(FakeClient("o", {"main": "a", "wip/x": "b"}), FakeClient("n", {"main": "a"}), exclude=["wip/*"])
        is False
    )


def test_missing_project_raises():
    with pytest.raises(ApiError, match="not found on old"):
        check(FakeClient("old", None), FakeClient("new", {}))


def test_build_precheck_is_none_when_disabled(tmp_path):
    cfg = Config(root=tmp_path, new_host="n", old_host="o")
    assert build_precheck(cfg, PerfTracker()) is None


def test_build_precheck_requires_tokens(tmp_path, monkeypatch):
    monkeypatch.delenv("GITLAB_NEW_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_OLD_TOKEN", raising=False)
    cfg = Config(root=tmp_path, new_host="n", old_host="o", api_precheck=ApiPrecheckConfig(enabled=True))
    with pytest.raises(ApiError, match="token missing"):
        build_precheck(cfg, PerfTracker())("git@n:grp/repo.git")


class FakeResponse:
    def __init__(self, data, next_page=""):
        self.status = 200
        self.headers = {"X-Next-Page": next_page}
        self._body = json.dumps(data).encode()

    def read(self, *args):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_client_follows_pagination_and_encodes_the_project():
    pages = [
        FakeResponse([{"name": "a", "commit": {"id": "1"}}], "2"),
        FakeResponse([{"name": "b", "commit": {"id": "2"}}]),
    ]
    with mock.patch("gitlab_sync.gitlab_api.urlopen", side_effect=pages) as urlopen:
        heads = GitLabClient("h", "tok").branch_heads("grp/sub/repo")
    assert heads == {"a": "1", "b": "2"}
    first_url = urlopen.call_args_list[0].args[0].full_url
    assert "/projects/grp%2Fsub%2Frepo/repository/branches?per_page=100&page=1" in first_url
