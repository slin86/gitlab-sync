"""Differences of pipeline schedules, feature flags and environments between both instances.

Variable values are never written to the findings, only their keys, because they can hold secrets.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from gitlab_sync.results import Finding, FindingKind

ScheduleKey = tuple[str, str, int]


def _field_diffs(old: dict[str, Any], new: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    diffs = []
    for name in fields:
        if (old.get(name) or None) != (new.get(name) or None):
            diffs.append(f"{name} differs (old `{old.get(name)}`, new `{new.get(name)}`)")
    return diffs


def _only_on(side: str) -> str:
    return f"only on the {side} instance"


# Pipeline schedules


def normalize_ref(ref: str) -> str:
    return ref.removeprefix("refs/heads/")


def index_schedules(schedules: list[dict[str, Any]]) -> dict[ScheduleKey, dict[str, Any]]:
    """Key schedules by description and branch. Duplicates get a running number."""
    seen: Counter[tuple[str, str]] = Counter()
    indexed = {}
    for schedule in schedules:
        base = (schedule.get("description") or "", normalize_ref(schedule["ref"]))
        indexed[(*base, seen[base])] = schedule
        seen[base] += 1
    return indexed


def schedule_label(key: ScheduleKey) -> str:
    description, ref, number = key
    suffix = f" #{number + 1}" if number else ""
    return f"Schedule '{description}' on {ref}{suffix}"


def _variable_diffs(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    def values(schedule: dict[str, Any]) -> dict[str, tuple[str, str]]:
        return {
            v["key"]: (v.get("value"), v.get("variable_type", "env_var"))
            for v in schedule.get("variables") or []
        }

    old_vars, new_vars = values(old), values(new)
    parts = []
    missing = sorted(set(old_vars) - set(new_vars))
    extra = sorted(set(new_vars) - set(old_vars))
    changed = sorted(k for k in set(old_vars) & set(new_vars) if old_vars[k] != new_vars[k])
    if missing:
        parts.append(f"variables missing on the new instance: {', '.join(missing)}")
    if extra:
        parts.append(f"variables only on the new instance: {', '.join(extra)}")
    if changed:
        parts.append(f"variables with different values: {', '.join(changed)}")
    return parts


def diff_schedules(
    project: str, old: list[dict[str, Any]], new: list[dict[str, Any]], link: str
) -> list[Finding]:
    old_index, new_index = index_schedules(old), index_schedules(new)
    findings = []
    for key in sorted(set(old_index) | set(new_index)):
        label = schedule_label(key)
        if key not in new_index:
            state = "active" if old_index[key].get("active") else "inactive"
            message = f"{label}: {_only_on('old')} ({state})"
        elif key not in old_index:
            message = f"{label}: {_only_on('new')}"
        else:
            a, b = old_index[key], new_index[key]
            diffs = _field_diffs(a, b, ("cron", "cron_timezone", "active")) + _variable_diffs(a, b)
            if not diffs:
                continue
            message = f"{label}: {'; '.join(diffs)}"
        findings.append(Finding(FindingKind.SCHEDULE, project, message, link))
    return findings


# Feature flags


def describe_strategies(flag: dict[str, Any]) -> str:
    """Readable and order independent form, e.g. 'gradualRolloutUserId(percentage=50) on production'."""
    parts = []
    for strategy in flag.get("strategies") or []:
        params = ", ".join(f"{k}={v}" for k, v in sorted((strategy.get("parameters") or {}).items()))
        scopes = ", ".join(sorted(s["environment_scope"] for s in strategy.get("scopes") or [])) or "no scope"
        parts.append(f"{strategy['name']}({params}) on {scopes}")
    return " and ".join(sorted(parts)) or "no strategies"


def _strategy_key(flag: dict[str, Any]) -> list[tuple]:
    return sorted(
        (
            s["name"],
            json.dumps(s.get("parameters") or {}, sort_keys=True),
            tuple(sorted(scope["environment_scope"] for scope in s.get("scopes") or [])),
        )
        for s in flag.get("strategies") or []
    )


def diff_feature_flags(
    project: str, old: list[dict[str, Any]], new: list[dict[str, Any]], link: str
) -> list[Finding]:
    old_by_name = {f["name"]: f for f in old}
    new_by_name = {f["name"]: f for f in new}
    findings = []
    for name in sorted(set(old_by_name) | set(new_by_name)):
        label = f"Feature flag {name}"
        if name not in new_by_name:
            state = "active" if old_by_name[name].get("active") else "inactive"
            message = f"{label}: {_only_on('old')} ({state})"
        elif name not in old_by_name:
            message = f"{label}: {_only_on('new')}"
        else:
            a, b = old_by_name[name], new_by_name[name]
            diffs = _field_diffs(a, b, ("active", "description"))
            if _strategy_key(a) != _strategy_key(b):
                diffs.append(
                    f"strategies differ (old {describe_strategies(a)}; new {describe_strategies(b)})"
                )
            if not diffs:
                continue
            message = f"{label}: {'; '.join(diffs)}"
        findings.append(Finding(FindingKind.FEATURE_FLAG, project, message, link))
    return findings


# Environments


def diff_environments(
    project: str, old: list[dict[str, Any]], new: list[dict[str, Any]], link: str
) -> list[Finding]:
    old_by_name = {e["name"]: e for e in old}
    new_by_name = {e["name"]: e for e in new}
    findings = []
    for name in sorted(set(old_by_name) | set(new_by_name)):
        label = f"Environment {name}"
        if name not in new_by_name:
            message = f"{label}: {_only_on('old')}"
        elif name not in old_by_name:
            message = f"{label}: {_only_on('new')}"
        else:
            diffs = _field_diffs(old_by_name[name], new_by_name[name], ("external_url", "tier", "state"))
            if not diffs:
                continue
            message = f"{label}: {'; '.join(diffs)}"
        findings.append(Finding(FindingKind.ENVIRONMENT, project, message, link))
    return findings
