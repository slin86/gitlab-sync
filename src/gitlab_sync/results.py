"""Outcome of a run: counters and the records that end up in the report files."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class RepoOutcome(Enum):
    OK = "ok"
    PROBLEM = "problem"
    SKIPPED = "skipped-api"


@dataclass(frozen=True)
class BranchConflict:
    """A branch whose merge from the old instance failed."""

    repo: Path
    branch: str
    old_sha: str
    new_sha: str
    commits: int
    jenkins_rollback: bool


@dataclass(frozen=True)
class ProtectedPush:
    """A push that the new instance rejected because the branch is protected."""

    repo: Path
    branch: str
    new_url: str


@dataclass(frozen=True)
class JenkinsCommit:
    sha: str
    committer: str
    subject: str


@dataclass(frozen=True)
class RollbackInstruction:
    """Manual rollback of Jenkins commits that block a merge. The tool never applies it itself."""

    repo: Path
    branch: str
    new_url: str
    new_sha: str
    target_sha: str
    commits: list[JenkinsCommit]
    tags: list[str]


@dataclass
class RunResult:
    repos: int = 0
    repos_ok: int = 0
    repos_skipped: int = 0
    commits: int = 0
    branches_new: int = 0
    branches_updated: int = 0
    branches_current: int = 0
    tags_pushed: int = 0
    tags_overwritten: int = 0
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    branch_conflicts: list[BranchConflict] = field(default_factory=list)
    protected_pushes: list[ProtectedPush] = field(default_factory=list)
    rollbacks: list[RollbackInstruction] = field(default_factory=list)

    @property
    def problem_count(self) -> int:
        return len(self.errors) + len(self.conflicts)

    @property
    def exit_code(self) -> int:
        return 1 if self.problem_count else 0
