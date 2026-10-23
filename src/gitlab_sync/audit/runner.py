"""Runs the audit checks and records their findings."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from gitlab_sync.audit.api import AuditApi
from gitlab_sync.audit.objects import diff_environments, diff_feature_flags, diff_schedules
from gitlab_sync.audit.permissions import check_push_permissions
from gitlab_sync.audit.repositories import compare_repositories
from gitlab_sync.config import Config
from gitlab_sync.gitlab_api import ApiError, GitLabClient
from gitlab_sync.log import TreeLog
from gitlab_sync.perf import PerfTracker
from gitlab_sync.results import Finding, RunResult
from gitlab_sync.urls import UrlError, project_path


class MissingTokenError(RuntimeError):
    """An audit check is enabled but the API tokens are not set."""


class Audit:
    """Read only comparison of both instances. Nothing is changed on either side."""

    def __init__(self, cfg: Config, old: AuditApi, new: AuditApi):
        self.cfg = cfg
        self.old = old
        self.new = new

    def check_repositories(self, log: TreeLog, result: RunResult) -> None:
        """Projects missing on the new instance and archive status differences."""
        if not self.cfg.audit.repositories:
            return
        log.info(0, "Comparing the project lists of both instances")
        try:
            findings = compare_repositories(
                self.old, self.new, self.cfg.audit.namespaces, self.cfg.api_precheck.scheme
            )
        except ApiError as exc:
            _fail(log, result, 1, "Project list comparison", exc)
            return
        _record(log, result, 1, findings)
        log.info(1, f"{len(findings)} findings")

    def check_project(self, repo: Path, new_url: str, log: TreeLog, depth: int, result: RunResult) -> None:
        """Push permissions and project object differences of one repo."""
        try:
            project = project_path(new_url)
        except UrlError as exc:
            _fail(log, result, depth, f"Audit of {repo}", exc)
            return
        checks = []
        if self.cfg.audit.permissions:
            checks.append(("Push permission check", self._permissions))
        if self.cfg.audit.project_objects:
            checks.append(("Project object comparison", self._objects))
        for label, check in checks:
            try:
                findings = check(project)
            except ApiError as exc:
                _fail(log, result, depth, f"{label} of {project}", exc)
                continue
            _record(log, result, depth, findings)
            log.info(depth, f"{label}: {len(findings)} findings")

    def _permissions(self, project: str) -> list[Finding]:
        branches = [
            b
            for b in self.old.branch_names(project)
            if not any(fnmatch.fnmatchcase(b, p) for p in self.cfg.exclude_branches)
        ]
        return check_push_permissions(
            project,
            self.new.protected_branches(project),
            branches,
            self._new_link(project, self.cfg.protected_branches_page),
        )

    def _objects(self, project: str) -> list[Finding]:
        findings = diff_environments(
            project,
            self.old.environments(project),
            self.new.environments(project),
            self._new_link(project, "/-/environments"),
        )
        findings += diff_schedules(
            project,
            self.old.schedules(project),
            self.new.schedules(project),
            self._new_link(project, "/-/pipeline_schedules"),
        )
        findings += diff_feature_flags(
            project,
            self.old.feature_flags(project),
            self.new.feature_flags(project),
            self._new_link(project, "/-/feature_flags"),
        )
        return findings

    def _new_link(self, project: str, page: str) -> str:
        return f"{self.cfg.api_precheck.scheme}://{self.cfg.new_host}/{project}{page}"


def build_audit(cfg: Config, perf: PerfTracker) -> Audit | None:
    """Return the audit, or None if every check is disabled. Raises MissingTokenError without tokens."""
    if not cfg.audit.enabled:
        return None
    api = cfg.api_precheck
    old_token = os.environ.get(api.old_token_env)
    new_token = os.environ.get(api.new_token_env)
    if not old_token or not new_token:
        raise MissingTokenError(
            f"the audit needs API tokens, set {api.old_token_env} and {api.new_token_env}"
        )
    return Audit(
        cfg,
        AuditApi(GitLabClient(cfg.old_host, old_token, api.scheme, api.timeout, perf)),
        AuditApi(GitLabClient(cfg.new_host, new_token, api.scheme, api.timeout, perf)),
    )


def _record(log: TreeLog, result: RunResult, depth: int, findings: list[Finding]) -> None:
    for finding in findings:
        log.warning(depth + 1, f"{finding.subject}: {finding.message}")
    result.findings.extend(findings)


def _fail(log: TreeLog, result: RunResult, depth: int, label: str, exc: Exception) -> None:
    message = f"{label} failed: {exc}"
    log.error(depth, message)
    result.errors.append(message)
