from pathlib import Path

import pytest

from gitlab_sync.config import Config, ConfigError, load_config


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_minimal_config_uses_defaults_and_resolves_paths(tmp_path):
    cfg = load_config(write(tmp_path, "root: work\nnew_host: n\nold_host: o\n"))
    assert cfg.root == tmp_path / "work"
    assert cfg.log_file == tmp_path / "gitlab_sync.log"
    assert cfg.old_remote == "git-old"
    assert cfg.api_precheck.enabled is False
    assert cfg.jenkins_rollback.branches == ["main"]


def test_nested_sections(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            (
                "root: /abs\nnew_host: n\nold_host: o\nlog_file: logs/run.log\n"
                "api_precheck: {enabled: true, timeout: 5}\n"
                "jenkins_rollback: {enabled: true, committers: ['jenkins*']}\n"
            ),
        )
    )
    assert cfg.root == Path("/abs")
    assert cfg.log_file == tmp_path / "logs" / "run.log"
    assert cfg.api_precheck.timeout == 5
    assert cfg.jenkins_rollback.committers == ["jenkins*"]


def test_missing_required_keys(tmp_path):
    with pytest.raises(ConfigError, match="new_host, old_host"):
        load_config(write(tmp_path, "root: x\n"))


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("root: x\nnew_host: n\nold_host: o\nconflict_file: typo\n", "top level: conflict_file"),
        ("root: x\nnew_host: n\nold_host: o\napi_precheck: {enable: true}\n", "api_precheck: enable"),
    ],
)
def test_unknown_keys_are_rejected(tmp_path, text, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, text))


def test_empty_perf_file_disables_csv(tmp_path):
    cfg = load_config(write(tmp_path, "root: x\nnew_host: n\nold_host: o\nperf_file:\n"))
    assert cfg.output_path(cfg.perf_file) is None


def test_output_path_is_relative_to_log_dir(tmp_path):
    cfg = Config(root=tmp_path, new_host="n", old_host="o", log_file=tmp_path / "logs" / "x.log")
    assert cfg.output_path("a.txt") == tmp_path / "logs" / "a.txt"
    assert cfg.output_path("/abs/a.txt") == Path("/abs/a.txt")
