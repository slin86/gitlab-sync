from gitlab_sync.discovery import find_repos


def make_repo(path):
    (path / ".git").mkdir(parents=True)


def test_finds_nested_repos_and_skips_hidden_and_nested(tmp_path):
    make_repo(tmp_path / "a" / "one")
    make_repo(tmp_path / "a" / "b" / "two")
    make_repo(tmp_path / "a" / "one" / "inner")
    make_repo(tmp_path / ".hidden" / "three")
    found = [p.relative_to(tmp_path).as_posix() for p in find_repos(tmp_path, [], [], [])]
    assert found == ["a/b/two", "a/one"]


def test_include_exclude_and_only(tmp_path):
    for name in ("team/a", "team/b", "archive/c"):
        make_repo(tmp_path / name)

    def rel(include, exclude, only):
        return [p.relative_to(tmp_path).as_posix() for p in find_repos(tmp_path, include, exclude, only)]

    assert rel([], ["archive/*"], []) == ["team/a", "team/b"]
    assert rel(["team/*"], ["team/b"], []) == ["team/a"]
    assert rel([], [], ["*/c"]) == ["archive/c"]
