"""`wq revise` -- one code turn, one review turn, and the guards in front of them.

Every test here reaches a real built state first, by running `build` against the same fake
that creates a real worktree and commits into it. Revising something that was never built
would test the happy path against a fixture rather than against what `build` actually
leaves behind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from herdr_workflow.config import Config
from herdr_workflow.errors import WorkflowError
from herdr_workflow.herdr import delivery
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.workflows import build_env, building, listing, prompts, revising
from tests.build_scenario import WS, Scenario, agent_info, build_config, with_plan
from tests.fake_herdr import FakeHerdr


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "POLL_INTERVAL", 0.001)
    monkeypatch.setattr(delivery, "SETTLE_CONFIRM_DELAY", 0.001)


async def _built(
    client: HerdrClient, fake: FakeHerdr, root: Path, repo: Path, reviews: list[str] | None = None
) -> tuple[Config, Scenario, building.BuildPaths]:
    """Run a build to completion, leaving panes, a worktree and a build.env behind."""
    config = build_config(root)
    paths = with_plan(root)
    scenario = Scenario(
        fake, paths, repo, reviews=reviews or [prompts.VERDICT_APPROVED, prompts.VERDICT_APPROVED]
    )
    await building.build(client, config, "x", repo)
    return config, scenario, paths


# -- the round ---------------------------------------------------------------


async def test_one_code_turn_and_one_review_turn(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """`build` runs a bounded loop; from here you are the round counter. A reviewer with
    findings must not trigger an automatic fix turn."""
    root = tmp_path / "wq"
    config, scenario, _paths = await _built(
        client, fake, root, repo, [prompts.VERDICT_APPROVED, prompts.VERDICT_CHANGES]
    )
    turns_before = scenario.code_turns
    scenario.prompts.clear()

    result = await revising.revise(client, config, "x", "rename the function")

    # The reviewer had findings, and nothing went back to the code pane to fix them.
    assert result.approved is False
    assert scenario.code_turns == turns_before + 1
    assert [p for p, _t in scenario.prompts] == [f"{WS}:p1", f"{WS}:p2"]


async def test_the_delta_is_what_this_turn_changed(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """`revise.patch` is two-dot from the pre-turn HEAD; `diff.patch` is the whole branch."""
    root = tmp_path / "wq"
    config, _scenario, paths = await _built(client, fake, root, repo)

    await revising.revise(client, config, "x", "add another one")

    delta = paths.delta.read_text()
    diff = paths.diff.read_text()
    # The build committed change1.py; the revise turn committed change2.py.
    assert "change2.py" in delta
    assert "change1.py" not in delta
    assert "change1.py" in diff
    assert "change2.py" in diff


async def test_the_reviewer_gets_the_delta_the_full_diff_and_the_plan(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """Scoped by instruction, not by withholding: a reviewer given only the delta is
    structurally blind to the call site the delta forgot to update."""
    root = tmp_path / "wq"
    config, scenario, paths = await _built(client, fake, root, repo)
    scenario.prompts.clear()

    await revising.revise(client, config, "x", "tighten the types")

    review_prompt = next(t for p, t in scenario.prompts if p == f"{WS}:p2")
    assert str(paths.delta) in review_prompt
    assert str(paths.diff) in review_prompt
    assert str(paths.plan) in review_prompt
    assert "tighten the types" in review_prompt


async def test_the_comment_reaches_the_code_pane(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    config, scenario, _paths = await _built(client, fake, root, repo)
    scenario.prompts.clear()

    await revising.revise(client, config, "x", "use a dataclass")

    code_prompt = next(t for p, t in scenario.prompts if p == f"{WS}:p1")
    assert "Change request: use a dataclass" in code_prompt


async def test_a_code_pane_that_commits_nothing_fails_the_round(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    config, scenario, _paths = await _built(client, fake, root, repo)
    scenario.commits = False

    with pytest.raises(WorkflowError) as caught:
        await revising.revise(client, config, "x", "do nothing at all")
    assert "nothing new committed on wq/x" in caught.value.message


async def test_the_diff_is_rewritten_before_the_delta_check(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """`diff.patch`'s mtime is the `*` marker in `wq list`, and the router's `revise`
    default reads it -- so it must be refreshed even on a round that then fails."""
    root = tmp_path / "wq"
    config, scenario, paths = await _built(client, fake, root, repo)
    scenario.commits = False
    paths.diff.unlink()

    with pytest.raises(WorkflowError):
        await revising.revise(client, config, "x", "do nothing at all")
    assert paths.diff.is_file()


async def test_a_revised_build_is_the_one_wq_list_marks(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """The router contract: `revise` defaults to the slug marked `*`, and `revise` itself
    is what keeps that marker current."""
    root = tmp_path / "wq"
    config, _scenario, _paths = await _built(client, fake, root, repo)

    await revising.revise(client, config, "x", "one more change")

    result = await listing.collect(client, root)
    assert result.current == "x"


async def test_a_master_repo_can_be_built_and_then_revised(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, master_repo: Path
) -> None:
    """The repository Bash's hard-coded `origin/main` could never touch, end to end.

    `build` resolves the base in the parent checkout and records it; `revise` reads it back
    and diffs in the *linked worktree*, a different directory. A linked worktree shares
    `.git` with its parent so `origin/master` resolves there too -- but that is worth
    proving rather than assuming, because a base that fails to resolve here would produce
    an empty delta and read as "the agent changed nothing".
    """
    root = tmp_path / "wq"
    config, _scenario, paths = await _built(client, fake, root, master_repo)
    assert build_env.read(paths.env).base == "origin/master"

    await revising.revise(client, config, "x", "and one more")

    assert paths.delta.read_text().strip()
    assert "change2.py" in paths.delta.read_text()
    assert "change1.py" in paths.diff.read_text()


async def test_approval_is_reported_without_stopping_anything(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    config, _scenario, _paths = await _built(client, fake, root, repo)

    result = await revising.revise(client, config, "x", "polish it")
    assert result.approved is True


# -- the guards --------------------------------------------------------------


async def test_an_empty_comment_is_refused(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    config, scenario, _paths = await _built(client, fake, root, repo)
    scenario.prompts.clear()

    with pytest.raises(WorkflowError) as caught:
        await revising.revise(client, config, "x", "   ")
    assert "describe the change you want" in caught.value.message
    assert scenario.prompts == []


async def test_a_slug_that_was_never_built_says_so(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    config = build_config(tmp_path / "wq")
    with pytest.raises(WorkflowError) as caught:
        await revising.revise(client, config, "never", "change something")
    assert "no build for never" in caught.value.message
    assert "wq build never" in (caught.value.fix or "")


async def test_a_worktree_that_is_gone_is_a_different_error_from_panes_that_are_gone(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """Both mean "cannot revise", and they want opposite remedies: a missing worktree means
    the slug was shipped or cleaned, missing panes mean the build can be re-run."""
    import shutil

    root = tmp_path / "wq"
    config, _scenario, paths = await _built(client, fake, root, repo)
    shutil.rmtree(f"{repo}-worktrees/x")

    with pytest.raises(WorkflowError) as caught:
        await revising.revise(client, config, "x", "change something")
    assert "is gone" in caught.value.message
    assert "shipped or cleaned" in (caught.value.why or "")
    assert paths.env.is_file()  # not treated as an unbuilt slug


async def test_a_closed_workspace_says_to_re_run_the_build(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    config, scenario, _paths = await _built(client, fake, root, repo)
    scenario.workspaces.clear()  # the user closed it

    with pytest.raises(WorkflowError) as caught:
        await revising.revise(client, config, "x", "change something")
    assert "no live build panes for x" in caught.value.message
    assert "wq build x" in (caught.value.fix or "")


@pytest.mark.parametrize("status", ["working", "blocked"])
async def test_a_pane_mid_turn_refuses_rather_than_interleaving(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path, status: str
) -> None:
    """Two revises landing in the same panes interleave their prompts and lose one of them
    silently. A pane mid-turn is not idle, so this is cheap to rule out."""
    root = tmp_path / "wq"
    config, scenario, _paths = await _built(client, fake, root, repo)
    scenario.prompts.clear()

    def mid_turn(_params: dict[str, Any]) -> dict[str, Any]:
        return agent_info(f"{WS}:p1", status, 9)

    fake.on("agent.get", mid_turn)

    with pytest.raises(WorkflowError) as caught:
        await revising.revise(client, config, "x", "change something")
    assert "a turn is still in flight" in caught.value.message
    assert scenario.prompts == []


async def test_an_agent_that_is_really_gone_is_fatal(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """`delivery` waits `unknown` out because registration is asynchronous. Here the agent
    was started by `build` minutes ago, so a pane that really has no agent is not going to
    grow one -- waiting would burn every retry on a pane that cannot answer."""
    root = tmp_path / "wq"
    config, _scenario, _paths = await _built(client, fake, root, repo)
    fake.on_error("agent.get", "no_agent", "no agent in pane")

    with pytest.raises(WorkflowError) as caught:
        await revising.revise(client, config, "x", "change something")
    assert "is gone" in caught.value.message
    assert "wq build x" in (caught.value.fix or "")


async def test_a_single_failed_read_does_not_condemn_a_live_agent(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
) -> None:
    """`agent_state` reports `unknown` for *any* failed `agent.get`, transient errors
    included, so one sample cannot tell "gone" from "the call failed once".

    Declaring it gone throws away a worktree with committed work in it. Rule of thumb #9:
    distinguish "not there" from "never coming" by a second read, not a single one.
    """
    monkeypatch.setattr(revising, "UNKNOWN_RECHECK", 0.001)
    root = tmp_path / "wq"
    config, scenario, _paths = await _built(client, fake, root, repo)

    calls = {"n": 0}

    def flaky(params: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"__error__": {"code": "internal", "message": "transient"}}
        # Then behave normally -- `scenario.seq` is what makes delivery confirmable.
        return agent_info(params["target"], "idle", scenario.seq)

    fake.on("agent.get", flaky)

    result = await revising.revise(client, config, "x", "change something")
    assert result.approved is True
    assert scenario.code_turns == 2  # the build's turn, and this one


async def test_the_review_pane_is_checked_too(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """Both panes are guarded. A busy reviewer loses its prompt just as silently."""
    root = tmp_path / "wq"
    config, scenario, _paths = await _built(client, fake, root, repo)
    scenario.prompts.clear()

    def by_pane(params: dict[str, Any]) -> dict[str, Any]:
        pane = params["target"]
        return agent_info(pane, "working" if pane == f"{WS}:p2" else "idle", 9)

    fake.on("agent.get", by_pane)

    with pytest.raises(WorkflowError) as caught:
        await revising.revise(client, config, "x", "change something")
    assert f"{WS}:p2" in (caught.value.why or "")
    assert scenario.prompts == []
