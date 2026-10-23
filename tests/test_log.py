import contextlib
import io

import pytest

from gitlab_sync.log import TreeLog


def console_output(tmp_path, colorize):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), TreeLog(tmp_path / "x.log", colorize=colorize) as log:
        log.error(1, "boom <b> {x}")
        log.warning(2, "hmm")
        log.conflict(2, "merge")
        log.info(0, "all good <tag>")
    return buffer.getvalue()


def test_file_has_timestamp_level_and_indentation(tmp_path):
    with TreeLog(tmp_path / "x.log", console=False) as log:
        log.error(1, "boom")
        log.warning(2, "hmm")
        log.info(0, "ok <tag> {braces}")
    lines = (tmp_path / "x.log").read_text().splitlines()
    assert lines[0].endswith("| ERROR   |     -> [ERROR] boom")
    assert "| WARNING |         -> [WARN] hmm" in lines[1]
    assert lines[2].endswith("| INFO    | ok <tag> {braces}")


def test_console_is_colored_by_level(tmp_path):
    lines = console_output(tmp_path, True).splitlines()
    assert "\x1b[31m" in lines[0]
    assert "[ERROR] boom <b> {x}" in lines[0]
    assert "\x1b[33m" in lines[1]
    assert "\x1b[33m" in lines[2]
    assert lines[3] == "all good <tag>"


def test_console_without_color_shows_only_the_message(tmp_path):
    output = console_output(tmp_path, False)
    assert "\x1b[" not in output
    assert output.splitlines()[0] == "    -> [ERROR] boom <b> {x}"


@pytest.mark.parametrize(
    ("env", "colored"), [({}, False), ({"NO_COLOR": "1"}, False), ({"FORCE_COLOR": "1"}, True)]
)
def test_color_detection(tmp_path, monkeypatch, env, colored):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert ("\x1b[" in console_output(tmp_path, None)) is colored


def test_log_file_never_contains_color_codes(tmp_path):
    console_output(tmp_path, True)
    assert "\x1b[" not in (tmp_path / "x.log").read_text()
