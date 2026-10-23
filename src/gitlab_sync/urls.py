"""Parsing and rewriting of git remote URLs."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_SCP_LIKE = re.compile(r"^(?P<user>[^@/:]+@)?(?P<host>[^:/]+):(?P<path>(?!//).*)$")


class UrlError(ValueError):
    """The URL cannot be parsed or does not point to the expected host."""


def derive_old_url(new_url: str, new_host: str, old_host: str) -> str:
    """Replace the host of a remote URL. Scheme, user and project path stay the same.

    Supports scp style URLs (git@host:group/repo.git) and URLs with a scheme (ssh, https).
    """
    if "://" in new_url:
        parts = urlsplit(new_url)
        _check_host(parts.hostname or "", new_host)
        userinfo = parts.netloc.rpartition("@")[0]
        netloc = f"{userinfo}@{old_host}" if userinfo else old_host
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    match = _SCP_LIKE.match(new_url)
    if not match:
        raise UrlError(f"unrecognized URL: {new_url}")
    _check_host(match["host"], new_host)
    return f"{match['user'] or ''}{old_host}:{match['path']}"


def project_path(url: str) -> str:
    """Project path (group/subgroup/repo) of a remote URL, without .git."""
    if "://" in url:
        path = urlsplit(url).path
    else:
        match = _SCP_LIKE.match(url)
        if not match:
            raise UrlError(f"unrecognized URL: {url}")
        path = match["path"]
    path = path.strip("/")
    return path.removesuffix(".git")


def _check_host(host: str, expected: str) -> None:
    if host.lower() != expected.lower():
        raise UrlError(f"origin host '{host}' is not '{expected}'")
