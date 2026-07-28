"""The git wq shells out for, against real repositories.

Real `git init` rather than a mock. These are the operations whose exact behaviour matters
-- three-dot diff ranges, what `origin/HEAD` says, what an uncommitted change looks like --
and a mock would only assert that wq calls git the way wq expects to call git.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from herdr_workflow import git
from herdr_workflow.errors import GitError
from tests.conftest import git_run as _git

# -- toplevel ----------------------------------------------------------------


def test_toplevel_finds_the_root_from_a_subdirectory(repo: Path) -> None:
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    assert git.toplevel(nested) == repo


def test_toplevel_resolves_symlinks(tmp_path: Path, repo: Path) -> None:
    """Behavior #6 compares this against the `repo_root` herdr reports. On macOS /tmp is a
    symlink to /private/tmp, so an unresolved path matches nothing -- silently, and the
    parent workspace leaks every build."""
    link = tmp_path / "link-to-repo"
    link.symlink_to(repo)
    assert git.toplevel(link) == repo
    assert not git.toplevel(link).is_symlink()


def test_a_directory_that_is_not_a_repo_says_so(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError) as caught:
        git.toplevel(plain)
    assert "not a git repository" in caught.value.message
    assert "wq build" in (caught.value.fix or "")


def test_a_missing_directory_says_so(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        git.toplevel(tmp_path / "nope")


# -- resolve_base ------------------------------------------------------------


def test_the_remote_head_is_used_when_published(repo: Path) -> None:
    _git(repo, "remote", "set-head", "origin", "--auto")
    assert git.resolve_base(repo) == "origin/main"


def test_a_master_repo_resolves_to_master(tmp_path: Path) -> None:
    """The reason this function exists. Bash hard-coded `origin/main`, which is wrong for
    every repository that never renamed its default branch."""
    work = tmp_path / "old"
    work.mkdir()
    _git(work, "init", "--initial-branch=master")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "f.txt").write_text("x\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "initial")
    bare = tmp_path / "old.git"
    _git(tmp_path, "clone", "--bare", str(work), str(bare))
    clone = tmp_path / "old-clone"
    _git(tmp_path, "clone", str(bare), str(clone))
    _git(clone, "remote", "set-head", "origin", "--delete")

    assert git.resolve_base(clone) == "origin/master"


def test_main_wins_over_master_without_a_published_head(repo: Path) -> None:
    """Parity: wherever Bash worked, Python picks the same base."""
    _git(repo, "remote", "set-head", "origin", "--delete")
    assert git.resolve_base(repo) == "origin/main"


def test_a_repo_with_no_usable_base_explains_itself(tmp_path: Path) -> None:
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    _git(lonely, "init", "--initial-branch=main")
    with pytest.raises(GitError) as caught:
        git.resolve_base(lonely)
    assert "could not work out what to branch from" in caught.value.message
    assert "set-head" in (caught.value.fix or "")


# -- write_diff --------------------------------------------------------------


def test_a_committed_change_produces_a_diff(repo: Path, tmp_path: Path) -> None:
    _git(repo, "checkout", "-b", "wq/x")
    (repo / "new.py").write_text("def two():\n    return 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add two")

    out = tmp_path / "diff.patch"
    size = git.write_diff(repo, "origin/main", out)
    assert size > 0
    assert "def two()" in out.read_text()


def test_an_uncommitted_change_produces_nothing(repo: Path, tmp_path: Path) -> None:
    """The failure `no changes committed on <branch>` exists for. The review reads the
    diff, so an agent that edited files without committing has produced nothing to review
    -- and would otherwise be reviewed as if it had done no work at all."""
    _git(repo, "checkout", "-b", "wq/x")
    (repo / "new.py").write_text("def two():\n    return 2\n")

    out = tmp_path / "diff.patch"
    assert git.write_diff(repo, "origin/main", out) == 0
    assert out.read_text() == ""


def test_the_range_is_three_dot(repo: Path, tmp_path: Path) -> None:
    """Three-dot diffs the branch against where it was cut from. Two-dot would show the
    base moving on as if the branch had reverted it."""
    _git(repo, "checkout", "-b", "wq/x")
    (repo / "mine.txt").write_text("mine\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "mine")

    # main moves on, independently.
    _git(repo, "checkout", "main")
    (repo / "theirs.txt").write_text("theirs\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "theirs")
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")
    _git(repo, "checkout", "wq/x")

    out = tmp_path / "diff.patch"
    git.write_diff(repo, "origin/main", out)
    text = out.read_text()
    assert "mine.txt" in text
    assert "theirs.txt" not in text


def test_a_bad_base_fails_with_gits_own_message(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(GitError) as caught:
        git.write_diff(repo, "origin/does-not-exist", tmp_path / "d.patch")
    assert caught.value.why


def test_fetch_reports_a_missing_remote(tmp_path: Path) -> None:
    """Fatal on purpose, matching Bash under `set -e`: a branch cut from a stale base is
    worse than a clear failure."""
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    _git(lonely, "init", "--initial-branch=main")
    with pytest.raises(GitError):
        git.fetch(lonely)
