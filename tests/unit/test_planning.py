"""`wq plan` -- the plan <-> review loop.

The interesting cases are all about the loop *terminating correctly*: on approval, on the
round cap, and never on a stale file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from herdr_workflow import config as config_module
from herdr_workflow.config import Config
from herdr_workflow.errors import WorkflowError
from herdr_workflow.herdr import delivery
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.workflows import planning, prompts
from tests.fake_herdr import FakeHerdr


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "POLL_INTERVAL", 0.001)
    monkeypatch.setattr(delivery, "SETTLE_CONFIRM_DELAY", 0.001)


def _pane(pane_id: str) -> dict[str, Any]:
    return {
        "pane_id": pane_id,
        "terminal_id": f"t-{pane_id}",
        "workspace_id": "w1",
        "tab_id": "w1:t1",
        "focused": False,
        "agent_status": "idle",
        "revision": 1,
    }


def _agent(pane_id: str, status: str = "idle", seq: int = 4) -> dict[str, Any]:
    return {
        "type": "agent_info",
        "agent": {
            "pane_id": pane_id,
            "terminal_id": "t1",
            "workspace_id": "w1",
            "tab_id": "w1:t1",
            "focused": False,
            "agent_status": status,
            "revision": 1,
            "state_change_seq": seq,
            "interactive_ready": True,
        },
    }


class Scenario:
    """Drives the fake so each prompt writes the file the loop expects next.

    Real agents write plan.md and review.md; here the fake does it on `agent.prompt`, which
    is what lets the loop's control flow be tested without a model.
    """

    def __init__(self, fake: FakeHerdr, paths: planning.PlanPaths, reviews: list[str]) -> None:
        self.fake = fake
        self.paths = paths
        self.reviews = list(reviews)
        self.seq = 4
        self.prompts: list[str] = []
        self.plan_writes = 0

        fake.on(
            "workspace.create",
            {
                "type": "workspace_created",
                "workspace": {
                    "workspace_id": "w1",
                    "number": 1,
                    "label": "plan-x",
                    "focused": False,
                    "pane_count": 1,
                    "tab_count": 1,
                    "active_tab_id": "w1:t1",
                    "agent_status": "unknown",
                },
                "tab": {
                    "tab_id": "w1:t1",
                    "workspace_id": "w1",
                    "number": 1,
                    "label": "plan-x",
                    "focused": False,
                    "pane_count": 1,
                    "agent_status": "unknown",
                },
                "root_pane": _pane("w1:p1"),
            },
        )
        fake.on("pane.split", {"type": "pane_info", "pane": _pane("w1:p2")})
        for method in ("tab.rename", "pane.rename", "agent.start", "notification.show"):
            fake.on(method, {"type": "ok"})
        fake.on("agent.get", self._on_get)
        fake.on("agent.read", {"type": "agent_read", "read": {"pane_id": "w1:p1", "text": "$ "}})
        fake.on("agent.wait", self._on_wait)
        fake.on("agent.prompt", self._on_prompt)

    def _on_get(self, _params: dict[str, Any]) -> dict[str, Any]:
        return _agent("w1:p1", "idle", self.seq)

    def _on_wait(self, _params: dict[str, Any]) -> dict[str, Any]:
        return _agent("w1:p1", "done", self.seq)

    def _touch(self, path: Path, body: str) -> None:
        before = path.stat().st_mtime if path.exists() else 0.0
        path.write_text(body)
        os.utime(path, (before + 10, before + 10))

    def _on_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        text = params["text"]
        self.prompts.append(text)
        # The receipt: every accepted prompt advances the sequence.
        self.seq += 1
        if str(self.paths.review) in text and "Review the plan" in text:
            body = self.reviews.pop(0) if self.reviews else prompts.VERDICT_APPROVED
            self._touch(self.paths.review, f"findings\n\n{body}")
        else:
            self.plan_writes += 1
            self._touch(self.paths.plan, f"plan revision {self.plan_writes}")
        return _agent("w1:p1", "idle", self.seq - 1)


def _config(tmp_path: Path, rounds: int = 3) -> Config:
    from dataclasses import replace

    base = Config()
    return replace(
        base,
        paths=replace(base.paths, root=tmp_path),
        loops=replace(base.loops, plan_rounds=rounds, turn_timeout_ms=1000),
    )


# -- the loop ----------------------------------------------------------------


async def test_approval_in_round_one_stops_immediately(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    paths = planning.PlanPaths.for_slug(tmp_path, "x")
    scenario = Scenario(fake, paths, reviews=[prompts.VERDICT_APPROVED])

    result = await planning.plan(client, config, "x", "do the thing")
    assert result.approved is True
    assert result.rounds == 1
    # One draft, one review, and no revision turn.
    assert scenario.plan_writes == 1


async def test_a_changes_verdict_triggers_a_revision_round(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    paths = planning.PlanPaths.for_slug(tmp_path, "x")
    scenario = Scenario(fake, paths, reviews=[prompts.VERDICT_CHANGES, prompts.VERDICT_APPROVED])

    result = await planning.plan(client, config, "x", "do the thing")
    assert result.approved is True
    assert result.rounds == 2
    assert scenario.plan_writes == 2  # draft + one revision
    assert any("Address every BLOCKING finding" in p for p in scenario.prompts)


async def test_the_round_cap_stops_a_loop_that_never_converges(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    """Two agents that disagree can disagree forever, and an unbounded loop spends real
    money doing it."""
    config = _config(tmp_path, rounds=2)
    paths = planning.PlanPaths.for_slug(tmp_path, "x")
    Scenario(fake, paths, reviews=[prompts.VERDICT_CHANGES] * 10)

    result = await planning.plan(client, config, "x", "do the thing")
    assert result.approved is False
    assert result.rounds == 2


async def test_the_round_cap_is_configurable(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    config = _config(tmp_path, rounds=1)
    paths = planning.PlanPaths.for_slug(tmp_path, "x")
    Scenario(fake, paths, reviews=[prompts.VERDICT_CHANGES] * 10)

    result = await planning.plan(client, config, "x", "do the thing")
    assert result.rounds == 1
    assert result.approved is False


async def test_wq_plan_rounds_env_var_is_honoured(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WQ_PLAN_ROUNDS", "1")
    monkeypatch.setenv("WQ_ROOT", str(tmp_path))
    config = config_module.load(None)
    paths = planning.PlanPaths.for_slug(tmp_path, "x")
    Scenario(fake, paths, reviews=[prompts.VERDICT_CHANGES] * 10)

    result = await planning.plan(client, config, "x", "do the thing")
    assert result.rounds == 1


# -- setup and outputs -------------------------------------------------------


async def test_the_request_is_written_for_the_agents_to_read(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    """Both panes are given a path to the request, never the request text inline."""
    config = _config(tmp_path)
    paths = planning.PlanPaths.for_slug(tmp_path, "x")
    scenario = Scenario(fake, paths, reviews=[prompts.VERDICT_APPROVED])

    await planning.plan(client, config, "x", "add authentication")
    assert paths.request.read_text().strip() == "add authentication"
    assert not any("add authentication" in p for p in scenario.prompts)


async def test_planner_and_reviewer_get_their_own_panes_and_roles(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    paths = planning.PlanPaths.for_slug(tmp_path, "x")
    Scenario(fake, paths, reviews=[prompts.VERDICT_APPROVED])

    await planning.plan(client, config, "x", "do the thing")

    starts = fake.calls("agent.start")
    assert len(starts) == 2
    panes = {c["params"]["pane_id"] for c in starts}
    assert panes == {"w1:p1", "w1:p2"}
    # The reviewer must not be the model that wrote -- the loop is pointless otherwise.
    kinds = {c["params"]["pane_id"]: c["params"]["args"] for c in starts}
    assert kinds["w1:p1"] != kinds["w1:p2"]

    labels = {c["params"]["label"] for c in fake.calls("pane.rename")}
    assert labels == {"plan", "review"}


async def test_the_tab_is_named_task(client: HerdrClient, fake: FakeHerdr, tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = planning.PlanPaths.for_slug(tmp_path, "x")
    Scenario(fake, paths, reviews=[prompts.VERDICT_APPROVED])

    await planning.plan(client, config, "x", "do the thing")
    assert fake.calls("tab.rename")[0]["params"]["label"] == "task"


async def test_a_planner_that_writes_nothing_fails_the_round(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    """Behavior #9: terminal state is a heuristic, a file on disk is not."""
    config = _config(tmp_path)
    paths = planning.PlanPaths.for_slug(tmp_path, "x")
    scenario = Scenario(fake, paths, reviews=[prompts.VERDICT_APPROVED])

    # Agent claims to finish but writes no file.
    def silent(params: dict[str, Any]) -> dict[str, Any]:
        scenario.seq += 1
        return _agent("w1:p1", "idle", scenario.seq - 1)

    fake.on("agent.prompt", silent)

    with pytest.raises(WorkflowError) as caught:
        await planning.plan(client, config, "x", "do the thing")
    assert "was not written" in caught.value.message


async def test_a_notification_failure_does_not_fail_the_command(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    """The work is already done; a toast that cannot be shown must not undo it."""
    config = _config(tmp_path)
    paths = planning.PlanPaths.for_slug(tmp_path, "x")
    Scenario(fake, paths, reviews=[prompts.VERDICT_APPROVED])
    fake.on_error("notification.show", "unsupported", "no notifier")

    result = await planning.plan(client, config, "x", "do the thing")
    assert result.approved is True
