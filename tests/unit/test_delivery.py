"""Prompt delivery: behaviors #1 (trust dialog) and #2 (delivery confirmation).

These are the tests that justify the whole rewrite. Against the Bash implementation the
only way to exercise any of this was a live agent, minutes and tokens per iteration. Here
each one is milliseconds.

The scripted responses mirror a real trace measured against herdr 0.7.5 -- see the module
docstring in herdr/delivery.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from herdr_workflow.errors import WorkflowError
from herdr_workflow.herdr import delivery
from herdr_workflow.herdr.client import HerdrClient
from tests.fake_herdr import FakeHerdr


def _agent(status: str = "idle", seq: int = 4, ready: bool | None = True) -> dict[str, Any]:
    body: dict[str, Any] = {
        "pane_id": "w1:p1",
        "terminal_id": "t1",
        "workspace_id": "w1",
        "tab_id": "w1:t1",
        "focused": False,
        "agent_status": status,
        "revision": 1,
        "state_change_seq": seq,
    }
    if ready is not None:
        body["interactive_ready"] = ready
    return body


def _info(**kw: Any) -> dict[str, Any]:
    return {"type": "agent_info", "agent": _agent(**kw)}


def _screen(text: str) -> dict[str, Any]:
    return {"type": "agent_read", "read": {"pane_id": "w1:p1", "text": text}}


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real intervals are 1s; nothing here needs wall-clock time."""
    monkeypatch.setattr(delivery, "POLL_INTERVAL", 0.001)


BLANK = _screen("$ ")


# -- readiness ---------------------------------------------------------------


async def test_interactive_ready_absent_then_true(client: HerdrClient, fake: FakeHerdr) -> None:
    """Matches the measured trace: absent for ~2s after start, then True."""
    fake.on_sequence(
        "agent.get",
        [_info(ready=None), _info(ready=None), _info(ready=True)],
    )
    assert await delivery.wait_ready(client, "w1:p1", timeout=5.0) is True


async def test_absent_readiness_is_not_fatal(client: HerdrClient, fake: FakeHerdr) -> None:
    """herdr omits the field entirely in some states. Waiting forever on it would hang a
    workflow that could have proceeded -- delivery confirmation is the real guard."""
    fake.on("agent.get", _info(ready=None))
    assert await delivery.wait_ready(client, "w1:p1", timeout=0.05) is False


async def test_registration_lag_is_tolerated(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agent.start` returning ok does not mean `agent.get` knows about the agent yet.

    Regression test: treating the first `unknown` as fatal made `wq chat` fail instantly
    on a tab it had just created. Found by running it against a real daemon.
    """
    monkeypatch.setattr(delivery, "UNKNOWN_GRACE", 5.0)
    fake.on_sequence(
        "agent.get",
        [
            {"__error__": {"code": "not_found", "message": "no agent"}},
            {"__error__": {"code": "not_found", "message": "no agent"}},
            _info(ready=None),
            _info(ready=True),
        ],
    )
    assert await delivery.wait_ready(client, "w1:p1", timeout=5.0) is True


async def test_a_pane_that_never_gets_an_agent_is_an_error(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinguished from registration lag by time: never once seen after the grace."""
    monkeypatch.setattr(delivery, "UNKNOWN_GRACE", 0.01)
    fake.on_error("agent.get", "not_found", "no agent")
    with pytest.raises(WorkflowError) as caught:
        await delivery.wait_ready(client, "w1:p1", timeout=5.0)
    assert "no agent in pane" in caught.value.message


# -- behavior #1: the trust dialog -------------------------------------------


async def test_trust_dialog_is_answered(client: HerdrClient, fake: FakeHerdr) -> None:
    """The pane reports ready while the only input it accepts is a dialog answer."""
    fake.on_sequence(
        "agent.read",
        [_screen(f"Do you trust?\n> {delivery.TRUST_DIALOG}\n  No"), BLANK],
    )
    fake.on("agent.send_keys", {"type": "ok"})

    assert await delivery.settle(client, "w1:p1") is True
    assert fake.calls("agent.send_keys")[0]["params"]["keys"] == ["enter"]


async def test_the_dialog_is_detected_in_a_real_captured_screen(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """The test above writes the string it then searches for, so it proves the loop works
    and nothing about whether `TRUST_DIALOG` matches what an agent actually draws.

    This one replays a screen captured from a live Claude Code starting in a fresh
    directory. It is what stops the constant drifting away from reality unnoticed -- and
    unnoticed is the whole danger, because a `settle` that silently matches nothing sends
    the prompt anyway and its Enter answers the security question.
    """
    captured = (Path(__file__).parent.parent / "fixtures" / "claude-trust-dialog.txt").read_text()
    assert delivery.TRUST_DIALOG in captured, (
        "TRUST_DIALOG no longer appears in the captured screen — recapture it "
        "(see tests/fixtures/README.md) and fix the constant"
    )

    fake.on_sequence("agent.read", [_screen(captured), BLANK])
    fake.on("agent.send_keys", {"type": "ok"})
    assert await delivery.settle(client, "w1:p1") is True


async def test_no_dialog_means_no_keys_sent(client: HerdrClient, fake: FakeHerdr) -> None:
    """A stray Enter into a live composer submits whatever is sitting in it."""
    fake.on("agent.read", BLANK)
    assert await delivery.settle(client, "w1:p1") is False
    assert fake.calls("agent.send_keys") == []


async def test_a_dialog_that_never_clears_is_reported(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(delivery, "SETTLE_ATTEMPTS", 3)
    fake.on("agent.read", _screen(delivery.TRUST_DIALOG))
    fake.on("agent.send_keys", {"type": "ok"})
    with pytest.raises(WorkflowError) as caught:
        await delivery.settle(client, "w1:p1")
    assert "herdr agent attach" in (caught.value.fix or "")


# -- behavior #2: delivery confirmation --------------------------------------


async def test_delivery_confirmed_when_seq_moves(client: HerdrClient, fake: FakeHerdr) -> None:
    """The measured receipt: seq 4 -> 5 once the agent takes the prompt."""
    fake.on("agent.read", BLANK)
    fake.on_sequence(
        "agent.get",
        [
            _info(seq=4),  # wait_ready
            _info(seq=4),  # before
            _info(status="working", seq=5),  # confirmation
        ],
    )
    fake.on("agent.prompt", _info(seq=4))  # pre-prompt state, as the real server returns

    await delivery.deliver(client, "w1:p1", "hello")
    assert len(fake.calls("agent.prompt")) == 1


async def test_the_prompt_response_is_not_used_as_the_receipt(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verified live: agent.prompt returns the PRE-prompt seq. Trusting it would report
    success for a prompt that was dropped."""
    monkeypatch.setattr(delivery, "DELIVERY_POLL_SECONDS", 2)
    fake.on("agent.read", BLANK)
    # The response says seq=4; every later poll also says 4, so nothing was delivered.
    fake.on("agent.get", _info(seq=4))
    fake.on("agent.prompt", _info(seq=4))
    fake.on("agent.send_keys", {"type": "ok"})

    with pytest.raises(WorkflowError):
        await delivery.deliver(client, "w1:p1", "hello", attempts=2)


async def test_dropped_prompt_is_retried_after_clearing_the_composer(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code draws a composer for tens of seconds while discarding input. The retry
    clears it with Esc first, or the second prompt lands after the first."""
    monkeypatch.setattr(delivery, "DELIVERY_POLL_SECONDS", 2)
    fake.on("agent.read", BLANK)
    fake.on("agent.prompt", _info(seq=4))
    fake.on("agent.send_keys", {"type": "ok"})
    fake.on_sequence(
        "agent.get",
        [
            _info(seq=4),  # wait_ready
            _info(seq=4),  # attempt 1 before
            _info(seq=4),  # attempt 1 poll
            _info(seq=4),  # attempt 1 poll
            _info(seq=4),  # attempt 1 final check
            _info(seq=4),  # attempt 2 before
            _info(status="working", seq=5),  # attempt 2 delivered
        ],
    )

    await delivery.deliver(client, "w1:p1", "hello", attempts=3)
    assert len(fake.calls("agent.prompt")) == 2
    assert ["esc"] in [c["params"]["keys"] for c in fake.calls("agent.send_keys")]


async def test_a_late_starting_turn_counts_as_delivered(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that starts late looks exactly like a dropped prompt until the last read.

    The status is compared against the one this attempt started from, so a warm pane
    sitting in `done` does not produce a false positive.
    """
    monkeypatch.setattr(delivery, "DELIVERY_POLL_SECONDS", 1)
    fake.on("agent.read", BLANK)
    fake.on("agent.prompt", _info(seq=6, status="done"))
    fake.on_sequence(
        "agent.get",
        [
            _info(status="done", seq=6),  # wait_ready
            _info(status="done", seq=6),  # before -- warm pane, previous turn finished
            _info(status="done", seq=6),  # poll: nothing yet
            _info(status="working", seq=6),  # final check: status changed after all
        ],
    )

    await delivery.deliver(client, "w1:p1", "hello", attempts=1)
    assert len(fake.calls("agent.prompt")) == 1


async def test_a_warm_done_pane_alone_does_not_prove_delivery(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inverse of the test above: `done` throughout means nothing moved."""
    monkeypatch.setattr(delivery, "DELIVERY_POLL_SECONDS", 1)
    fake.on("agent.read", BLANK)
    fake.on("agent.get", _info(status="done", seq=6))
    fake.on("agent.prompt", _info(status="done", seq=6))
    fake.on("agent.send_keys", {"type": "ok"})

    with pytest.raises(WorkflowError) as caught:
        await delivery.deliver(client, "w1:p1", "hello", attempts=2)
    assert "never took the prompt" in caught.value.message


async def test_a_rejected_prompt_fails_immediately(client: HerdrClient, fake: FakeHerdr) -> None:
    """herdr refusing the request is a different failure from the TUI dropping the text,
    and retrying it would just repeat the same rejection."""
    fake.on("agent.read", BLANK)
    fake.on("agent.get", _info())
    fake.on_error("agent.prompt", "agent_not_found", "no agent")

    with pytest.raises(WorkflowError) as caught:
        await delivery.deliver(client, "w1:p1", "hello")
    assert "agent_not_found" in (caught.value.why or "")
    assert len(fake.calls("agent.prompt")) == 1


async def test_the_dialog_is_cleared_before_every_attempt(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dialog can appear between attempts, and a prompt sent into it is swallowed
    while its Enter confirms the highlighted option."""
    monkeypatch.setattr(delivery, "DELIVERY_POLL_SECONDS", 1)
    fake.on_sequence(
        "agent.read",
        [BLANK, _screen(delivery.TRUST_DIALOG), BLANK],
    )
    fake.on("agent.prompt", _info(seq=4))
    fake.on("agent.send_keys", {"type": "ok"})
    fake.on_sequence(
        "agent.get",
        [
            _info(seq=4),
            _info(seq=4),
            _info(seq=4),
            _info(seq=4),
            _info(seq=4),
            _info(status="working", seq=5),
        ],
    )

    await delivery.deliver(client, "w1:p1", "hello", attempts=3)
    keys = [c["params"]["keys"] for c in fake.calls("agent.send_keys")]
    assert ["enter"] in keys


# -- the unknown-state false positive ----------------------------------------


async def test_delivery_never_starts_from_an_unknown_state(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """An `unknown` sample carries seq 0, and every real seq differs from 0.

    Using it as the `before` state made `deliver` report success on the very next poll
    even when nothing had moved -- a silent false positive in exactly the registration-lag
    window that was just discovered to exist.
    """
    fake.on("agent.read", BLANK)
    fake.on_sequence(
        "agent.get",
        [
            _info(seq=4),  # wait_ready
            {"__error__": {"code": "not_found", "message": "lag"}},  # before: would be seq 0
            _info(seq=4),  # a real sample -- unchanged, so nothing was delivered
        ],
    )
    fake.on("agent.prompt", _info(seq=4))
    fake.on("agent.send_keys", {"type": "ok"})

    with pytest.raises(WorkflowError):
        await delivery.deliver(client, "w1:p1", "hello", attempts=1)


async def test_unknown_polls_do_not_count_as_movement(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that vanishes mid-turn must not read as a delivered prompt."""
    monkeypatch.setattr(delivery, "DELIVERY_POLL_SECONDS", 2)
    fake.on("agent.read", BLANK)
    fake.on_sequence(
        "agent.get",
        [
            _info(seq=4),  # wait_ready
            _info(seq=4),  # before: known
            {"__error__": {"code": "not_found", "message": "gone"}},  # poll
            {"__error__": {"code": "not_found", "message": "gone"}},  # poll
            {"__error__": {"code": "not_found", "message": "gone"}},  # final check
        ],
    )
    fake.on("agent.prompt", _info(seq=4))
    fake.on("agent.send_keys", {"type": "ok"})

    with pytest.raises(WorkflowError):
        await delivery.deliver(client, "w1:p1", "hello", attempts=1)


async def test_readiness_ceiling_is_short_enough_for_a_prompt_command(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """`wq chat` and `wq ask` are documented as returning promptly (go.md).

    A chat tab's agent was measured never setting interactive_ready, so this ceiling is
    paid in full on every fresh tab. It must stay small -- the receipt loop, not this wait,
    is what establishes delivery.
    """
    assert delivery.READY_TIMEOUT <= 15.0
