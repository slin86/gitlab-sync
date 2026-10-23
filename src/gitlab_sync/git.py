"""Thin wrapper around the git command line."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from gitlab_sync.perf import PerfTracker

NETWORK_COMMANDS = ("fetch", "push", "ls-remote", "clone")


class GitError(RuntimeError):
    """A git command failed or timed out."""


def command_category(args: tuple[str, ...]) -> str:
    """Category for timing: the subcommand, plus the remote name for network commands."""
    if args[0] in NETWORK_COMMANDS:
        for arg in args[1:]:
            if not arg.startswith("-"):
                return f"git {args[0]} {arg}"
    return f"git {args[0]}"


class Git:
    """Runs git in one working directory. Never prompts for credentials."""

    def __init__(self, cwd: Path, timeout: int = 600, perf: PerfTracker | None = None):
        self.cwd = cwd
        self.timeout = timeout
        self.perf = perf or PerfTracker(slow_threshold=0)
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")

    def run(self, *args: str, check: bool = True, input: str | None = None) -> str:
        """Run a command and return stripped stdout. With check=False a failure returns ''."""
        try:
            with self.perf.measure(command_category(args)):
                result = subprocess.run(
                    ["git", *args],
                    cwd=self.cwd,
                    env=self.env,
                    input=input,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]}: timed out after {self.timeout}s") from exc
        if result.returncode != 0:
            if check:
                detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
                raise GitError(f"git {' '.join(args)}: {detail}")
            return ""
        return result.stdout.strip()

    def succeeds(self, *args: str) -> bool:
        try:
            with self.perf.measure(command_category(args)):
                subprocess.run(
                    ["git", *args],
                    cwd=self.cwd,
                    env=self.env,
                    check=True,
                    capture_output=True,
                    timeout=self.timeout,
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
        return True

    def output_or_none(self, *args: str) -> str | None:
        return self.run(*args, check=False) or None

    def resolve(self, ref: str) -> str | None:
        return self.output_or_none("rev-parse", "--verify", "-q", ref)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.succeeds("merge-base", "--is-ancestor", ancestor, descendant)

    def count(self, rev_range: str) -> int:
        return int(self.run("rev-list", "--count", rev_range))

    def remote_url(self, remote: str) -> str | None:
        return self.output_or_none("remote", "get-url", remote)

    def remote_branches(self, remote: str) -> list[str]:
        refs = self.run("for-each-ref", "--format=%(refname:strip=3)", f"refs/remotes/{remote}/")
        return [name for name in refs.splitlines() if name and name != "HEAD"]

    def checkout_reset(self, branch: str, start: str, *extra: str) -> None:
        """Point branch at start and check it out, discarding local changes."""
        self.run("checkout", "-f", "-B", branch, start, *extra)

    def delete_refs(self, prefix: str) -> None:
        refs = self.run("for-each-ref", "--format=delete %(refname)", prefix, check=False)
        if refs:
            self.run("update-ref", "--stdin", input=refs + "\n", check=False)
