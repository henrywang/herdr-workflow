"""Shared subprocess execution for Git and GitHub CLI operations.

Commands are always passed as argument lists without a shell, so values such as branch
names and paths are not interpreted as shell syntax.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

TimeoutExpired = subprocess.TimeoutExpired


def run(
    args: list[str], *, cwd: Path | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command and capture its text output without raising for its exit code."""
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
