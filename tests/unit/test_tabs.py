"""`wq chat`, `wq ask`, `wq tidy`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from herdr_workflow.config import Config
from herdr_workflow.errors import WorkflowError
from herdr_workflow.herdr import delivery
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.workflows import tabs
from tests.fake_herdr import FakeHerdr


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "POLL_INTERVAL", 0.001)


def _ws(label: str, ws_id: str) -> dict[str, Any]:
    return {
        "workspace_id": ws_id,
        "number": 1,
        "label": label,
        "focused": False,
        "pane_count": 1,
        "tab_count": 1,
        "active_tab_id": f"{ws_id}:t1",
        "agent_status": "idle",
    }


def _tab(tab_id: str, ws_id: str, label: str, status: str = "idle") -> dict[str, Any]:
    return {
        "tab_id": tab_id,
        "workspace_id": ws_id,
        "number": 1,
        "label": label,
        "focused": False,
        "pane_count": 1,
        "agent_status": status,
    }


def _pane(pane_id: str, ws_id: str, tab_id: str) -> dict[str, Any]:
    return {
        "pane_id": pane_id,
        "terminal_id": f"t-{pane_id}",
        "workspace_id": ws_id,
        "tab_id": tab_id,
        "focused": False,
        "agent_status": "idle",
        "revision": 1,
    }


def _snapshot(**parts: Any) -> dict[str, Any]:
    return {
        "type": "session_snapshot",
        "snapshot": {
            "workspaces": parts.get("workspaces", []),
            "tabs": parts.get("tabs", []),
            "panes": parts.get("panes", []),
            "agents": parts.get("agents", []),
            "protocol": 17,
            "version": "0.0.0-test",
        },
    }


def _agent_info(seq: int) -> dict[str, Any]:
    return {
        "type": "agent_info",
        "agent": {
            "pane_id": "w1:p2",
            "terminal_id": "t2",
            "workspace_id": "w1",
            "tab_id": "w1:t2",
            "focused": False,
            "agent_status": "idle" if seq == 4 else "working",
            "revision": 1,
            "state_change_seq": seq,
            "interactive_ready": True,
        },
    }


def _delivery_ok(fake: FakeHerdr) -> None:
    """Script a successful prompt delivery: seq moves on the first poll."""
    fake.on("agent.read", {"type": "agent_read", "read": {"pane_id": "w1:p2", "text": "$ "}})
    fake.on_sequence("agent.get", [_agent_info(4), _agent_info(4), _agent_info(5)])
    fake.on("agent.prompt", _agent_info(4))
    fake.on("agent.send_keys", {"type": "ok"})
    fake.on("pane.focus", {"type": "ok"})


# -- chat --------------------------------------------------------------------


async def test_chat_creates_the_tab_once_then_reuses_it(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    """Follow-ups only make sense with the previous turn in context, and a fresh tab per
    question would bury the workspace in tabs."""
    fake.on("session.snapshot", _snapshot(workspaces=[_ws("inbox", "w1")]))
    fake.on(
        "tab.create",
        {
            "type": "tab_created",
            "tab": _tab("w1:t2", "w1", "chat"),
            "root_pane": _pane("w1:p2", "w1", "w1:t2"),
        },
    )
    fake.on("agent.start", {"type": "agent_started"})
    fake.on("pane.rename", {"type": "ok"})
    _delivery_ok(fake)

    first = await tabs.chat(client, Config(), "hello", tmp_path)
    assert first.created_tab is True

    # Second call: the tab now exists in the snapshot.
    fake.on(
        "session.snapshot",
        _snapshot(
            workspaces=[_ws("inbox", "w1")],
            tabs=[_tab("w1:t2", "w1", "chat")],
            panes=[_pane("w1:p2", "w1", "w1:t2")],
        ),
    )
    fake.on_sequence("agent.get", [_agent_info(4), _agent_info(4), _agent_info(5)])

    second = await tabs.chat(client, Config(), "and again", tmp_path)
    assert second.created_tab is False
    assert len(fake.calls("tab.create")) == 1
    assert len(fake.calls("agent.start")) == 1


async def test_chat_without_an_inbox_says_how_to_make_one(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    fake.on("session.snapshot", _snapshot())
    with pytest.raises(WorkflowError) as caught:
        await tabs.chat(client, Config(), "hello", tmp_path)
    assert caught.value.fix == "create it with: wq up"


async def test_chat_rejects_an_empty_message(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    with pytest.raises(WorkflowError):
        await tabs.chat(client, Config(), "   ", tmp_path)


async def test_chat_confirms_delivery(client: HerdrClient, fake: FakeHerdr, tmp_path: Path) -> None:
    """Fire-and-forget on the answer, but not on the prompt: a dropped prompt in a tab you
    are not watching is exactly the failure that hides."""
    fake.on(
        "session.snapshot",
        _snapshot(
            workspaces=[_ws("inbox", "w1")],
            tabs=[_tab("w1:t2", "w1", "chat")],
            panes=[_pane("w1:p2", "w1", "w1:t2")],
        ),
    )
    fake.on("agent.read", {"type": "agent_read", "read": {"pane_id": "w1:p2", "text": "$ "}})
    fake.on("agent.get", _agent_info(4))  # never moves
    fake.on("agent.prompt", _agent_info(4))
    fake.on("agent.send_keys", {"type": "ok"})

    with pytest.raises(WorkflowError) as caught:
        await tabs.chat(client, Config(), "hello", tmp_path)
    assert "never took the prompt" in caught.value.message


# -- ask ---------------------------------------------------------------------


async def test_ask_makes_a_timestamped_tab_scoped_to_the_directory(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    fake.on("session.snapshot", _snapshot(workspaces=[_ws("inbox", "w1")]))
    fake.on(
        "tab.create",
        {
            "type": "tab_created",
            "tab": _tab("w1:t3", "w1", "ask-120000"),
            "root_pane": _pane("w1:p2", "w1", "w1:t3"),
        },
    )
    fake.on("agent.start", {"type": "agent_started"})
    fake.on("pane.rename", {"type": "ok"})
    _delivery_ok(fake)

    result = await tabs.ask(client, Config(), "where is auth handled?", tmp_path)
    assert result.tab_label.startswith("ask-")
    params = fake.calls("tab.create")[0]["params"]
    assert params["cwd"] == str(tmp_path)
    assert params["label"].startswith("ask-")


async def test_every_ask_gets_its_own_tab(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    """The file reads behind an answer are what would otherwise accumulate."""
    fake.on("session.snapshot", _snapshot(workspaces=[_ws("inbox", "w1")]))
    fake.on(
        "tab.create",
        {
            "type": "tab_created",
            "tab": _tab("w1:t3", "w1", "ask-1"),
            "root_pane": _pane("w1:p2", "w1", "w1:t3"),
        },
    )
    fake.on("agent.start", {"type": "agent_started"})
    fake.on("pane.rename", {"type": "ok"})
    _delivery_ok(fake)
    await tabs.ask(client, Config(), "one", tmp_path)

    fake.on_sequence("agent.get", [_agent_info(4), _agent_info(4), _agent_info(5)])
    await tabs.ask(client, Config(), "two", tmp_path)

    assert len(fake.calls("tab.create")) == 2
    assert len(fake.calls("agent.start")) == 2


# -- tidy --------------------------------------------------------------------


async def test_tidy_closes_finished_ask_tabs_only(client: HerdrClient, fake: FakeHerdr) -> None:
    fake.on(
        "session.snapshot",
        _snapshot(
            workspaces=[_ws("inbox", "w1")],
            tabs=[
                _tab("w1:t1", "w1", "router"),
                _tab("w1:t2", "w1", "chat"),
                _tab("w1:t3", "w1", "ask-120000", "idle"),
                _tab("w1:t4", "w1", "ask-120500", "done"),
                _tab("w1:t5", "w1", "ask-121000", "working"),
            ],
        ),
    )
    fake.on("tab.close", {"type": "ok"})

    result = await tabs.tidy(client, Config())
    assert set(result.closed) == {"ask-120000", "ask-120500"}
    assert result.kept_working == ["ask-121000"]
    # router and chat are not ask tabs and must survive.
    closed_ids = {c["params"]["tab_id"] for c in fake.calls("tab.close")}
    assert closed_ids == {"w1:t3", "w1:t4"}


async def test_tidy_ignores_ask_tabs_in_other_workspaces(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    fake.on(
        "session.snapshot",
        _snapshot(
            workspaces=[_ws("inbox", "w1")],
            tabs=[_tab("w9:t1", "w9", "ask-120000", "idle")],
        ),
    )
    result = await tabs.tidy(client, Config())
    assert result.closed == []
    assert fake.calls("tab.close") == []


async def test_tidy_with_nothing_to_close_is_fine(client: HerdrClient, fake: FakeHerdr) -> None:
    fake.on("session.snapshot", _snapshot(workspaces=[_ws("inbox", "w1")]))
    result = await tabs.tidy(client, Config())
    assert result.closed == []
    assert result.kept_working == []


async def test_tidy_keeps_going_when_one_close_fails(client: HerdrClient, fake: FakeHerdr) -> None:
    fake.on(
        "session.snapshot",
        _snapshot(
            workspaces=[_ws("inbox", "w1")],
            tabs=[
                _tab("w1:t3", "w1", "ask-1", "idle"),
                _tab("w1:t4", "w1", "ask-2", "idle"),
            ],
        ),
    )
    fake.on_error("tab.close", "not_found", "gone")
    result = await tabs.tidy(client, Config())
    assert result.closed == []
    assert len(fake.calls("tab.close")) == 2
