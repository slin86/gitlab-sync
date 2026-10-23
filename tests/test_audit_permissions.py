import pytest

from gitlab_sync.audit.permissions import access_level, check_push_permissions, push_allowed
from gitlab_sync.results import FindingKind

USER = {"id": 7, "username": "dev", "is_admin": False}
LINK = "https://gitlab.example.com/grp/repo/-/settings/repository"


def project(project_level=None, group_level=None):
    return {
        "permissions": {
            "project_access": {"access_level": project_level} if project_level else None,
            "group_access": {"access_level": group_level} if group_level else None,
        }
    }


def rule(name, *entries):
    return {"name": name, "push_access_levels": list(entries)}


def test_access_level_uses_the_highest_and_treats_admins_as_owner():
    assert access_level(project(30, 40), USER) == 40
    assert access_level(project(), USER) == 0
    assert access_level(project(), {**USER, "is_admin": True}) == 50


@pytest.mark.parametrize(
    ("entries", "level", "expected"),
    [
        ([{"access_level": 40}], 40, True),
        ([{"access_level": 40}], 30, False),
        ([{"access_level": 0}], 50, False),
        ([{"access_level": 40, "user_id": 7}], 30, True),
        ([{"access_level": 40, "group_id": 3}], 40, None),
        ([], 50, False),
    ],
)
def test_push_allowed(entries, level, expected):
    assert push_allowed(rule("main", *entries), level, 7) is expected


def test_no_findings_when_maintainers_may_push():
    findings = check_push_permissions(
        "grp/repo", project(40), USER, [rule("main", {"access_level": 40})], ["main"], LINK
    )
    assert findings == []


def test_protected_branch_without_push_rights():
    protected = [rule("main", {"access_level": 0}), rule("release/*", {"access_level": 40}), rule("x")]
    findings = check_push_permissions(
        "grp/repo", project(40), USER, protected, ["main", "release/1", "release/2"], LINK
    )
    assert [f.message for f in findings] == [
        "user dev (maintainer) may not push to protected branch main (affects main)"
    ]
    assert findings[0].kind is FindingKind.PUSH_PERMISSION
    assert findings[0].link == LINK


def test_rules_for_branches_that_are_not_synced_are_ignored():
    findings = check_push_permissions(
        "p", project(40), USER, [rule("main", {"access_level": 0})], ["dev"], LINK
    )
    assert findings == []


def test_wildcard_rule_and_group_only_rule():
    protected = [rule("release/*", {"access_level": 50}), rule("main", {"group_id": 3, "access_level": 40})]
    findings = check_push_permissions("p", project(40), USER, protected, ["main", "release/1"], LINK)
    assert [f.message for f in findings] == [
        "user dev (maintainer) may not push to protected branch release/* (affects release/1)",
        "protected branch main only allows pushes for groups; whether user dev is a member was not "
        "checked (affects main)",
    ]


def test_user_without_developer_access_gets_one_finding():
    findings = check_push_permissions("p", project(20), USER, [rule("main")], ["main"], LINK)
    assert [f.message for f in findings] == [
        "user dev has reporter access and cannot push to this project at all"
    ]
