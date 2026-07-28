"""The git wq shells out for.

wq drives git through the `git` binary rather than a library, for the same reason the Bash
version did: the operations are few, the CLI is the interface everyone already knows, and a
worktree created by `git worktree add` behaves identically whoever created it.

One deliberate divergence from Bash lives here. Bash hard-coded `origin/main` as the base
ref -- in the worktree it created, and in the diff range it reviewed. That is a personal
assumption a shared tool must not make: plenty of repositories still branch from `master`,
and plenty branch from `develop`. `resolve_base` asks the repository instead, and the
answer is recorded in `build.env` so every later command diffs against the same commit the
branch was cut from. See docs/parity.md.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from herdr_workflow.errors import GitError

# Tried in order when the remote does not publish a HEAD. Bash's hard-coded default comes
# first, so parity holds on any repository where Bash worked at all.
_FALLBACK_BRANCHES = ("main", "master")


def _run(args: list[str], cwd: Path, *, what: str) -> str:
    """Run a git command, returning stdout. Raises GitError with git's own message."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise GitError(
            f"could not run git: {what}",
            why=str(exc),
            fix="check that git is installed and on PATH",
        ) from exc
    if proc.returncode != 0:
        # git's stderr is more useful than anything wq could synthesise.
        raise GitError(
            f"git {what} failed in {cwd}",
            why=(proc.stderr or proc.stdout).strip() or f"git exited {proc.returncode}",
        )
    return proc.stdout


def _ok(args: list[str], cwd: Path) -> bool:
    """Did the command succeed? For probes, where failure is an answer rather than an error."""
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    except OSError:
        return False
    return proc.returncode == 0


def toplevel(path: Path) -> Path:
    """The repository root containing `path`.

    Resolved, because behavior #6 compares this against the `repo_root` herdr reports, and
    on macOS `/tmp` is a symlink to `/private/tmp` -- an unresolved comparison matches
    nothing, silently, and the parent workspace leaks.
    """
    if not path.is_dir():
        raise GitError(
            f"not a directory: {path}",
            fix="pass a path inside the repository you want to build in",
        )
    try:
        out = _run(["rev-parse", "--show-toplevel"], path, what="rev-parse")
    except GitError as exc:
        raise GitError(
            f"not a git repository: {path}",
            why=exc.why,
            fix="run wq build from inside a repository, or pass one: wq build <slug> <repo>",
        ) from exc
    return Path(out.strip()).resolve()


def fetch(repo: Path, remote: str = "origin") -> None:
    """Update remote refs before cutting a branch.

    Fatal, matching Bash under `set -e`: building on a stale `origin/main` produces a diff
    against a commit that is not what anyone will review, and a branch cut from the wrong
    place is worse than a clear failure.
    """
    _run(["fetch", remote], repo, what=f"fetch {remote}")


def resolve_base(repo: Path, remote: str = "origin") -> str:
    """The ref a build branch should be cut from, e.g. `origin/main`.

    Asks the remote what its HEAD is, then falls back to the conventional names. Only refs
    that actually exist are returned: a base that cannot be resolved would fail later, in
    the middle of `worktree.create`, with a much worse message.
    """
    head_ref = ["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"]
    if _ok(head_ref, repo):
        published = _run(head_ref, repo, what="symbolic-ref").strip()
        if published:
            return published

    for name in _FALLBACK_BRANCHES:
        ref = f"{remote}/{name}"
        if _ok(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo):
            return ref

    raise GitError(
        f"could not work out what to branch from in {repo}",
        why=(
            f"{remote} publishes no HEAD, and neither "
            f"{' nor '.join(f'{remote}/{n}' for n in _FALLBACK_BRANCHES)} exists"
        ),
        fix=f"set it once with: git remote set-head {remote} --auto",
    )


def write_diff(worktree: Path, base: str, out: Path) -> int:
    """Write `git diff <base>...HEAD` to `out`, returning its size in bytes.

    Three-dot on purpose: the diff is the branch's own work against where it was cut from,
    not against wherever the base has moved since.

    An empty result is the signal that an agent edited files but never committed. That is
    a real and common failure, so the size comes back rather than being asserted here --
    the caller has the branch name and can say something useful about it.
    """
    text = _run(["diff", f"{base}...HEAD"], worktree, what=f"diff {base}...HEAD")
    out.write_text(text)
    return len(text)
