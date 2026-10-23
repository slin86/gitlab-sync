"""Check up front whether the token user may push to the branches that the sync would push."""

from __future__ import annotations

import fnmatch
from typing import Any

from gitlab_sync.results import Finding, FindingKind

DEVELOPER = 30
OWNER = 50
ACCESS_NAMES = {0: "no access", 10: "guest", 20: "reporter", 30: "developer", 40: "maintainer", 50: "owner"}


def access_level(project: dict[str, Any], user: dict[str, Any]) -> int:
    """Highest of project and group access. Instance admins are treated like owners."""
    permissions = project.get("permissions") or {}
    level = max(
        (permissions.get(scope) or {}).get("access_level") or 0
        for scope in ("project_access", "group_access")
    )
    if user.get("is_admin"):
        level = max(level, OWNER)
    return level


def push_allowed(rule: dict[str, Any], level: int, user_id: int) -> bool | None:
    """True or False, or None when only group based entries could allow it."""
    entries = rule.get("push_access_levels") or []
    for entry in entries:
        if entry.get("user_id") == user_id:
            return True
        is_role_entry = not any(entry.get(k) for k in ("user_id", "group_id", "deploy_key_id"))
        required = entry.get("access_level") or 0
        if is_role_entry and required and level >= required:
            return True
    if any(entry.get("group_id") for entry in entries):
        return None
    return False


def check_push_permissions(
    project_path: str,
    project: dict[str, Any],
    user: dict[str, Any],
    protected: list[dict[str, Any]],
    branches: list[str],
    settings_link: str,
) -> list[Finding]:
    """Findings for protected branches that exist on the old instance but cannot be pushed to."""
    level = access_level(project, user)
    username = user.get("username", "?")
    role = ACCESS_NAMES.get(level, f"level {level}")
    if level < DEVELOPER:
        message = f"user {username} has {role} access and cannot push to this project at all"
        return [Finding(FindingKind.PUSH_PERMISSION, project_path, message, settings_link)]

    findings = []
    for rule in protected:
        matching = [b for b in branches if fnmatch.fnmatchcase(b, rule["name"])]
        if not matching:
            continue
        allowed = push_allowed(rule, level, user.get("id"))
        if allowed:
            continue
        affected = ", ".join(matching)
        if allowed is None:
            message = (
                f"protected branch {rule['name']} only allows pushes for groups; "
                f"whether user {username} is a member was not checked (affects {affected})"
            )
        else:
            message = (
                f"user {username} ({role}) may not push to protected branch {rule['name']} "
                f"(affects {affected})"
            )
        findings.append(Finding(FindingKind.PUSH_PERMISSION, project_path, message, settings_link))
    return findings
