"""`wq build <slug> [repo]` -- worktree, code ↔ review loop, commits.

Same shape as `wq plan`, with three things `plan` does not have to deal with.

**A throwaway worktree.** The code agent works on a branch in a directory nothing else is
using, which is what makes running it unattended reasonable. Nothing it does touches the
checkout you are sitting in.

**Behavior #6.** One `worktree.create` opens *two* workspaces when the repository has no
workspace open yet, and reports only one. The other is found by diffing the workspace list
around the call, and recorded, or every build leaks a workspace nothing knows how to close.

**A non-zero exit.** Where `plan` stopping at its round cap is a result you read, `build`
stopping at its cap means unreviewed code is sitting on a branch. The router's contract is
to stop on a non-zero exit, so this reports `2` -- distinguishable from the `1` of a real
failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from herdr_workflow import git
from herdr_workflow.config import Config
from herdr_workflow.errors import WorkflowError
from herdr_workflow.herdr import ops
from herdr_workflow.herdr.agents import start_agent
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.herdr.delivery import ask
from herdr_workflow.output import console
from herdr_workflow.workflows import build_env, prompts
from herdr_workflow.workflows.loops import RoundOutcome, expect_file, mtime


@dataclass(frozen=True)
class BuildPaths:
    dir: Path
    plan: Path
    review: Path
    diff: Path
    delta: Path
    env: Path

    @classmethod
    def for_slug(cls, root: Path, slug: str) -> BuildPaths:
        d = root / slug
        return cls(
            dir=d,
            plan=d / "plan.md",
            # `code-review.md`, not `review.md`: a slug that was planned and then built has
            # both files in the same directory, and the plan's review must survive.
            review=d / "code-review.md",
            diff=d / "diff.patch",
            # Written by `revise` only: what one revision turn changed, where `diff` is the
            # whole branch.
            delta=d / "revise.patch",
            env=d / "build.env",
        )


@dataclass(frozen=True)
class BuildResult:
    slug: str
    workspace_id: str
    worktree: Path
    branch: str
    base: str
    diff_file: Path
    review_file: Path
    rounds: int
    approved: bool
    parent_workspace: str | None


async def build(client: HerdrClient, config: Config, slug: str, repo_arg: Path) -> BuildResult:
    paths = BuildPaths.for_slug(config.root, slug)

    # Deliberately *not* "approved": a plan that hit its round cap is still a plan, and
    # deciding to build it anyway is the user's call.
    if not paths.plan.is_file() or paths.plan.stat().st_size == 0:
        raise WorkflowError(
            f"no plan at {paths.plan}",
            why="build implements a plan, and this slug does not have one",
            fix=f'run: wq plan {slug} "..."',
        )

    repo = git.toplevel(repo_arg)
    branch = f"wq/{slug}"
    worktree = Path(f"{repo}-worktrees/{slug}")

    console.log(f"creating worktree {branch}")
    git.fetch(repo)
    base = git.resolve_base(repo)
    console.detail(f"branching from {base}")

    workspace_id, code_pane, parent = await _create_worktree(
        client, repo=repo, branch=branch, base=base, path=worktree, label=slug
    )
    build_env.write(
        paths.env,
        build_env.BuildEnv(
            repo=str(repo),
            branch=branch,
            worktree=str(worktree),
            parent_workspace=parent,
            base=base,
        ),
    )

    review_pane = await _open_panes(client, config, code_pane, worktree)

    turn_timeout = config.loops.turn_timeout_ms
    attempts = config.loops.prompt_attempts

    console.log("implementing")
    await ask(
        client,
        code_pane,
        prompts.implement(paths.plan, worktree, branch),
        turn_timeout_ms=turn_timeout,
        attempts=attempts,
    )

    outcome = await _review_loop(
        client,
        code_pane=code_pane,
        review_pane=review_pane,
        paths=paths,
        worktree=worktree,
        branch=branch,
        base=base,
        max_rounds=config.loops.code_rounds,
        turn_timeout_ms=turn_timeout,
        attempts=attempts,
    )

    await ops.notify(
        client,
        f"wq build: {slug}",
        "Diff ready for your review"
        if outcome.approved
        else f"Stopped after {config.loops.code_rounds} rounds with findings outstanding",
    )
    return BuildResult(
        slug=slug,
        workspace_id=workspace_id,
        worktree=worktree,
        branch=branch,
        base=base,
        diff_file=paths.diff,
        review_file=paths.review,
        rounds=outcome.rounds,
        approved=outcome.approved,
        parent_workspace=parent,
    )


async def _create_worktree(
    client: HerdrClient,
    *,
    repo: Path,
    branch: str,
    base: str,
    path: Path,
    label: str,
) -> tuple[str, str, str | None]:
    """Create the worktree workspace, and identify the parent one herdr opened silently.

    Returns the workspace, its root pane -- which becomes the code pane -- and the parent
    workspace id, if one was opened and could be identified.

    Behavior #6. `worktree.create` reports the linked worktree it was asked for; when the
    repository had no workspace open it opens a second one for the parent checkout and
    says nothing. Diffing the workspace list around the call is the only way to see it.

    Two things about the predicate. The **diff** is what proves wq opened it -- when the
    repository already had a workspace, that workspace is the user's and not wq's to close.
    The **repo_root and is_linked_worktree test** is what says which of the new ones it is:
    a concurrent wq command opens workspaces of its own, and picking by timing alone would
    eventually record, and later close, someone else's.

    A parent that cannot be identified is recorded as `None` and the build continues. The
    cost is one leaked workspace; failing here would throw away a worktree that was
    successfully created.
    """
    before = await ops.workspace_ids(client)
    created = await ops.worktree_create(
        client, repo=repo, branch=branch, base=base, path=path, label=label
    )
    workspace_id = created.workspace.workspace_id
    # Taken from the response rather than looked up in a later snapshot: the schema marks
    # it required, and a snapshot scan can only race the workspace it is trying to describe.
    root_pane = created.root_pane.pane_id

    parent: str | None = None
    try:
        snapshot = await client.snapshot()
        new_ids = {w.workspace_id for w in snapshot.workspaces} - before - {workspace_id}
        for workspace in snapshot.workspaces:
            if workspace.workspace_id not in new_ids or workspace.worktree is None:
                continue
            # Resolved on both sides: on macOS /tmp is a symlink to /private/tmp, and an
            # unresolved comparison here matches nothing, silently, every time.
            if (
                Path(workspace.worktree.repo_root).resolve() == repo
                and not workspace.worktree.is_linked_worktree
            ):
                parent = workspace.workspace_id
                break
    except Exception as exc:
        console.detail(f"could not identify the parent workspace: {exc}")

    if parent:
        console.detail(f"herdr also opened {parent} for the parent checkout")
    return workspace_id, root_pane, parent


async def _open_panes(client: HerdrClient, config: Config, code_pane: str, worktree: Path) -> str:
    """Split the reviewer in beside the code pane and start both agents.

    Both panes are opened in the worktree, never in the parent checkout: the reviewer needs
    to read the files the diff refers to, and neither agent has any business in the
    checkout you are sitting in.
    """
    review_pane = await ops.pane_split(client, code_pane, worktree)
    await start_agent(client, code_pane, config.agents.code, "code", config)
    await start_agent(client, review_pane, config.agents.review, "review", config)
    return review_pane


async def _review_loop(
    client: HerdrClient,
    *,
    code_pane: str,
    review_pane: str,
    paths: BuildPaths,
    worktree: Path,
    branch: str,
    base: str,
    max_rounds: int,
    turn_timeout_ms: int,
    attempts: int,
) -> RoundOutcome:
    round_number = 1
    while True:
        # Regenerated every round, before the review: the reviewer must see what the last
        # fix turn actually committed, not what it committed the round before.
        if git.write_diff(worktree, base, paths.diff) == 0:
            raise WorkflowError(
                f"no changes committed on {branch}",
                why=(
                    f"git diff {base}...HEAD is empty, so the agent either changed nothing "
                    "or left its work uncommitted"
                ),
                fix=f"look at the worktree: cd {worktree} && git status",
            )

        console.log(f"round {round_number}: code review")
        before = mtime(paths.review)
        await ask(
            client,
            review_pane,
            prompts.review_code(paths.diff, paths.plan, paths.review),
            turn_timeout_ms=turn_timeout_ms,
            attempts=attempts,
        )
        expect_file(paths.review, before, "reviewer")

        if prompts.approved_file(paths.review):
            console.log(f"code approved in round {round_number}")
            return RoundOutcome(rounds=round_number, approved=True)

        if round_number >= max_rounds:
            console.log(f"round limit ({max_rounds}) reached with findings outstanding")
            console.log(f"review: {paths.review}")
            return RoundOutcome(rounds=round_number, approved=False)

        round_number += 1
        console.log(f"round {round_number}: fixing")
        await ask(
            client,
            code_pane,
            prompts.fix_findings(paths.review),
            turn_timeout_ms=turn_timeout_ms,
            attempts=attempts,
        )
