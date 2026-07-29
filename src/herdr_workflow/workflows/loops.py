"""Shared machinery for the review loops: output files and round bookkeeping.

`expect_file` is behavior #9, and it is the backstop that makes the rest of wq's inferences
safe to get occasionally wrong. Everything else in this codebase reasons about what a TUI is
doing from the outside; a file on disk with a newer mtime is the one thing that is not a
guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from herdr_workflow.errors import WorkflowError


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def expect_file(path: Path, before: float, what: str) -> None:
    """Confirm a turn actually produced its output. Behavior #9.

    Both halves matter. Existence alone passes on a stale file left by the previous round,
    which is how a loop can spin without anyone noticing; the mtime check is what makes it
    a statement about *this* turn.
    """
    if not path.is_file() or path.stat().st_size == 0:
        raise WorkflowError(
            f"{what}: {path} was not written",
            why="the agent finished its turn without producing the file it was asked for",
            fix=f"look at what it did instead, then re-run: {path.parent.name}",
        )
    if mtime(path) == before:
        raise WorkflowError(
            f"{what}: {path} was not updated this round",
            why="the file is unchanged since before the turn, so nothing new was written",
            fix="the agent may have replied in chat instead of writing the file",
        )


@dataclass
class RoundOutcome:
    """How a bounded review loop ended."""

    rounds: int
    approved: bool
