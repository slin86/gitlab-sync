"""Check up front whether the protected branch rules let Maintainers push.

The check only reads the rules of the protected branches. It does not look at the membership of the
token user, because inherited group memberships are not reliably visible on the project.
"""

from __future__ import annotations

import fnmatch
from typing import Any

from gitlab_sync.results import Finding, FindingKind

MAINTAINER = 40
LEVEL_NAMES = {0: "No one", 30: "Developers and Maintainers", 40: "Maintainers", 60: "Admins"}


def maintainers_may_push(rule: dict[str, Any]) -> bool:
    """True if a role entry of the rule allows Maintainers. GitLab grants every role above the level."""
    for entry in rule.get("push_access_levels") or []:
        is_role_entry = not any(entry.get(key) for key in ("user_id", "group_id", "deploy_key_id"))
        level = entry.get("access_level") or 0
        if is_role_entry and 0 < level <= MAINTAINER:
            return True
    return False


def describe_push_access(rule: dict[str, Any]) -> str:
    """Who may push according to the rule, using the descriptions GitLab returns."""
    names = []
    for entry in rule.get("push_access_levels") or []:
        name = entry.get("access_level_description") or LEVEL_NAMES.get(entry.get("access_level") or 0)
        names.append(name or f"level {entry.get('access_level')}")
    return ", ".join(sorted(set(names))) or "No one"


def check_push_permissions(
    project_path: str, protected: list[dict[str, Any]], branches: list[str], settings_link: str
) -> list[Finding]:
    """One finding per protected branch rule that blocks Maintainers on branches the sync pushes."""
    findings = []
    for rule in protected:
        matching = [b for b in branches if fnmatch.fnmatchcase(b, rule["name"])]
        if not matching or maintainers_may_push(rule):
            continue
        message = (
            f"protected branch {rule['name']} does not allow Maintainers to push "
            f"(allowed to push: {describe_push_access(rule)}; affects {', '.join(matching)})"
        )
        findings.append(Finding(FindingKind.PUSH_PERMISSION, project_path, message, settings_link))
    return findings
