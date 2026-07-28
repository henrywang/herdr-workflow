"""`build.env` -- what `build` records so `revise`, `ship`, `go` and `clean` can pick it up.

**The first four lines are frozen for v0.1.** Bash and Python run side by side during the
cutover, and a build started by one has to be finishable by the other. The Bash reader is::

    { read -r repo; read -r branch; read -r wt_path; read -r parent || true; } < build.env

which takes the first four lines and ignores whatever follows. That is what makes a fifth
line safe to add: Python records the base ref there, Bash never looks, and a build.env
written by either is readable by both.

Line five exists because the base ref is used in four places -- the worktree's branch
point, and the diff range that `build`, `revise` and `ship` each regenerate. Re-deriving
it in each command and hoping detection is stable would eventually diff a branch against a
commit it was not cut from. Recording it once is the fix.

Files written before wq recorded the parent workspace have only three lines, and files
written by Bash have four. Both are read without complaint.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE = "origin/main"


@dataclass(frozen=True)
class BuildEnv:
    repo: str
    branch: str
    worktree: str
    # Absent in a build.env written by Bash before wq recorded it, so a build in flight
    # during the cutover reads back as "no parent workspace to close" rather than failing.
    parent_workspace: str | None = None
    # Absent in every Bash-written file. Bash always meant `origin/main`, so that is what
    # a missing line means -- not "unknown".
    base: str = DEFAULT_BASE


def path_for(root: Path, slug: str) -> Path:
    return root / slug / "build.env"


def read(path: Path) -> BuildEnv:
    lines = path.read_text().splitlines()
    while len(lines) < 5:
        lines.append("")
    parent = lines[3].strip() or None
    return BuildEnv(
        repo=lines[0].strip(),
        branch=lines[1].strip(),
        worktree=lines[2].strip(),
        parent_workspace=parent,
        base=lines[4].strip() or DEFAULT_BASE,
    )


def write(path: Path, env: BuildEnv) -> None:
    """Write all five lines, always.

    The parent workspace is written as an empty line when there is none, so the base ref
    stays on line five for every file wq writes -- a positional format cannot afford an
    optional line in the middle of it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join([env.repo, env.branch, env.worktree, env.parent_workspace or "", env.base]) + "\n"
    )
