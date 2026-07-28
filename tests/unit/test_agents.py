"""Agent startup: behaviors #3 (agent_pane_busy retry) and #4 (global agent names)."""

from __future__ import annotations

from typing import Any

import pytest

from herdr_workflow.config import AgentSpec, Config
from herdr_workflow.errors import ApiError, WorkflowError
from herdr_workflow.herdr import agents
from herdr_workflow.herdr.client import HerdrClient
from tests.fake_herdr import FakeHerdr

CLAUDE = AgentSpec("claude", "opus", "high")
PI = AgentSpec("pi", "openai-codex/gpt-5.6-sol", "medium")


def test_agent_name_is_qualified_by_pane_id() -> None:
    """Behavior #4: names are global in herdr, so two workspaces both starting a
    `review` pane would collide with agent_name_taken."""
    assert agents.qualified_name("review", "w2:p3") == "review-w2-p3"


def test_agent_name_is_lowercased_and_filtered() -> None:
    """herdr rejects anything outside [a-z0-9_-]."""
    name = agents.qualified_name("review", "W2:P3.x")
    assert name == "review-w2-p3-x"
    assert all(c.isalnum() or c in "-_" for c in name)


def test_claude_gets_a_permission_mode_and_pi_does_not() -> None:
    """Claude Code prompts on first tool use, which stalls an unattended loop
    immediately. Pi does not gate tools this way."""
    config = Config()
    claude_args = agents.spec_args(CLAUDE, config)
    assert "--permission-mode" in claude_args
    assert "--effort" in claude_args
    pi_args = agents.spec_args(PI, config)
    assert "--permission-mode" not in pi_args
    assert "--thinking" in pi_args


async def test_start_agent_retries_agent_pane_busy(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behavior #3: a freshly split pane is not at a shell prompt yet."""
    monkeypatch.setattr(agents, "BUSY_DELAY", 0.001)
    fake.on_sequence(
        "agent.start",
        [
            {"__error__": {"code": "agent_pane_busy", "message": "busy"}},
            {"__error__": {"code": "agent_pane_busy", "message": "busy"}},
            {"type": "agent_started"},
        ],
    )
    fake.on("pane.rename", {"type": "ok"})

    name = await agents.start_agent(client, "w1:p1", PI, "router", Config())
    assert name == "router-w1-p1"
    assert len(fake.calls("agent.start")) == 3


async def test_start_agent_does_not_retry_other_errors(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """A blanket retry turns a typo in a model name into a long hang."""
    fake.on_error("agent.start", "unknown_agent_kind", "no such kind")
    with pytest.raises(ApiError):
        await agents.start_agent(client, "w1:p1", PI, "router", Config())
    assert len(fake.calls("agent.start")) == 1


async def test_start_agent_gives_up_with_an_attach_hint(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agents, "BUSY_DELAY", 0.001)
    monkeypatch.setattr(agents, "BUSY_ATTEMPTS", 3)
    fake.on_error("agent.start", "agent_pane_busy", "busy")
    with pytest.raises(WorkflowError) as caught:
        await agents.start_agent(client, "w1:p1", PI, "router", Config())
    assert "herdr agent attach w1:p1" in (caught.value.fix or "")


async def test_pane_is_labelled_with_the_short_role_name(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """Panes are found by label later, so the rename is how discovery works."""
    fake.on("agent.start", {"type": "agent_started"})
    fake.on("pane.rename", {"type": "ok"})

    await agents.start_agent(client, "w1:p1", PI, "review", Config())
    rename = fake.calls("pane.rename")[0]["params"]
    assert rename == {"pane_id": "w1:p1", "label": "review"}


async def test_a_failed_rename_does_not_fail_the_start(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """A missing label degrades discovery; it does not break the agent."""
    fake.on("agent.start", {"type": "agent_started"})
    fake.on_error("pane.rename", "not_found", "gone")
    assert await agents.start_agent(client, "w1:p1", PI, "review", Config())


async def test_agent_args_reach_the_wire(client: HerdrClient, fake: FakeHerdr) -> None:
    captured: dict[str, Any] = {}

    def handler(params: dict[str, Any]) -> dict[str, str]:
        captured.update(params)
        return {"type": "agent_started"}

    fake.on("agent.start", handler)
    fake.on("pane.rename", {"type": "ok"})

    await agents.start_agent(client, "w1:p1", CLAUDE, "plan", Config())
    assert captured["kind"] == "claude"
    assert captured["pane_id"] == "w1:p1"
    assert captured["args"][:2] == ["--model", "opus"]
