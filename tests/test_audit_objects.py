from gitlab_sync.audit.objects import (
    describe_strategies,
    diff_environments,
    diff_feature_flags,
    diff_schedules,
)
from gitlab_sync.results import FindingKind

LINK = "https://gitlab.example.com/grp/repo/-/x"


def schedule(description="Nightly", ref="main", cron="0 2 * * *", active=True, variables=(), **extra):
    return {
        "description": description,
        "ref": ref,
        "cron": cron,
        "active": active,
        "cron_timezone": "UTC",
        "variables": list(variables),
        **extra,
    }


def var(key, value, variable_type="env_var"):
    return {"key": key, "value": value, "variable_type": variable_type}


def messages(findings):
    return [f.message for f in findings]


def test_schedules_only_on_one_side():
    findings = diff_schedules("grp/repo", [schedule(active=False)], [schedule("Other")], LINK)
    assert messages(findings) == [
        "Schedule 'Nightly' on main: only on the old instance (inactive)",
        "Schedule 'Other' on main: only on the new instance",
    ]
    assert {f.kind for f in findings} == {FindingKind.SCHEDULE}
    assert findings[0].link == LINK


def test_schedule_field_and_variable_differences_without_values():
    old = schedule(cron="0 3 * * *", variables=[var("A", "secret1"), var("B", "x"), var("C", "same")])
    new = schedule(active=False, variables=[var("A", "secret2"), var("C", "same"), var("D", "y")])
    (finding,) = diff_schedules("grp/repo", [old], [new], LINK)
    assert finding.message == (
        "Schedule 'Nightly' on main: cron differs (old `0 3 * * *`, new `0 2 * * *`); "
        "active differs (old `True`, new `False`); variables missing on the new instance: B; "
        "variables only on the new instance: D; variables with different values: A"
    )
    assert "secret" not in finding.message


def test_identical_schedules_with_full_and_short_refs_have_no_findings():
    assert diff_schedules("p", [schedule(ref="refs/heads/main")], [schedule(ref="main")], LINK) == []


def test_duplicate_schedules_are_matched_in_order():
    findings = diff_schedules("p", [schedule(), schedule()], [schedule()], LINK)
    assert messages(findings) == ["Schedule 'Nightly' on main #2: only on the old instance (active)"]


def flag(name="checkout", active=True, strategies=None, description=""):
    if strategies is None:
        strategies = [{"name": "default", "parameters": {}, "scopes": [{"environment_scope": "*"}]}]
    return {"name": name, "active": active, "description": description, "strategies": strategies}


def rollout(percentage, *scopes):
    return {
        "id": 1,
        "name": "gradualRolloutUserId",
        "parameters": {"percentage": percentage, "groupId": "default"},
        "scopes": [{"id": 9, "environment_scope": s} for s in scopes],
    }


def test_feature_flag_differences():
    old = [flag(active=False, strategies=[rollout("50", "production")]), flag("gone")]
    new = [flag(description="new text", strategies=[rollout("20", "production")]), flag("extra")]
    findings = diff_feature_flags("grp/repo", old, new, LINK)
    assert messages(findings) == [
        "Feature flag checkout: active differs (old `False`, new `True`); description differs "
        "(old ``, new `new text`); strategies differ (old gradualRolloutUserId(groupId=default, "
        "percentage=50) on production; new gradualRolloutUserId(groupId=default, percentage=20) on "
        "production)",
        "Feature flag extra: only on the new instance",
        "Feature flag gone: only on the old instance (active)",
    ]


def test_feature_flags_that_only_differ_in_ids_and_order_have_no_findings():
    old = flag(strategies=[rollout("10", "b", "a"), rollout("20", "c")])
    new = flag(strategies=[rollout("20", "c"), rollout("10", "a", "b")])
    assert diff_feature_flags("p", [old], [new], LINK) == []


def test_describe_strategies():
    assert describe_strategies(flag(strategies=[])) == "no strategies"
    assert describe_strategies(flag()) == "default() on *"


def test_environment_differences():
    old = [
        {"name": "production", "external_url": "https://shop", "tier": "production", "state": "available"},
        {"name": "staging", "external_url": None},
    ]
    new = [
        {"name": "production", "external_url": "https://new", "tier": "production", "state": "stopped"},
        {"name": "review/x"},
    ]
    findings = diff_environments("grp/repo", old, new, LINK)
    assert messages(findings) == [
        "Environment production: external_url differs (old `https://shop`, new `https://new`); "
        "state differs (old `available`, new `stopped`)",
        "Environment review/x: only on the new instance",
        "Environment staging: only on the old instance",
    ]
    assert {f.kind for f in findings} == {FindingKind.ENVIRONMENT}
