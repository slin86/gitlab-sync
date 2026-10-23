"""Indented run log on top of loguru, written to a file and to the console."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {message}"
CONSOLE_COLORS = {"ERROR": "red", "CRITICAL": "red", "WARNING": "yellow"}


def _console_format(record) -> str:
    color = CONSOLE_COLORS.get(record["level"].name)
    return f"<{color}>{{message}}</{color}>\n" if color else "{message}\n"


def _resolve_colorize(colorize: bool | None) -> bool | None:
    """An explicit value wins. Otherwise NO_COLOR and FORCE_COLOR decide, then loguru checks for a TTY."""
    if colorize is not None:
        return colorize
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return None


class TreeLog:
    """Writes the run log as an indented tree, one level per folder, repo and step.

    The file contains timestamp and level. The console shows the indented message only, errors in
    red and warnings in yellow, and no colors when output is piped.
    """

    def __init__(self, path: Path, console: bool = True, colorize: bool | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handler_ids = [logger.add(path, format=FILE_FORMAT, colorize=False, encoding="utf-8")]
        if console:
            self._handler_ids.append(
                logger.add(sys.stdout, format=_console_format, colorize=_resolve_colorize(colorize))
            )

    def info(self, depth: int, message: str) -> None:
        self._emit("INFO", depth, message)

    def warning(self, depth: int, message: str) -> None:
        self._emit("WARNING", depth, f"[WARN] {message}")

    def conflict(self, depth: int, message: str) -> None:
        self._emit("WARNING", depth, f"[CONFLICT] {message}")

    def error(self, depth: int, message: str) -> None:
        self._emit("ERROR", depth, f"[ERROR] {message}")

    def close(self) -> None:
        for handler_id in self._handler_ids:
            logger.remove(handler_id)
        self._handler_ids = []

    def __enter__(self) -> TreeLog:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @staticmethod
    def _emit(level: str, depth: int, message: str) -> None:
        indent = "    " * depth + ("-> " if depth else "")
        for line in message.splitlines() or [""]:
            logger.log(level, f"{indent}{line}")
