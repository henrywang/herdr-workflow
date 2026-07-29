"""`wq ship <slug>` and `wq go <slug>` -- push, PR, CI, merge, clean up.

**`go` cannot run just anywhere, and `ship` exists because of it.** `go` blocks for the
length of CI and its cleanup closes the workspace the build panes live in. Run from the
code pane it deletes itself mid-command; run from the router it blocks the one pane that
has to stay responsive; run from any agent pane its bash tool times out long before a slow
CI run ends.

So `ship` puts it where a long blocking command belongs: a plain shell tab in the inbox
with no agent in it at all. Typing the command returns immediately, so the caller -- usually
the router -- is free straight away, and you watch the push, the CI and the merge in the tab.

**The rule is enforced in `go` itself, not only in the router's prompt.** A prompt is
advice; this command pushes, merges and deletes branches.

The other rule this file exists to honour is rule of thumb #4: **after an irreversible step,
nothing may abort.** Everything after `gh pr merge` succeeds is cleanup, and cleanup that
fails is a message, never an error.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from herdr_workflow import git
from herdr_workflow.config import Config
from herdr_workflow.errors import WorkflowError
from herdr_workflow.git import gh
from herdr_workflow.herdr import ops
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.output import console
from herdr_workflow.workflows import build_env, cleanup, slugs
from herdr_workflow.workflows.building import BuildPaths
from herdr_workflow.workflows.inbox import inbox_workspace_id


@dataclass(frozen=True)
class ShipResult:
    slug: str
    workspace_id: str
    tab_label: str
    pane_id: str
    command: str
    created_tab: bool


@dataclass(frozen=True)
class GoResult:
    slug: str
    pr: int
    branch: str
    merged: bool


def _require_build(config: Config, slug: str) -> tuple[BuildPaths, build_env.BuildEnv]:
    slugs.validate(slug)
    paths = BuildPaths.for_slug(config.root, slug)
    return paths, build_env.require(paths.env, slug)


# -- ship --------------------------------------------------------------------


def wq_command() -> str:
    """How to invoke this same wq from a fresh shell.

    Three arms, in order of fidelity: the executable actually running, then whatever `wq`
    is on PATH, then the module. The last is the one that works from a checkout with no
    console script installed.
    """
    argv0 = Path(sys.argv[0])
    if argv0.name in ("wq", "wq.exe") and argv0.is_file():
        return shlex.quote(str(argv0.resolve()))
    found = shutil.which("wq")
    if found:
        return shlex.quote(found)
    return f"{shlex.quote(sys.executable)} -m herdr_workflow"


def ship_command(slug: str, root: Path) -> str:
    """The shell line typed into the ship tab.

    `WQ_ROOT` is passed explicitly. The tab is a fresh login shell, so nothing from the invoking
    process's environment follows it there; a `WQ_ROOT` set inline or exported by a
    wrapper would be silently lost and `go` would look for a build that, from where it is
    standing, does not exist.

    Everything is `shlex.quote`d. This is a shell line, not an argv, and the slug comes
    from a router that is repeating a human's words.
    """
    return f"WQ_ROOT={shlex.quote(str(root))} {wq_command()} go {shlex.quote(slug)}"


async def ship(client: HerdrClient, config: Config, slug: str, home: Path) -> ShipResult:
    _require_build(config, slug)

    workspace_id = await inbox_workspace_id(client, config)
    label = f"ship-{slug}"

    snapshot = await client.snapshot()
    pane = ops.tab_pane_by_label(snapshot, workspace_id, label)
    created = pane is None
    if pane is None:
        tab = await ops.tab_create(client, workspace_id, label, home)
        pane = tab.root_pane.pane_id

    command = ship_command(slug, config.root)
    await ops.pane_run(client, pane, command)

    try:
        await ops.pane_focus(client, pane)
    except Exception:
        console.detail(f"could not focus pane {pane}")

    console.log(f"shipping {slug} in tab {label} — push, PR, CI and merge run there")
    return ShipResult(
        slug=slug,
        workspace_id=workspace_id,
        tab_label=label,
        pane_id=pane,
        command=command,
        created_tab=created,
    )


# -- go ----------------------------------------------------------------------


async def running_in_agent_pane(client: HerdrClient) -> bool:
    """Is this command running inside a pane that has an agent registered against it?

    herdr exports `HERDR_PANE_ID` into every pane. A pane with an agent is an agent pane --
    the router, or a build pane it delegated to. The shell tab `wq ship` opens has no agent
    in it, so the sanctioned path passes straight through.

    **Fails open:** if the agent list cannot be read, `go` proceeds. The
    trade is deliberate. Failing closed would block a legitimate ship whenever herdr
    hiccups, and the guard is a backstop for a rule the router's prompt already states --
    not the only thing standing between the user and a bad merge.
    """
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return False
    try:
        agents = await ops.agent_list(client)
    except Exception:
        return False
    return any(a.pane_id == pane for a in agents)


async def go(client: HerdrClient, config: Config, slug: str) -> GoResult:
    paths, env = _require_build(config, slug)
    gh.require()

    if await running_in_agent_pane(client):
        raise WorkflowError(
            "wq go cannot run in an agent pane",
            why="it pushes, merges and deletes branches, and its cleanup closes this workspace",
            fix=f"run: wq ship {slug}",
        )

    repo = Path(env.repo)
    worktree = Path(env.worktree)
    base_branch = git.branch_of(env.base)

    console.log(f"pushing {env.branch}")
    git.push(worktree, env.branch)

    console.log("opening pull request")
    gh.open_pr(worktree, env.branch, base_branch)
    pr = gh.pr_number(worktree)

    console.log(f"PR #{pr} — waiting for CI")
    gh.wait_for_checks_to_appear(worktree, pr, config.loops.ci_appear_timeout)

    if not gh.watch_checks(worktree, pr):
        await ops.notify(client, f"wq go: {slug}", f"CI failed on PR #{pr}")
        raise WorkflowError(
            f"CI failed on PR #{pr}",
            why="at least one check run failed",
            fix=f"fix it, then re-run: wq go {slug}",
        )

    console.log(f"merging PR #{pr}")
    gh.merge(worktree, pr)

    # ---- past the point of no return -------------------------------------
    # The merge has landed. Nothing below may raise, and nothing below may change the
    # outcome this function reports.
    await _clean_up(client, slug, repo, worktree, env, base_branch, paths)

    await ops.notify(client, f"wq go: {slug}", f"PR #{pr} merged")
    console.log(f"merged PR #{pr}")
    return GoResult(slug=slug, pr=pr, branch=env.branch, merged=True)


async def _clean_up(
    client: HerdrClient,
    slug: str,
    repo: Path,
    worktree: Path,
    env: build_env.BuildEnv,
    base_branch: str,
    paths: BuildPaths,
) -> None:
    """Tidy up after a merge that has already landed.

    **Every step runs through `_step`, which swallows everything.** The individual helpers
    are already written not to raise, but relying on that is one refactor away from being
    wrong -- and the cost of being wrong here is a user staring at a traceback with no idea
    whether their code went out. A leaked worktree is an annoyance; that is not.
    """
    console.log(f"refreshing {repo}")
    _step("fetch", lambda: git.fetch_prune(repo), f"could not fetch {repo}")

    # Skipped rather than failed when the checkout is busy: `repo` is the user's primary
    # checkout, and rebasing a branch they happen to be sitting on is worse than leaving it
    # a commit behind.
    if _step("check", lambda: git.can_refresh(repo, base_branch), "could not read repo state"):
        _step(
            "rebase",
            lambda: git.rebase(repo, env.base),
            f"could not fast-forward {repo} — rebase by hand",
        )
    else:
        console.log(f"skipping refresh — {repo} is dirty or not on {base_branch}")

    console.log("cleaning up")
    await _astep(
        lambda: _remove_worktree_workspace(client, slug),
        f"could not remove the worktree workspace for {slug}",
    )

    # The workspace may already have been closed, in which case herdr has no id to remove
    # and the checkout is still on disk holding the branch. Drop it directly, then prune,
    # so `branch -D` is not refused by a worktree nobody is using.
    _step("worktree remove", lambda: git.worktree_remove(repo, worktree), "")
    _step("worktree prune", lambda: git.worktree_prune(repo), "")
    _step(
        "branch -D",
        lambda: git.delete_branch(repo, env.branch),
        f"local branch {env.branch} was not deleted",
    )
    # Repositories with "automatically delete head branches" on have already dropped it,
    # and that failure means success.
    _step("push --delete", lambda: git.delete_remote_branch(repo, env.branch), "")

    await _astep(
        lambda: cleanup.close_parent_ws(client, env.parent_workspace),
        "could not close the parent workspace",
    )

    console.detail(f"build artifacts kept at {paths.dir}")


def _step(name: str, run: Callable[[], bool], on_false: str) -> bool:
    """Run one cleanup step. Never raises, whatever `run` does."""
    try:
        ok = run()
    except Exception as exc:
        console.detail(f"{name} failed after the merge: {exc}")
        return False
    if not ok and on_false:
        console.detail(on_false)
    return ok


async def _astep(run: Callable[[], Awaitable[bool]], on_error: str) -> bool:
    try:
        return await run()
    except Exception:
        console.detail(on_error)
        return False


async def _remove_worktree_workspace(client: HerdrClient, slug: str) -> bool:
    snapshot = await client.snapshot()
    workspace = ops.workspace_by_label(snapshot, slug)
    if workspace is None:
        return False
    await ops.worktree_remove(client, workspace.workspace_id)
    return True
