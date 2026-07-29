"""`build.env` -- what `build` records so `revise`, `ship`, `go` and `clean` can pick it up.

The positional format is kept backward compatible: older files may have three or four
lines, while current files add the base ref on line five.

The base ref is used in four places -- the worktree's branch
point, and the diff range that `build`, `revise` and `ship` each regenerate. Re-deriving
it in each command and hoping detection is stable would eventually diff a branch against a
commit it was not cut from. Recording it once is the fix.

Files written before wq recorded the parent workspace have only three lines. Older files
without a base ref have four. Both are read without complaint.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from herdr_workflow.errors import WorkflowError

DEFAULT_BASE = "origin/main"


@dataclass(frozen=True)
class BuildEnv:
    repo: str
    branch: str
    worktree: str
    # Absent in older files, which means there is no parent workspace to close.
    parent_workspace: str | None = None
    # Older files implicitly used `origin/main`, so that is what a missing line means.
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


def require(path: Path, slug: str) -> BuildEnv:
    if not path.is_file() or path.stat().st_size == 0:
        raise WorkflowError(
            f"no build for {slug}",
            why=f"nothing has been built here: {path} does not exist",
            fix=f"run: wq build {slug} <repo>",
        )
    try:
        env = read(path)
    except (OSError, ValueError) as exc:
        raise WorkflowError(
            f"could not read the build metadata for {slug}",
            why=f"{path} is malformed: {exc}",
            fix=f"re-run: wq build {slug} <repo>",
        ) from exc
    if not env.repo or not env.branch or not env.worktree:
        raise WorkflowError(
            f"could not read the build metadata for {slug}",
            why=f"{path} is missing a repository, branch, or worktree",
            fix=f"re-run: wq build {slug} <repo>",
        )
    return env


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
