from pathlib import Path
from unittest import mock

import pytest

from gitlab_sync.git import Git, command_category
from gitlab_sync.gitlab_api import ApiError, GitLabClient
from gitlab_sync.perf import PerfTracker, Timing, format_duration, network_seconds
from tests.conftest import git


@pytest.mark.parametrize(
    ("args", "category"),
    [
        (("fetch", "origin", "--prune"), "git fetch origin"),
        (("fetch", "--no-tags", "git-old", "+refs/tags/*:x"), "git fetch git-old"),
        (("push", "-u", "origin", "refs/heads/main"), "git push origin"),
        (("merge", "--no-edit", "x"), "git merge"),
    ],
)
def test_command_category(args, category):
    assert command_category(args) == category


@pytest.mark.parametrize(("seconds", "text"), [(1.234, "1.2s"), (75, "1m15s"), (3725, "1h02m")])
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


def test_records_per_category_and_per_repo():
    perf = PerfTracker(slow_threshold=5)
    with perf.repo("a/b"):
        perf.record("git fetch origin", 2.0)
        perf.record("git fetch origin", 4.0)
        perf.record("git merge", 1.0)
    perf.record("git fetch origin", 1.0)
    assert perf.categories["git fetch origin"] == Timing(3, 7.0, 4.0)
    assert perf.repos["a/b"].categories["git fetch origin"] == Timing(2, 6.0, 4.0)
    assert network_seconds(perf.repos["a/b"].categories) == 6.0
    assert perf.slow_calls == []


def test_slow_calls_are_reported_unless_disabled():
    seen = []
    perf = PerfTracker(slow_threshold=5, on_slow=seen.append)
    perf.record("api h branches", 6.5)
    assert [(c.seconds, c.category) for c in seen] == [(6.5, "api h branches")]

    disabled = PerfTracker(slow_threshold=0)
    disabled.record("git fetch origin", 999)
    assert disabled.slow_calls == []


def test_measure_records_even_when_the_block_raises():
    perf = PerfTracker()
    with pytest.raises(RuntimeError), perf.measure("git push origin"):
        raise RuntimeError("boom")
    assert perf.categories["git push origin"].count == 1


def test_git_commands_and_api_requests_are_measured(tmp_path: Path):
    git(tmp_path, "init", "-q")
    perf = PerfTracker()
    repo = Git(tmp_path, perf=perf)
    repo.run("status")
    repo.succeeds("rev-parse", "--git-dir")
    assert perf.categories["git status"].count == 1
    assert perf.categories["git rev-parse"].count == 1

    with mock.patch("gitlab_sync.gitlab_api.urlopen", side_effect=OSError("down")), pytest.raises(ApiError):
        GitLabClient("h.example", "tok", perf=perf).get("/projects/x/repository/tags", {})
    assert perf.categories["api h.example tags"].count == 1
