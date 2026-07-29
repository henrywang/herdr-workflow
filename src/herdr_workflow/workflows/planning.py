"""`wq plan <slug> "<request>"` -- the plan ↔ review loop.

Two panes side by side in their own workspace: a planner writes, a reviewer attacks, and
they iterate until the reviewer signs off or the round cap is reached.

The reviewer is deliberately **not the model that wrote**. Which way round you assign them
is a free choice; that they differ is the whole point. A model reviewing its own plan still
produces a review, still reports approvals, and is no longer adversarial -- which is the
failure you would never notice from the output.

The loop is bounded. Two agents that disagree can disagree forever, and an unbounded loop
spends real money doing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from herdr_workflow.config import Config
from herdr_workflow.herdr import ops
from herdr_workflow.herdr.agents import start_agent
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.herdr.delivery import ask
from herdr_workflow.output import console
from herdr_workflow.workflows import prompts, slugs
from herdr_workflow.workflows.loops import RoundOutcome, expect_file, mtime


@dataclass(frozen=True)
class PlanResult:
    slug: str
    workspace_id: str
    plan_file: Path
    review_file: Path
    rounds: int
    approved: bool


@dataclass(frozen=True)
class PlanPaths:
    dir: Path
    request: Path
    plan: Path
    review: Path

    @classmethod
    def for_slug(cls, root: Path, slug: str) -> PlanPaths:
        d = root / slug
        return cls(dir=d, request=d / "request.md", plan=d / "plan.md", review=d / "review.md")


async def plan(client: HerdrClient, config: Config, slug: str, request: str) -> PlanResult:
    slugs.validate(slug)
    paths = PlanPaths.for_slug(config.root, slug)
    paths.dir.mkdir(parents=True, exist_ok=True)
    paths.request.write_text(request.rstrip() + "\n")

    created = await ops.workspace_create(client, f"plan-{slug}", paths.dir)
    workspace_id = created.workspace.workspace_id
    planner_pane = created.root_pane.pane_id

    if created.tab is not None:
        await ops.tab_rename(client, created.tab.tab_id, "task")
    reviewer_pane = await ops.pane_split(client, planner_pane, paths.dir)

    await start_agent(client, planner_pane, config.agents.plan, "plan", config)
    await start_agent(client, reviewer_pane, config.agents.review, "review", config)

    turn_timeout = config.loops.turn_timeout_ms
    attempts = config.loops.prompt_attempts

    console.log("round 1: drafting plan")
    before = mtime(paths.plan)
    await ask(
        client,
        planner_pane,
        prompts.draft_plan(paths.request, paths.plan),
        turn_timeout_ms=turn_timeout,
        attempts=attempts,
    )
    expect_file(paths.plan, before, "planner")

    outcome = await _review_loop(
        client,
        planner_pane=planner_pane,
        reviewer_pane=reviewer_pane,
        paths=paths,
        max_rounds=config.loops.plan_rounds,
        turn_timeout_ms=turn_timeout,
        attempts=attempts,
    )

    await ops.notify(
        client,
        f"wq plan: {slug}",
        "Plan ready for your review" if outcome.approved else "Stopped with findings outstanding",
    )
    return PlanResult(
        slug=slug,
        workspace_id=workspace_id,
        plan_file=paths.plan,
        review_file=paths.review,
        rounds=outcome.rounds,
        approved=outcome.approved,
    )


async def _review_loop(
    client: HerdrClient,
    *,
    planner_pane: str,
    reviewer_pane: str,
    paths: PlanPaths,
    max_rounds: int,
    turn_timeout_ms: int,
    attempts: int,
) -> RoundOutcome:
    round_number = 1
    while True:
        console.log(f"round {round_number}: review")
        before = mtime(paths.review)
        # The reviewer gets a path, never a pasted plan.
        await ask(
            client,
            reviewer_pane,
            prompts.review_plan(paths.plan, paths.request, paths.review),
            turn_timeout_ms=turn_timeout_ms,
            attempts=attempts,
        )
        expect_file(paths.review, before, "reviewer")

        if prompts.approved_file(paths.review):
            console.log(f"approved in round {round_number}")
            return RoundOutcome(rounds=round_number, approved=True)

        if round_number >= max_rounds:
            console.log(f"round limit ({max_rounds}) reached with findings outstanding")
            return RoundOutcome(rounds=round_number, approved=False)

        round_number += 1
        console.log(f"round {round_number}: revising plan")
        before = mtime(paths.plan)
        await ask(
            client,
            planner_pane,
            prompts.revise_plan(paths.review, paths.plan),
            turn_timeout_ms=turn_timeout_ms,
            attempts=attempts,
        )
        expect_file(paths.plan, before, "planner")
