"""Behaviors #5 (`done` does not persist) and #9 (confirm output files), and the
convergence rule the review loop turns on."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from herdr_workflow.errors import WorkflowError
from herdr_workflow.herdr import delivery
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.workflows import prompts
from herdr_workflow.workflows.loops import expect_file, mtime
from tests.fake_herdr import FakeHerdr


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "POLL_INTERVAL", 0.001)
    monkeypatch.setattr(delivery, "SETTLE_CONFIRM_DELAY", 0.001)


def _info(status: str, seq: int = 5) -> dict[str, Any]:
    return {
        "type": "agent_info",
        "agent": {
            "pane_id": "w1:p1",
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


# -- behavior #5: `done` does not persist ------------------------------------


async def test_done_is_a_finished_turn(client: HerdrClient, fake: FakeHerdr) -> None:
    fake.on("agent.wait", _info("done"))
    assert await delivery.wait_settled(client, "w1:p1", 1000) == "done"


async def test_idle_counts_as_finished_once_it_holds(client: HerdrClient, fake: FakeHerdr) -> None:
    """Measured: `done` held ~11s then became `idle` with the sequence unchanged.

    Waiting on `done` alone would hang for the full turn timeout -- 30 minutes by default
    -- on work that finished minutes ago. `idle` is unambiguous here only because
    `deliver` already proved the prompt was taken.
    """
    fake.on("agent.wait", _info("idle"))
    fake.on("agent.get", _info("idle"))
    assert await delivery.wait_settled(client, "w1:p1", 1000) == "idle"


async def test_a_flash_of_idle_mid_turn_does_not_end_the_wait(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """A pane can flash idle between steps of a single turn, so idle is confirmed by
    holding rather than taken on the first sample."""
    fake.on_sequence("agent.wait", [_info("idle"), _info("done")])
    fake.on("agent.get", _info("working"))  # the flash was not real

    assert await delivery.wait_settled(client, "w1:p1", 5000) == "done"
    assert len(fake.calls("agent.wait")) == 2


async def test_blocked_is_returned_rather_than_waited_out(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    fake.on("agent.wait", _info("blocked"))
    assert await delivery.wait_settled(client, "w1:p1", 1000) == "blocked"


async def test_a_turn_that_never_settles_reports_the_timeout(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    fake.on("agent.wait", _info("idle"))
    fake.on("agent.get", _info("working"))
    with pytest.raises(WorkflowError) as caught:
        await delivery.wait_settled(client, "w1:p1", 1)
    assert "never finished its turn" in caught.value.message


async def test_ask_surfaces_a_blocked_agent_with_its_screen(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """`blocked` means the agent stopped to ask something. Surfacing it as itself beats
    letting it fall through as a mysteriously missing output file."""
    fake.on(
        "agent.read", {"type": "agent_read", "read": {"pane_id": "w1:p1", "text": "Which one? "}}
    )
    fake.on_sequence("agent.get", [_info("idle", 4), _info("idle", 4), _info("working", 5)])
    fake.on("agent.prompt", _info("idle", 4))
    fake.on("agent.wait", _info("blocked"))

    with pytest.raises(WorkflowError) as caught:
        await delivery.ask(client, "w1:p1", "go", turn_timeout_ms=1000)
    assert "waiting for input" in caught.value.message
    assert "Which one?" in (caught.value.why or "")


# -- behavior #9: confirm the file ------------------------------------------


def test_a_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(WorkflowError) as caught:
        expect_file(tmp_path / "plan.md", 0.0, "planner")
    assert "was not written" in caught.value.message


def test_an_empty_file_is_an_error(tmp_path: Path) -> None:
    target = tmp_path / "plan.md"
    target.write_text("")
    with pytest.raises(WorkflowError):
        expect_file(target, 0.0, "planner")


def test_a_stale_file_is_an_error(tmp_path: Path) -> None:
    """Existence alone passes on a file left by the previous round, which is how a loop
    spins without anyone noticing. The mtime is what makes it about *this* turn."""
    target = tmp_path / "plan.md"
    target.write_text("old content")
    before = mtime(target)
    with pytest.raises(WorkflowError) as caught:
        expect_file(target, before, "planner")
    assert "was not updated this round" in caught.value.message


def test_a_freshly_written_file_passes(tmp_path: Path) -> None:
    target = tmp_path / "plan.md"
    target.write_text("old")
    before = mtime(target)
    target.write_text("new")
    os.utime(target, (before + 10, before + 10))
    expect_file(target, before, "planner")


# -- convergence -------------------------------------------------------------


def test_an_exact_verdict_line_approves() -> None:
    assert prompts.approved("Findings: none.\n\nVERDICT: APPROVED")


def test_trailing_whitespace_is_tolerated() -> None:
    assert prompts.approved("VERDICT: APPROVED   ")


def test_changes_is_not_approval() -> None:
    assert not prompts.approved("VERDICT: CHANGES")


def test_prose_about_approval_is_not_approval() -> None:
    """The whole point of a machine-readable verdict: convergence is a grep, not an
    interpretation. A reviewer musing about approval has not approved."""
    assert not prompts.approved("I would be inclined to say VERDICT: APPROVED if you fixed X.")
    assert not prompts.approved("This looks approved to me, broadly.")


@pytest.mark.parametrize(
    "ending",
    ["\n", "\n\n", "\n\n\n", "\r\n\r\n"],
)
def test_trailing_blank_lines_are_tolerated(ending: str) -> None:
    assert prompts.approved(f"VERDICT: APPROVED{ending}")


def test_text_after_the_verdict_is_not_approval() -> None:
    assert not prompts.approved("VERDICT: APPROVED\n\nBLOCKING: missed finding")
    assert not prompts.approved("VERDICT: APPROVED\n\n  BLOCKING: missed finding")


def test_an_empty_review_is_not_approval() -> None:
    assert not prompts.approved("")


def test_the_review_protocol_names_both_verdicts() -> None:
    """The reviewer cannot emit the line it was never told about."""
    assert prompts.VERDICT_APPROVED in prompts.REVIEW_PROTOCOL
    assert prompts.VERDICT_CHANGES in prompts.REVIEW_PROTOCOL
    assert "BLOCKING" in prompts.REVIEW_PROTOCOL


def test_prompts_pass_paths_not_content(tmp_path: Path) -> None:
    """Passing content between panes is the largest token sink in a loop like this, and it
    grows every round."""
    plan = tmp_path / "plan.md"
    plan.write_text("SECRET PLAN BODY")
    text = prompts.review_plan(plan, tmp_path / "request.md", tmp_path / "review.md")
    assert str(plan) in text
    assert "SECRET PLAN BODY" not in text


def test_approved_file_of_a_missing_file_is_false(tmp_path: Path) -> None:
    assert prompts.approved_file(tmp_path / "absent.md") is False
