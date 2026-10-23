import pytest

from gitlab_sync.audit.permissions import check_push_permissions, describe_push_access, maintainers_may_push
from gitlab_sync.results import FindingKind

LINK = "https://gitlab.example.com/grp/repo/-/settings/repository#js-protected-branches-settings"


def level(value, description=None, **extra):
    entry = {"access_level": value, **extra}
    if description:
        entry["access_level_description"] = description
    return entry


def rule(name, *entries):
    return {"name": name, "push_access_levels": list(entries)}


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([level(40, "Maintainers")], True),
        ([level(30, "Developers + Maintainers")], True),
        ([level(0, "No one")], False),
        ([level(60, "Admins")], False),
        ([level(40, "nils", user_id=7)], False),
        ([level(40, "backend", group_id=3)], False),
        ([level(0, "No one"), level(40, "Maintainers")], True),
        ([], False),
    ],
)
def test_maintainers_may_push(entries, expected):
    assert maintainers_may_push(rule("main", *entries)) is expected


def test_maintainers_allowed_on_master_gives_no_finding():
    protected = [rule("master", level(40, "Maintainers"))]
    assert check_push_permissions("grp/repo", protected, ["master", "feature"], LINK) == []


def test_rule_that_blocks_maintainers():
    protected = [rule("main", level(0, "No one")), rule("release/*", level(40, "Maintainers")), rule("x")]
    findings = check_push_permissions("grp/repo", protected, ["main", "release/1"], LINK)
    assert [f.message for f in findings] == [
        "protected branch main does not allow Maintainers to push (allowed to push: No one; affects main)"
    ]
    assert findings[0].kind is FindingKind.PUSH_PERMISSION
    assert findings[0].link == LINK


def test_wildcard_rule_lists_all_affected_branches():
    protected = [rule("release/*", level(60, "Admins"), level(40, "nils", user_id=7))]
    findings = check_push_permissions("p", protected, ["main", "release/1", "release/2"], LINK)
    assert [f.message for f in findings] == [
        "protected branch release/* does not allow Maintainers to push "
        "(allowed to push: Admins, nils; affects release/1, release/2)"
    ]


def test_rules_for_branches_that_are_not_synced_are_ignored():
    assert check_push_permissions("p", [rule("main", level(0, "No one"))], ["dev"], LINK) == []


def test_describe_push_access_without_descriptions():
    assert describe_push_access(rule("m", level(0), level(40))) == "Maintainers, No one"
    assert describe_push_access(rule("m")) == "No one"
