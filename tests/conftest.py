"""Shared fixtures: a bare old and new remote plus a local checkout of the new one.

State after setup:
  main      old has two extra commits (fast forward possible)
  feature   exists only on old
  diverge   changed on both sides in the same file (merge conflict)
  tags      v1 on both, v2 only on old
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from loguru import logger

from gitlab_sync.config import Config
from gitlab_sync.log import TreeLog
from gitlab_sync.precheck import Precheck
from gitlab_sync.results import RepoOutcome, RunResult
from gitlab_sync.sync import sync_repo

PROTECTED_REJECTION = "GitLab: You are not allowed to push code to protected branches on this project."


@pytest.fixture(autouse=True)
def _no_default_log_handler():
    logger.remove()


def git(cwd: Path | str, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def commit(repo: Path, name: str, content: str, message: str, who: str = "dev") -> None:
    (repo / name).write_text(content)
    git(repo, "add", "-A")
    git(repo, "-c", f"user.name={who}", "-c", f"user.email={who}@example.com", "commit", "-qm", message)


@dataclass
class World:
    tmp: Path
    old: Path
    new: Path
    root: Path
    repo: Path
    cfg: Config

    def sync(self, dry_run: bool = False, precheck: Precheck | None = None) -> tuple[RepoOutcome, RunResult]:
        self.cfg.dry_run = dry_run
        result = RunResult()
        with TreeLog(self.cfg.log_file, console=False) as log:
            outcome = sync_repo(self.repo, self.cfg, log, 1, result, old_url=str(self.old), precheck=precheck)
        return outcome, result

    def heads(self, bare: Path) -> list[str]:
        return git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()

    def clone(self, remote: Path) -> Path:
        target = self.tmp / f"clone{len(list(self.tmp.glob('clone*')))}"
        git(self.tmp, "clone", "-q", str(remote), str(target))
        return target

    def push_commits(self, remote: Path, branch: str, *commits: tuple[str, str, str]) -> None:
        """Commit (file, content, committer) tuples on branch and push them to remote."""
        work = self.clone(remote)
        git(work, "checkout", "-q", branch)
        for name, content, who in commits:
            commit(work, name, content, f"{who} {name}", who)
        git(work, "push", "-q", "origin", branch)

    def move_old_tag(self, tag: str = "v1") -> str:
        work = self.clone(self.old)
        commit(work, "tagged", "t", "tag target")
        git(work, "tag", "-f", tag)
        git(work, "push", "-q", "--force", "origin", tag)
        return git(work, "rev-parse", tag)

    def reject_pushes(self, message: str) -> None:
        hook = self.new / "hooks" / "pre-receive"
        hook.write_text(f"#!/bin/sh\necho '{message}' >&2\nexit 1\n")
        hook.chmod(0o755)


@pytest.fixture
def world(tmp_path: Path) -> World:
    old, new = tmp_path / "old.git", tmp_path / "new.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    commit(seed, "a", "1", "initial")
    git(seed, "branch", "diverge")
    git(seed, "tag", "v1")
    for bare in (old, new):
        git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))

    root = tmp_path / "work"
    (root / "grp").mkdir(parents=True)
    repo = root / "grp" / "repo"
    git(tmp_path, "clone", "-q", str(new), str(repo))
    git(repo, "config", "user.name", "dev")
    git(repo, "config", "user.email", "dev@example.com")

    old_work = tmp_path / "old_work"
    git(tmp_path, "clone", "-q", str(old), str(old_work))
    commit(old_work, "a", "2", "old main")
    commit(old_work, "b", "x", "old main 2")
    git(old_work, "checkout", "-q", "-b", "feature")
    commit(old_work, "f", "f", "feature")
    git(old_work, "checkout", "-q", "diverge")
    commit(old_work, "a", "old", "old diverge")
    git(old_work, "tag", "v2")
    git(old_work, "push", "-q", "origin", "--all")
    git(old_work, "push", "-q", "origin", "--tags")

    new_work = tmp_path / "new_work"
    git(tmp_path, "clone", "-q", str(new), str(new_work))
    git(new_work, "checkout", "-q", "diverge")
    commit(new_work, "a", "new", "new diverge")
    git(new_work, "push", "-q", "origin", "diverge")

    cfg = Config(
        root=root,
        new_host="gitlab.example.com",
        old_host="git.example.com",
        log_file=tmp_path / "logs" / "sync.log",
    )
    return World(tmp_path, old, new, root, repo, cfg)
