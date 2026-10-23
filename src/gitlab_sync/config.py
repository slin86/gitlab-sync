"""Configuration model and YAML loader."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

REQUIRED_KEYS = ("root", "new_host", "old_host")


class ConfigError(Exception):
    """Raised when the configuration file is missing values or contains unknown keys."""


@dataclass
class ApiPrecheckConfig:
    enabled: bool = False
    new_token_env: str = "GITLAB_NEW_TOKEN"
    old_token_env: str = "GITLAB_OLD_TOKEN"
    scheme: str = "https"
    timeout: int = 15


@dataclass
class JenkinsRollbackConfig:
    enabled: bool = False
    committers: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=lambda: ["main"])
    max_commits: int = 50
    instructions_file: str = "jenkins_rollback.md"


@dataclass
class AuditConfig:
    """Read only API checks whose results go to the findings document.

    Uses the token and connection settings of api_precheck; read_api is enough on both instances.
    """

    repositories: bool = False
    permissions: bool = False
    project_objects: bool = False
    namespaces: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.repositories or self.permissions or self.project_objects


@dataclass
class Config:
    root: Path
    new_host: str
    old_host: str
    new_remote: str = "origin"
    old_remote: str = "git-old"
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    exclude_branches: list[str] = field(default_factory=list)
    overwrite_tags: bool = False
    restore_branch: bool = True
    dry_run: bool = False
    git_timeout: int = 600
    log_file: Path = Path("gitlab_sync.log")
    conflicts_file: str = "branch_conflicts.txt"
    protected_branches_file: str = "protected_branches.md"
    protected_branches_page: str = "/-/settings/repository#js-protected-branches-settings"
    findings_file: str = "findings.md"
    perf_file: str = "performance.csv"
    perf_top: int = 10
    slow_command_seconds: float = 30.0
    api_precheck: ApiPrecheckConfig = field(default_factory=ApiPrecheckConfig)
    jenkins_rollback: JenkinsRollbackConfig = field(default_factory=JenkinsRollbackConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)

    def output_path(self, name: str) -> Path | None:
        """Resolve an output file name relative to the log directory. An empty name disables it."""
        if not name:
            return None
        path = Path(name)
        return path if path.is_absolute() else self.log_file.parent / path

    def relative_repo(self, repo: Path) -> str:
        """Repo path relative to the configured root, used in reports."""
        try:
            return repo.relative_to(self.root).as_posix()
        except ValueError:
            return str(repo)


def load_config(path: Path) -> Config:
    """Read and validate a YAML config. Relative paths are resolved against the config file."""
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    missing = [key for key in REQUIRED_KEYS if not raw.get(key)]
    if missing:
        raise ConfigError(f"{path}: missing required keys: {', '.join(missing)}")

    base = path.resolve().parent
    data = dict(raw)
    data["root"] = _resolve_path(data["root"], base)
    if "log_file" in data:
        data["log_file"] = _resolve_path(data["log_file"], base)
    else:
        data["log_file"] = base / Config.log_file
    if data.get("perf_file") is None and "perf_file" in data:
        data["perf_file"] = ""
    data["api_precheck"] = _build(ApiPrecheckConfig, data.get("api_precheck"), "api_precheck")
    data["audit"] = _build(AuditConfig, data.get("audit"), "audit")
    data["jenkins_rollback"] = _build(JenkinsRollbackConfig, data.get("jenkins_rollback"), "jenkins_rollback")
    return _build(Config, data, "top level")


def _resolve_path(value: Any, base: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return path if path.is_absolute() else base / path


def _build(cls: type, data: dict | None, section: str):
    data = data or {}
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown keys in {section}: {', '.join(unknown)}")
    return cls(**data)
