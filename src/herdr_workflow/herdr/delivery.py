"""Getting a prompt into an agent, and knowing that it landed.

This module is behaviors #1, #2 and #5 from docs/behaviors.md. It is the part of wq that
exists only because driving a TUI unattended is not the same as calling an API.

Measured against herdr 0.7.5 with a live pi agent:

    after agent.start +1s   idle  seq=4  interactive_ready=None
    after agent.start +3s   idle  seq=4  interactive_ready=True
    agent.prompt returns    idle  seq=4  <- the PRE-prompt state, not a receipt
    +1s                     working seq=5   <- the receipt: seq moved
    +4s                     done    seq=6
    +12s                    idle    seq=6   <- `done` does not persist

Every rule below follows from that trace.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass

from herdr_workflow.errors import ApiError, WorkflowError
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.protocol.messages import AgentReadResult, AgentResult

# Claude Code asks "Is this a project you created or one you trust?" the first time it runs
# in a directory -- which is every pane wq starts, since the scratch dir and the build
# worktree are both new. Every directory wq starts an agent in is one it just created or
# one the caller named on the command line, so the answer is yes.
TRUST_DIALOG = "Yes, I trust this folder"

# How long to wait for the readiness *hint* before prompting anyway.
#
# Short on purpose. `interactive_ready` is advisory (see wait_ready), and a chat tab's agent
# was measured never setting it at all -- so a long ceiling here buys nothing and costs the
# caller the whole wait. `wq chat` and `wq ask` are documented as returning promptly, and
# the delivery receipt is what actually establishes success.
READY_TIMEOUT = 10.0
SETTLE_ATTEMPTS = 20
DELIVERY_POLL_SECONDS = 10
POLL_INTERVAL = 1.0

# How long a pane may report `unknown` before we call the agent gone.
#
# `agent.start` returning ok does not mean `agent.get` knows about the agent yet --
# registration is asynchronous, and for the first second or so the pane answers as if it
# has no agent at all. Treating the first `unknown` as fatal makes `wq chat` fail
# immediately on a tab it just created, which is exactly how this constant came to exist.
UNKNOWN_GRACE = 20.0


@dataclass(frozen=True)
class AgentState:
    status: str
    seq: int
    interactive_ready: bool | None


async def agent_state(client: HerdrClient, pane: str) -> AgentState:
    """Current status and change sequence for a pane's agent.

    A pane with no agent reports `unknown`, matching herdr's own vocabulary, so callers can
    treat "gone" as a status rather than an exception.
    """
    try:
        result = await client.call("agent.get", AgentResult, {"target": pane})
    except ApiError:
        return AgentState("unknown", 0, None)
    agent = result.agent
    return AgentState(agent.agent_status, agent.state_change_seq, agent.interactive_ready)


async def wait_ready(client: HerdrClient, pane: str, timeout: float = READY_TIMEOUT) -> bool:
    """Wait for herdr to report the agent's prompt box.

    **Advisory, not a gate.** `interactive_ready` is optional in the schema and reads as
    absent for the first seconds after `agent.start` -- and behavior #1 means that even
    `True` can mean "showing a trust dialog whose option list looks like a prompt". So a
    timeout here is not fatal: the delivery confirmation in `deliver` is the real guard,
    and it catches the case this would have caught plus the ones it cannot see.

    Returns whether readiness was actually observed, for logging.
    """
    started = time.monotonic()
    deadline = started + timeout
    seen_agent = False

    while time.monotonic() < deadline:
        state = await agent_state(client, pane)
        if state.status != "unknown":
            seen_agent = True
        if state.interactive_ready:
            return True

        # `unknown` means either "registration has not caught up yet" or "the agent is
        # gone". They are indistinguishable in a single sample, so distinguish by time: if
        # we have never once seen an agent here after the grace period, it is not coming.
        if not seen_agent and time.monotonic() - started > UNKNOWN_GRACE:
            raise WorkflowError(
                f"no agent in pane {pane}",
                why=f"the pane reported no agent for {UNKNOWN_GRACE:.0f}s after being asked",
                fix=f"look at it: herdr agent attach {pane}",
            )
        await asyncio.sleep(POLL_INTERVAL)
    return False


async def await_known_state(client: HerdrClient, pane: str) -> AgentState:
    """Poll until the pane reports an agent, and return that state.

    Needed because `agent_state` reports `unknown` with `seq == 0` both when registration
    has not caught up and when the agent is gone. Using such a sample as the *before* state
    of a delivery is a silent false positive: any real sequence number differs from 0, so
    the very next poll looks like the prompt landed even when nothing moved.

    So a delivery never starts from an unknown state -- it waits for a real one, or fails.
    """
    deadline = time.monotonic() + UNKNOWN_GRACE
    while True:
        state = await agent_state(client, pane)
        if state.status != "unknown":
            return state
        if time.monotonic() >= deadline:
            raise WorkflowError(
                f"no agent in pane {pane}",
                why=f"the pane reported no agent for {UNKNOWN_GRACE:.0f}s",
                fix=f"look at it: herdr agent attach {pane}",
            )
        await asyncio.sleep(POLL_INTERVAL)


async def read_screen(client: HerdrClient, pane: str, lines: int = 40) -> str:
    try:
        result = await client.call(
            "agent.read",
            AgentReadResult,
            {"target": pane, "source": "visible", "lines": lines, "strip_ansi": True},
        )
    except ApiError:
        return ""
    return result.read.text


async def settle(client: HerdrClient, pane: str) -> bool:
    """Clear a trust dialog if one is on screen. Behavior #1.

    herdr infers readiness from the prompt box, and the dialog's option list draws one, so
    the pane reports ready while the only input it accepts is an answer. A prompt sent into
    that state is swallowed **and its Enter confirms the highlighted option** -- you do not
    just lose a prompt, you answer a security question on the agent's behalf.

    Returns whether a dialog had to be answered.
    """
    answered = False
    for _ in range(SETTLE_ATTEMPTS):
        screen = await read_screen(client, pane)
        if TRUST_DIALOG not in screen:
            return answered
        await send_keys(client, pane, ["enter"])
        answered = True
        await asyncio.sleep(POLL_INTERVAL)

    raise WorkflowError(
        f"pane {pane} is still asking whether to trust its directory",
        why="the trust dialog did not clear after answering it repeatedly",
        fix=f"attach and answer it by hand: herdr agent attach {pane}",
    )


async def send_keys(client: HerdrClient, pane: str, keys: list[str]) -> None:
    # Best-effort: used to clear a composer and to answer dialogs, and the retry loop
    # around it is what actually establishes the outcome.
    with suppress(ApiError):
        await client.request("agent.send_keys", {"target": pane, "keys": keys})


async def deliver(client: HerdrClient, pane: str, text: str, attempts: int = 5) -> None:
    """Send a prompt and confirm the agent took it. Behavior #2.

    `agent.prompt` returning ok means **herdr** accepted the request, not that the **TUI**
    accepted the text. Claude Code keeps drawing a composer for tens of seconds after
    answering the trust dialog while discarding everything typed into it, and nothing in
    its status, title, or screen distinguishes that pane from a working one.

    `state_change_seq` is the receipt. Nothing moved after
    `DELIVERY_POLL_SECONDS` means nothing was delivered: clear the composer with Esc and
    try again.
    """
    ready = await wait_ready(client, pane)
    if not ready:
        # Worth saying, not worth stopping for. See wait_ready.
        from herdr_workflow.output import console

        console.detail(f"pane {pane} never reported interactive_ready; prompting anyway")

    for attempt in range(1, attempts + 1):
        await settle(client, pane)

        # Never start from `unknown`: its seq is 0, and every real seq differs from 0.
        before = await await_known_state(client, pane)
        try:
            # The returned AgentInfo carries the pre-prompt state, so it is discarded --
            # verified live, seq was unchanged in the response and only moved on a
            # subsequent poll.
            await client.request("agent.prompt", {"target": pane, "text": text})
        except ApiError as exc:
            raise WorkflowError(
                f"herdr rejected the prompt for pane {pane}",
                why=f"{exc.code}: {exc.message}",
                fix=f"look at the pane: herdr agent attach {pane}",
            ) from exc

        if await _confirm_delivery(client, pane, before):
            return

        from herdr_workflow.output import console

        console.log(
            f"pane {pane} did not take the prompt — clearing and retrying ({attempt}/{attempts})"
        )
        await send_keys(client, pane, ["esc"])
        await asyncio.sleep(POLL_INTERVAL)

    raise WorkflowError(
        f"pane {pane} never took the prompt",
        why=f"{attempts} attempts produced no change in the agent's state",
        fix=f"attach with: herdr agent attach {pane}",
    )


async def _confirm_delivery(client: HerdrClient, pane: str, before: AgentState) -> bool:
    """Did the agent take the prompt?

    Two signals, in order of reliability:

    1. `state_change_seq` moved. Unambiguous.
    2. The status differs from the one **this attempt started from**. A turn that starts
       late looks exactly like a dropped prompt until the last poll, which may land after
       the loop gave up -- so this is checked once more at the end. Compared against the
       starting status rather than a fixed list, because a warm pane sits in `done` from
       its previous turn and seeing `done` would prove nothing.
    An `unknown` sample proves nothing either way -- it carries `seq == 0`, which differs
    from every real sequence number -- so those are skipped rather than counted as movement.
    """
    for _ in range(DELIVERY_POLL_SECONDS):
        now = await agent_state(client, pane)
        if now.status != "unknown" and now.seq != before.seq:
            return True
        await asyncio.sleep(POLL_INTERVAL)

    now = await agent_state(client, pane)
    if now.status == "unknown":
        return False
    return now.seq != before.seq or now.status != before.status
