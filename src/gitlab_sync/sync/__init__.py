"""Sync of one repository: branches, tags and the Jenkins rollback analysis."""

from gitlab_sync.sync.repo import sync_repo

__all__ = ["sync_repo"]
