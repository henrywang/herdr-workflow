"""The GitHub CLI, for the one command that opens and merges a pull request.

Two things in here are the reason this is its own module rather than three inline
subprocess calls.

**`gh pr checks` says "no checks reported" before CI has registered.** That reads exactly
like a CI failure and means the opposite -- see behavior #13. Handing straight to
`--watch` returns instantly and successfully on a PR whose tests have not started.

**`gh pr merge --delete-branch` fails *after* the merge lands.** Behavior #7. The flag is
absent from `merge` on purpose, and there is a test asserting its absence.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from herdr_workflow import process
from herdr_workflow.errors import WorkflowError
from herdr_workflow.output import console

CHECK_POLL_SECONDS = 5

# gh says this when the PR exists but GitHub has not registered its check runs yet.
_NO_CHECKS = "no checks reported"


@dataclass(frozen=True)
class Run:
    code: int
    out: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def _run(args: list[str], cwd: Path) -> Run:
    """Run gh, merging stderr into stdout.

    Merged because gh puts the interesting parts on both, and every caller here is either
    matching on the text or reporting it.
    """
    try:
        proc = process.run(["gh", *args], cwd=cwd)
    except OSError as exc:
        raise WorkflowError(
            "could not run gh",
            why=str(exc),
            fix="install the GitHub CLI: https://cli.github.com",
        ) from exc
    return Run(proc.returncode, (proc.stdout + proc.stderr).strip())


def require() -> None:
    if shutil.which("gh") is None:
        raise WorkflowError(
            "gh is not installed",
            why="wq go opens and merges a pull request through the GitHub CLI",
            fix="install it: https://cli.github.com",
        )


def open_pr(worktree: Path, branch: str, base: str) -> None:
    """Open a pull request, treating "already exists" as success.

    `--fill` takes the title and body from the commits, which is why `build` insists the
    code agent writes a real commit message.
    """
    result = _run(["pr", "create", "--head", branch, "--base", base, "--fill"], worktree)
    if result.ok:
        return
    if "already exists" in result.out:
        console.detail("a pull request for this branch already exists")
        return
    raise WorkflowError(
        f"could not open a pull request for {branch}",
        why=result.out,
        fix=f"open it by hand: cd {worktree} && gh pr create --base {base} --fill",
    )


def pr_number(worktree: Path) -> int:
    result = _run(["pr", "view", "--json", "number", "-q", ".number"], worktree)
    if not result.ok or not result.out.isdigit():
        raise WorkflowError(
            "could not read the pull request number",
            why=result.out or "gh returned nothing",
            fix=f"look for it: cd {worktree} && gh pr view",
        )
    return int(result.out)


def wait_for_checks_to_appear(worktree: Path, pr: int, timeout: int) -> None:
    """Wait until GitHub has registered at least one check run. Behavior #13.

    GitHub registers check runs a few seconds after the PR opens. Until it does,
    `gh pr checks --watch` exits **straight away** with "no checks reported" -- which reads
    exactly like a CI failure and means the opposite. Waiting for the first check to appear
    is what makes the watch below meaningful.
    """
    waited = 0
    while True:
        if _NO_CHECKS not in _run(["pr", "checks", str(pr)], worktree).out:
            return
        if waited >= timeout:
            raise WorkflowError(
                f"no checks appeared on PR #{pr} after {timeout}s",
                why="GitHub never registered a check run for this pull request",
                fix=(
                    "if the repository has no CI, merge by hand: "
                    f"gh pr merge {pr} --squash --delete-branch"
                ),
            )
        time.sleep(CHECK_POLL_SECONDS)
        waited += CHECK_POLL_SECONDS


def watch_checks(worktree: Path, pr: int) -> bool:
    """Block until CI finishes. True if it passed.

    Watched in the shell rather than polled by an agent: an agent looping over
    `gh pr checks` is the most expensive possible way to sit and wait for nothing.
    """
    return _run(["pr", "checks", str(pr), "--watch", "--fail-fast"], worktree).ok


def merge(worktree: Path, pr: int) -> None:
    """Squash-merge. **Deliberately without `--delete-branch`** -- behavior #7.

    In a worktree checkout, `--delete-branch` makes gh clean up the local branch by first
    switching the current checkout to the default branch. But that branch is already
    checked out in the parent repository, so git refuses the second checkout and gh exits
    non-zero **after the merge has landed** -- taking everything after it down too.

    Both branches are deleted by the caller instead, once the worktree holding this one is
    gone.
    """
    result = _run(["pr", "merge", str(pr), "--squash"], worktree)
    if not result.ok:
        raise WorkflowError(
            f"could not merge PR #{pr}",
            why=result.out,
            fix=f"merge it by hand: cd {worktree} && gh pr merge {pr} --squash",
        )
