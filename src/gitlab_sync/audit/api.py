"""Read only API calls used by the audit."""

from __future__ import annotations

from typing import Any

from gitlab_sync.gitlab_api import ApiError, GitLabClient, project_id


class AuditApi:
    """Instance and project level reads on one GitLab instance."""

    def __init__(self, client: GitLabClient):
        self.client = client
        self._user: dict[str, Any] | None = None

    @property
    def host(self) -> str:
        return self.client.host

    def current_user(self) -> dict[str, Any]:
        if self._user is None:
            status, user, _ = self.client.get("/user", {})
            if status == 404 or not user:
                raise ApiError(f"cannot read the token user on {self.host}")
            self._user = user
        return self._user

    def project_paths(self, archived: bool) -> set[str]:
        """Paths of all projects the token can see, either archived or active ones."""
        query = {"simple": "true", "archived": "true" if archived else "false"}
        projects = self.client.get_all("/projects", query) or []
        return {p["path_with_namespace"] for p in projects}

    def project(self, project: str) -> dict[str, Any]:
        status, data, _ = self.client.get(f"/projects/{project_id(project)}", {})
        if status == 404:
            raise ApiError(f"project {project} not found on {self.host}")
        return data

    def branch_names(self, project: str) -> list[str]:
        heads = self.client.branch_heads(project)
        if heads is None:
            raise ApiError(f"project {project} not found on {self.host}")
        return sorted(heads)

    def protected_branches(self, project: str) -> list[dict[str, Any]]:
        return self._list(project, "protected_branches")

    def schedules(self, project: str) -> list[dict[str, Any]]:
        """All schedules including their variables, which the list endpoint does not return."""
        details = []
        for item in self._list(project, "pipeline_schedules"):
            path = f"/projects/{project_id(project)}/pipeline_schedules/{item['id']}"
            status, detail, _ = self.client.get(path, {})
            if status != 404:
                details.append(detail)
        return details

    def feature_flags(self, project: str) -> list[dict[str, Any]]:
        return self._list(project, "feature_flags")

    def environments(self, project: str) -> list[dict[str, Any]]:
        return self._list(project, "environments")

    def _list(self, project: str, resource: str) -> list[dict[str, Any]]:
        items = self.client.get_all(f"/projects/{project_id(project)}/{resource}")
        if items is None:
            raise ApiError(f"{resource} of {project} not found on {self.host}")
        return items
