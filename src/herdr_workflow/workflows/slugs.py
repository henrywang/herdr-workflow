"""Validation for workflow slugs used in paths, branch names, and herdr labels.

A slug crosses several boundaries and `wq clean` eventually uses it to remove a directory.
Keeping it to one safe component prevents path traversal and invalid Git branch names.
"""

from __future__ import annotations

import re

from herdr_workflow.errors import WorkflowError

_SAFE_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def validate(slug: str) -> str:
    """Return a safe slug or raise with the accepted format."""
    if _SAFE_SLUG.fullmatch(slug):
        return slug
    raise WorkflowError(
        f"invalid slug: {slug!r}",
        why="slugs must be 1-64 lowercase letters, numbers, hyphens, or underscores",
        fix="use a short name such as: auth-refresh",
    )
