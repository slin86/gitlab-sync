"""Compare the project lists of both instances: missing projects and archive status."""

from __future__ import annotations

from gitlab_sync.audit.api import AuditApi
from gitlab_sync.results import Finding, FindingKind


def in_namespaces(path: str, namespaces: list[str]) -> bool:
    if not namespaces:
        return True
    prefixes = [ns.rstrip("/") for ns in namespaces]
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def compare_repositories(old: AuditApi, new: AuditApi, namespaces: list[str], scheme: str) -> list[Finding]:
    old_archived, old_active = old.project_paths(archived=True), old.project_paths(archived=False)
    new_archived, new_active = new.project_paths(archived=True), new.project_paths(archived=False)
    findings = []
    for path in sorted(old_archived | old_active):
        if not in_namespaces(path, namespaces):
            continue
        old_state = "archived" if path in old_archived else "active"
        if path not in new_archived and path not in new_active:
            findings.append(
                Finding(
                    FindingKind.MISSING_REPO,
                    path,
                    f"exists on the old instance ({old_state}) but not on the new one",
                    f"{scheme}://{old.host}/{path}",
                )
            )
            continue
        new_state = "archived" if path in new_archived else "active"
        if old_state != new_state:
            findings.append(
                Finding(
                    FindingKind.ARCHIVE_STATUS,
                    path,
                    f"{old_state} on the old instance, {new_state} on the new one",
                    f"{scheme}://{new.host}/{path}/edit",
                )
            )
    return findings
