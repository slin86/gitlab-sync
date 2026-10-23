import pytest

from gitlab_sync.urls import UrlError, derive_old_url, project_path

NEW, OLD = "gitlab.example.com", "git.example.com"


@pytest.mark.parametrize(
    ("new_url", "old_url"),
    [
        ("git@gitlab.example.com:a/b/c.git", "git@git.example.com:a/b/c.git"),
        ("https://gitlab.example.com/a/b.git", "https://git.example.com/a/b.git"),
        ("ssh://git@gitlab.example.com/a/b.git", "ssh://git@git.example.com/a/b.git"),
        ("https://GitLab.Example.com/a/b.git", "https://git.example.com/a/b.git"),
    ],
)
def test_derive_old_url(new_url, old_url):
    assert derive_old_url(new_url, NEW, OLD) == old_url


@pytest.mark.parametrize("url", ["https://other.com/a.git", "git@other.com:a.git", "/local/path.git"])
def test_derive_old_url_rejects_other_hosts_and_paths(url):
    with pytest.raises(UrlError):
        derive_old_url(url, NEW, OLD)


@pytest.mark.parametrize(
    ("url", "path"),
    [
        ("git@gitlab.example.com:grp/sub/repo.git", "grp/sub/repo"),
        ("https://h/a/b.git", "a/b"),
        ("ssh://git@h/a/b", "a/b"),
    ],
)
def test_project_path(url, path):
    assert project_path(url) == path
