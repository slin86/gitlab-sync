"""Minimal read only client for the GitLab REST API v4. The tool never writes through the API."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from gitlab_sync.perf import PerfTracker

PAGE_SIZE = 100


ENDPOINT_KINDS = (
    "branches",
    "tags",
    "refs",
    "protected_branches",
    "pipeline_schedules",
    "feature_flags",
    "environments",
    "projects",
    "user",
)


class ApiError(RuntimeError):
    """The API could not be reached or answered with an unexpected status."""


def endpoint_kind(path: str) -> str:
    """Resource name used as timing category, e.g. branches or pipeline_schedules."""
    for segment in reversed(path.strip("/").split("/")):
        if segment in ENDPOINT_KINDS:
            return "commit_refs" if segment == "refs" else segment
    return "project" if path.startswith("/projects/") else "other"


def project_id(project: str) -> str:
    """URL encoded project path for use in /projects/:id."""
    return quote(project, safe="")


class GitLabClient:
    def __init__(
        self, host: str, token: str, scheme: str = "https", timeout: int = 15, perf: PerfTracker | None = None
    ):
        self.host = host
        self.token = token
        self.scheme = scheme
        self.timeout = timeout
        self.perf = perf or PerfTracker(slow_threshold=0)

    def get(self, path: str, query: dict[str, Any]) -> tuple[int, Any, Any]:
        """Return (status, json, headers). A 404 is returned, every other failure raises ApiError."""
        url = f"{self.scheme}://{self.host}/api/v4{path}?{urlencode(query)}"
        request = Request(url, headers={"PRIVATE-TOKEN": self.token, "Accept": "application/json"})
        category = f"api {self.host} {endpoint_kind(path)}"
        try:
            with self.perf.measure(category), urlopen(request, timeout=self.timeout) as response:
                return response.status, json.load(response), response.headers
        except HTTPError as exc:
            if exc.code == 404:
                return 404, None, exc.headers
            raise ApiError(f"{self.host}{path}: HTTP {exc.code}") from exc
        except (URLError, OSError, ValueError) as exc:
            raise ApiError(f"{self.host}{path}: {exc}") from exc

    def get_all(self, path: str, query: dict[str, Any] | None = None) -> list | None:
        """Follow X-Next-Page until the last page. Returns None if the resource does not exist."""
        items: list = []
        page = 1
        while page:
            status, data, headers = self.get(path, {**(query or {}), "per_page": PAGE_SIZE, "page": page})
            if status == 404:
                return None
            items.extend(data)
            next_page = headers.get("X-Next-Page") if headers else ""
            page = int(next_page) if next_page else 0
        return items

    def branch_heads(self, project: str) -> dict[str, str] | None:
        branches = self.get_all(f"{_repository(project)}/branches")
        if branches is None:
            return None
        return {b["name"]: b["commit"]["id"] for b in branches}

    def tag_targets(self, project: str) -> dict[str, str]:
        """Tag name to target. For annotated tags the target is the tag object, as in git."""
        tags = self.get_all(f"{_repository(project)}/tags") or []
        return {t["name"]: t.get("target") or t["commit"]["id"] for t in tags}

    def branches_containing(self, project: str, sha: str) -> set[str] | None:
        refs = self.get_all(f"{_repository(project)}/commits/{sha}/refs")
        if refs is None:
            return None
        return {r["name"] for r in refs if r["type"] == "branch"}


def _repository(project: str) -> str:
    return f"/projects/{project_id(project)}/repository"
