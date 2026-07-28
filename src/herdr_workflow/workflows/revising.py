"""`wq revise <slug> "<comment>"` -- one more round of the build loop, driven by you.

`build` runs its own bounded loop and stops. From there **you** are the round counter, so
this does exactly one code turn and one review turn and hands back.

It deliberately does not auto-fix what the reviewer raises. By this point the reviewer has
already had its rounds, so a new finding is either something your comment caused or
something it previously let pass -- both worth your eyes, not an automatic rewrite of a
diff you had already approved.

And it exits 0 either way. `build` exits 2 at its cap because unreviewed code is sitting on
a branch; `revise` hands you the findings you asked for, which is a result, not a failure.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from herdr_workflow import git
from herdr_workflow.config import Config
from herdr_workflow.errors import WorkflowError
from herdr_workflow.herdr import ops
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.herdr.delivery import agent_state, ask
from herdr_workflow.output import console
from herdr_workflow.workflows import build_env, prompts
from herdr_workflow.workflows.building import BuildPaths
from herdr_workflow.workflows.loops import expect_file, mtime


@dataclass(frozen=True)
class ReviseResult:
    slug: str
    worktree: Path
    branch: str
    delta_file: Path
    diff_file: Path
    review_file: Path
    approved: bool


async def revise(client: HerdrClient, config: Config, slug: str, comment: str) -> ReviseResult:
    if not comment.strip():
        raise WorkflowError(
            "describe the change you want",
            why="revise needs something to ask the code agent for",
            fix=f'run: wq revise {slug} "..."',
        )

    paths = BuildPaths.for_slug(config.root, slug)
    env = _read_env(paths, slug)
    worktree = Path(env.worktree)

    # Each of these produces a different remedy, so the order they are checked in is the
    # order that gets the remedy right: a worktree that is gone means the slug was shipped
    # or cleaned; panes that are gone mean the workspace was closed and the build can be
    # re-run in place.
    if not worktree.is_dir():
        raise WorkflowError(
            f"worktree {worktree} is gone",
            why=f"{slug} has already been shipped or cleaned",
            fix=f'start again: wq plan {slug} "..."',
        )

    code_pane, review_pane = await _find_panes(client, slug, env.repo)
    await _require_idle(client, slug, env.repo, code_pane, review_pane)

    # The pre-turn commit, so the delta below is exactly what this one turn produced.
    # Deliberately not called `base`: in build.env `base` is the branch point, and letting
    # one word mean two things here is how a diff ends up answering a question nobody asked.
    before_head = git.head_commit(worktree)

    console.log("revising")
    await ask(
        client,
        code_pane,
        prompts.revise_code(comment),
        turn_timeout_ms=config.loops.turn_timeout_ms,
        attempts=config.loops.prompt_attempts,
    )

    # The recorded base, never a fresh `resolve_base`: the branch was cut from what
    # build.env says, and re-deriving it here could quietly diff against something else.
    #
    # `diff.patch` is rewritten before anything can fail, because its mtime is the `*`
    # most-recently-worked marker in `wq list`, and the router's `revise` default reads it.
    git.write_diff(worktree, env.base, paths.diff)

    if git.write_delta(worktree, before_head, paths.delta) == 0:
        raise WorkflowError(
            f"nothing new committed on {env.branch}",
            why="the code pane changed nothing this round",
            fix=f"look at the pane: herdr agent attach {code_pane}",
        )

    console.log("reviewing the change")
    before = mtime(paths.review)
    await ask(
        client,
        review_pane,
        prompts.review_revision(paths.delta, comment, paths.diff, paths.plan, paths.review),
        turn_timeout_ms=config.loops.turn_timeout_ms,
        attempts=config.loops.prompt_attempts,
    )
    expect_file(paths.review, before, "reviewer")

    approved = prompts.approved_file(paths.review)
    await ops.notify(
        client,
        f"wq revise: {slug}",
        "Change approved" if approved else "Reviewer has findings",
    )
    if approved:
        console.log("approved")
    else:
        console.log("reviewer has findings — revise again, or ship anyway once you have read them")

    return ReviseResult(
        slug=slug,
        worktree=worktree,
        branch=env.branch,
        delta_file=paths.delta,
        diff_file=paths.diff,
        review_file=paths.review,
        approved=approved,
    )


def _read_env(paths: BuildPaths, slug: str) -> build_env.BuildEnv:
    if not paths.env.is_file() or paths.env.stat().st_size == 0:
        raise WorkflowError(
            f"no build for {slug}",
            why=f"nothing has been built here: {paths.env} does not exist",
            fix=f"run: wq build {slug} <repo>",
        )
    return build_env.read(paths.env)


async def _find_panes(client: HerdrClient, slug: str, repo: str) -> tuple[str, str]:
    """The build's `code` and `review` panes, by label.

    Found in the snapshot rather than read from a stored id: `start_agent` renames each
    pane to its role and the workspace carries the slug, so the snapshot already knows
    where they are. Nothing to persist, nothing to drift.
    """
    snapshot = await client.snapshot()
    code_pane = ops.workspace_pane_by_label(snapshot, slug, "code")
    review_pane = ops.workspace_pane_by_label(snapshot, slug, "review")
    if code_pane is None or review_pane is None:
        raise WorkflowError(
            f"no live build panes for {slug}",
            why="the workspace was closed, so there is nothing to revise in",
            fix=f"re-run: wq build {slug} {repo}",
        )
    return code_pane, review_pane


# A second look before declaring an agent gone. Short, because this is confirming a
# reading rather than waiting for anything to happen.
UNKNOWN_RECHECK = 1.0


async def _require_idle(client: HerdrClient, slug: str, repo: str, *panes: str) -> None:
    """Refuse to start a turn in a pane that is already in one.

    Two revises landing in the same panes interleave their prompts and one is lost
    silently. A pane mid-turn is not idle, so this is cheap to rule out up front.

    **`unknown` is fatal here**, unlike in `delivery`, where `UNKNOWN_GRACE` waits out a
    long registration. There the agent was started moments ago and registration is
    asynchronous; here it was started by `build`, minutes or hours back, so a pane that
    really has no agent is not going to grow one.

    But it is confirmed with a second read first, because `agent_state` reports `unknown`
    for *any* failed `agent.get` -- a transient error included -- and one sample cannot
    tell "gone" from "the call failed once". Declaring it gone throws away a worktree with
    committed work in it, which is far too expensive to get wrong on a single read.
    """
    for pane in panes:
        status = (await agent_state(client, pane)).status
        if status == "unknown":
            await asyncio.sleep(UNKNOWN_RECHECK)
            status = (await agent_state(client, pane)).status
        if status in ("working", "blocked"):
            raise WorkflowError(
                f"a turn is still in flight for {slug}",
                why=f"pane {pane} is {status}",
                fix="wait for it to finish, then try again",
            )
        if status == "unknown":
            raise WorkflowError(
                f"the agent in pane {pane} is gone",
                why="two reads in a row found no agent running in it",
                fix=f"re-run: wq build {slug} {repo}",
            )
