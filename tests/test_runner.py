import re

from gitlab_sync.cli import main
from gitlab_sync.runner import run
from tests.conftest import git


def run_world(world, **kwargs):
    return run(world.cfg, [], console=False, old_url_override=lambda repo: str(world.old), **kwargs)


def test_full_run_writes_summary_performance_report_and_csv(world):
    world.cfg.slow_command_seconds = 0
    assert run_world(world) == 1

    log = world.cfg.log_file.read_text()
    for expected in (
        "Syncing folder grp",
        "-> Repo repo",
        "Summary",
        "Branch conflicts: 1 written to",
        "Performance",
        "git fetch origin",
        "git fetch git-old",
        "git push origin",
        "Slowest repos (top 1):",
        "grp/repo: ",
    ):
        assert expected in log
    assert re.search(r"-> Took [\d.]+s \(network [\d.]+s\)", log)

    csv_path = world.cfg.log_file.parent / "performance.csv"
    rows = csv_path.read_text().splitlines()
    assert rows[0] == (
        "run_start,repo,status,total_s,network_s,fetch_new_s,fetch_old_s,push_s,api_s,git_other_s"
    )
    assert len(rows) == 2
    assert ",grp/repo,problem," in rows[1]

    run_world(world)
    assert len(csv_path.read_text().splitlines()) == 3


def test_skipped_repo_counts_as_ok(world):
    assert run_world(world, precheck=lambda url: False) == 0
    assert "skipped-api" in (world.cfg.log_file.parent / "performance.csv").read_text()
    assert "1 without problems, 1 skipped by API precheck" in world.cfg.log_file.read_text()


def test_perf_csv_can_be_disabled(world):
    world.cfg.perf_file = ""
    run_world(world)
    assert not (world.cfg.log_file.parent / "performance.csv").exists()


def test_missing_root_exits_with_2(world, tmp_path):
    world.cfg.root = tmp_path / "missing"
    assert run_world(world) == 2


def test_cli_dry_run_end_to_end(world, tmp_path, monkeypatch):
    # Route the derived old URL to the local bare repo so the real CLI path can run.
    monkeypatch.setattr("gitlab_sync.sync.repo.derive_old_url", lambda *args: str(world.old))
    config = tmp_path / "config.yaml"
    config.write_text(
        f"root: {world.root}\nnew_host: gitlab.example.com\n"
        f"old_host: git.example.com\nlog_file: {world.cfg.log_file}\n"
    )
    before = git(world.new, "for-each-ref")
    assert main(["-c", str(config), "--dry-run"]) == 1
    assert git(world.new, "for-each-ref") == before
    assert "mode: dry run, no push" in world.cfg.log_file.read_text()


def test_cli_reports_config_errors(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("root: x\n")
    assert main(["-c", str(config)]) == 2
    assert "missing required keys" in capsys.readouterr().out


def test_audit_without_tokens_exits_with_2(world, monkeypatch):
    monkeypatch.delenv("GITLAB_NEW_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_OLD_TOKEN", raising=False)
    world.cfg.audit.permissions = True
    before = git(world.new, "for-each-ref")
    assert run_world(world) == 2
    assert git(world.new, "for-each-ref") == before
    assert "the audit needs API tokens" in world.cfg.log_file.read_text()
