"""State shared by the steps that sync one repository."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from gitlab_sync.config import Config
from gitlab_sync.git import Git, GitError
from gitlab_sync.log import TreeLog
from gitlab_sync.results import Finding, FindingKind, RunResult


@dataclass
class RepoContext:
    path: Path
    cfg: Config
    git: Git
    log: TreeLog
    result: RunResult
    depth: int
    created_branches: list[str] = field(default_factory=list)

    @property
    def new(self) -> str:
        return self.cfg.new_remote

    @property
    def old(self) -> str:
        return self.cfg.old_remote

    @property
    def dry_run(self) -> bool:
        return self.cfg.dry_run

    def new_ref(self, branch: str) -> str:
        return f"refs/remotes/{self.new}/{branch}"

    def old_ref(self, branch: str) -> str:
        return f"refs/remotes/{self.old}/{branch}"

    def new_url(self) -> str:
        return self.git.remote_url(self.new) or ""

    def finding(self, depth: int, kind: FindingKind, message: str) -> None:
        """Log a warning and record it for the findings document."""
        self.log.warning(depth, message)
        self.result.findings.append(Finding(kind, self.cfg.relative_repo(self.path), message))

    def fail(self, depth: int, message: str) -> None:
        self.log.error(depth, message)
        self.result.errors.append(f"{self.path}: {message}")

    def conflict(self, depth: int, message: str) -> None:
        self.log.conflict(depth, message)
        self.result.conflicts.append(f"{self.path}: {message}")

    def try_merge(self, branch: str) -> bool:
        """Merge the old branch into the checked out branch. On failure abort and return False."""
        message = f"Sync {self.old} to {self.new}: {branch}"
        try:
            self.git.run("merge", "--no-edit", "-m", message, self.old_ref(branch))
        except GitError:
            self.git.run("merge", "--abort", check=False)
            return False
        return True
