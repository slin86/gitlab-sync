"""Command line entry point."""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

from loguru import logger

from gitlab_sync import __doc__ as package_doc
from gitlab_sync.config import ConfigError, load_config
from gitlab_sync.runner import run

EXIT_CONFIG_ERROR = 2


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gitlab_sync", description=package_doc)
    parser.add_argument(
        "-c",
        "--config",
        default="config.yaml",
        type=Path,
        help="path to the YAML config (default: config.yaml)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="do everything except pushing to the new instance"
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="GLOB",
        help="only sync repos whose path relative to root matches, can be repeated",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # The run log adds its own file and console handlers.
    logger.remove()
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as exc:
        print(f"Config error: {exc}")
        return EXIT_CONFIG_ERROR
    if args.dry_run:
        cfg = dataclasses.replace(cfg, dry_run=True)
    return run(cfg, args.only)
