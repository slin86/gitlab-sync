"""The findings document: one Markdown file with everything a developer should look at.

It is replaced on every run, also in a dry run, and lists each category even when it is empty, so
the reader can tell a clean result from a check that did not run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gitlab_sync.config import Config
from gitlab_sync.reports import protected_branches_url
from gitlab_sync.results import (
    BranchConflict,
    Finding,
    FindingKind,
    ProtectedPush,
    RollbackInstruction,
    RunResult,
)
from gitlab_sync.urls import UrlError

# Finding kinds produced by the audit, with the config switch that enables them.
AUDIT_SWITCHES = {
    FindingKind.MISSING_REPO: "repositories",
    FindingKind.ARCHIVE_STATUS: "repositories",
    FindingKind.PUSH_PERMISSION: "permissions",
    FindingKind.SCHEDULE: "project_objects",
    FindingKind.FEATURE_FLAG: "project_objects",
    FindingKind.ENVIRONMENT: "project_objects",
}
LEADING_KINDS = (FindingKind.MISSING_REPO, FindingKind.ARCHIVE_STATUS, FindingKind.PUSH_PERMISSION)
TRAILING_KINDS = (
    FindingKind.TAG,
    FindingKind.LOCAL_BRANCH,
    FindingKind.SCHEDULE,
    FindingKind.FEATURE_FLAG,
    FindingKind.ENVIRONMENT,
)


@dataclass(frozen=True)
class Section:
    title: str
    lines: list[str]
    note: str = ""


def write_findings(cfg: Config, result: RunResult, started: datetime) -> tuple[Path, int] | None:
    """Write the document and return its path and the number of findings, or None if disabled."""
    path = cfg.output_path(cfg.findings_file)
    if path is None:
        return None
    sections = build_sections(cfg, result)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(cfg, result, sections, started), encoding="utf-8")
    return path, sum(len(s.lines) for s in sections)


def build_sections(cfg: Config, result: RunResult) -> list[Section]:
    by_kind: dict[FindingKind, list[Finding]] = {kind: [] for kind in FindingKind}
    for finding in result.findings:
        by_kind[finding.kind].append(finding)

    sections = [_kind_section(cfg, kind, by_kind[kind]) for kind in LEADING_KINDS]
    sections += [
        Section("Merge conflicts", [_conflict_line(cfg, c) for c in result.branch_conflicts]),
        Section(
            "Rejected pushes to protected branches",
            [_rejected_push_line(cfg, p) for p in result.protected_pushes],
            "No pushes in a dry run, see push permission problems instead." if cfg.dry_run else "",
        ),
        Section("Jenkins rollbacks needed", [_rollback_line(cfg, r) for r in result.rollbacks]),
    ]
    sections += [_kind_section(cfg, kind, by_kind[kind]) for kind in TRAILING_KINDS]
    sections += [
        Section("Errors", [f"* {e}" for e in result.errors]),
        Section("Other warnings", [f"* {w}" for w in result.warnings]),
    ]
    return sections


def render(cfg: Config, result: RunResult, sections: list[Section], started: datetime) -> str:
    mode = "dry run, nothing was pushed" if cfg.dry_run else "live"
    total = sum(len(s.lines) for s in sections)
    lines = [
        "# gitlab_sync findings",
        "",
        f"Run started {started:%Y-%m-%d %H:%M:%S}, mode: {mode}. "
        f"{result.repos} repos checked, {total} findings.",
        "",
        "## Overview",
        "",
    ]
    lines += [f"* {s.title}: {s.note or len(s.lines)}" for s in sections]
    for section in sections:
        lines += ["", f"## {section.title} ({len(section.lines)})", ""]
        lines += section.lines or [section.note or "None."]
    return "\n".join(lines) + "\n"


def _kind_section(cfg: Config, kind: FindingKind, findings: list[Finding]) -> Section:
    lines = [_finding_line(f) for f in sorted(findings, key=lambda f: (f.subject, f.message))]
    switch = AUDIT_SWITCHES.get(kind)
    note = f"Not checked, `audit.{switch}` is disabled." if switch and not getattr(cfg.audit, switch) else ""
    return Section(kind.value, lines, note)


def _finding_line(finding: Finding) -> str:
    link = f" ([open]({finding.link}))" if finding.link else ""
    return f"* **{finding.subject}**: {finding.message}{link}"


def _conflict_line(cfg: Config, conflict: BranchConflict) -> str:
    old, new = cfg.old_remote, cfg.new_remote
    line = (
        f"* **{cfg.relative_repo(conflict.repo)}**: branch {conflict.branch}, {conflict.commits} commits "
        f"from {old} cannot be merged ({old} {conflict.old_sha[:10]}, {new} {conflict.new_sha[:10]})"
    )
    return line + (", caused by Jenkins commits" if conflict.jenkins_rollback else "")


def _rejected_push_line(cfg: Config, push: ProtectedPush) -> str:
    try:
        link = f" ([settings]({protected_branches_url(cfg, push.new_url)}))"
    except UrlError:
        link = ""
    return f"* **{cfg.relative_repo(push.repo)}**: push to branch {push.branch} was rejected{link}"


def _rollback_line(cfg: Config, rollback: RollbackInstruction) -> str:
    return (
        f"* **{cfg.relative_repo(rollback.repo)}**: branch {rollback.branch}, {len(rollback.commits)} "
        f"Jenkins commits block the merge, steps in {cfg.jenkins_rollback.instructions_file}"
    )
