"""Tests against a real herdr daemon.

Skipped unless one is running. These exist because the unit suite runs against a fake we
wrote ourselves: if our reading of the protocol is wrong, the fake is wrong the same way
and the unit tests still pass. That is exactly how the one-request-per-connection bug
survived 27 green tests.

Run with: uv run pytest -m integration
"""

from __future__ import annotations

import pytest

from herdr_workflow.errors import ApiError
from herdr_workflow.herdr import socket_path
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.protocol.messages import PINNED_PROTOCOL

pytestmark = pytest.mark.integration


@pytest.fixture
def live() -> HerdrClient:
    path = socket_path.resolve()
    if not path.exists():
        pytest.skip(f"no herdr socket at {path} — start it with: herdr")
    return HerdrClient(path, timeout=10.0)


async def test_ping(live: HerdrClient) -> None:
    pong = await live.ping()
    assert pong.type == "pong"
    assert pong.protocol > 0


async def test_pinned_protocol_matches_the_running_server(live: HerdrClient) -> None:
    """Fails loudly when herdr moves ahead of our pin.

    Not a warning here, unlike `wq doctor`: in CI this is the signal to regenerate types.
    """
    pong = await live.ping()
    assert pong.protocol == PINNED_PROTOCOL, (
        f"herdr {pong.version} speaks protocol {pong.protocol}, wq is pinned to "
        f"{PINNED_PROTOCOL} — re-check the models against `herdr api schema --json`"
    )


async def test_sequential_requests_survive_the_server_closing_each_connection(
    live: HerdrClient,
) -> None:
    """The regression test for the bug the fake could not catch."""
    await live.ping()
    await live.ping()
    snapshot = await live.snapshot()
    assert snapshot.protocol == PINNED_PROTOCOL


async def test_snapshot_decodes_into_our_hand_written_types(live: HerdrClient) -> None:
    """Every field we marked required must really be required.

    A ValidationError here means the models drifted from the server, which is the failure
    `wq doctor`'s protocol check is meant to predict.
    """
    snapshot = await live.snapshot()
    for workspace in snapshot.workspaces:
        assert workspace.workspace_id
        assert workspace.agent_status in ("idle", "working", "blocked", "done", "unknown")
    for pane in snapshot.panes:
        assert pane.pane_id
        assert pane.workspace_id
    for agent in snapshot.agents:
        assert agent.pane_id
        assert isinstance(agent.state_change_seq, int)
        # interactive_ready is optional in the schema and herdr 0.7.5 omits it entirely
        # for a running pi agent, so None is a legitimate value. Anything that waits on
        # readiness must cope with never seeing it.
        assert agent.interactive_ready in (True, False, None)


async def test_params_is_required_even_when_empty(live: HerdrClient) -> None:
    """Documents the server's own contract by exercising it."""
    await live.request("session.snapshot", {})


async def test_unknown_method_is_invalid_request(live: HerdrClient) -> None:
    with pytest.raises(ApiError) as caught:
        await live.request("no.such.method")
    assert caught.value.code == "invalid_request"


async def test_subscription_acks_with_dotted_event_names(live: HerdrClient) -> None:
    """`events.subscribe` wants `type` with dotted names; `events.wait` wants `event`
    with underscored ones. Getting these backwards is an easy and confusing mistake."""
    async with live.subscribe([{"type": "workspace.created"}]):
        pass  # a clean ack and teardown is the assertion


async def test_underscored_subscription_name_is_rejected(live: HerdrClient) -> None:
    with pytest.raises(ApiError) as caught:
        async with live.subscribe([{"type": "workspace_created"}]):
            pass
    assert caught.value.code == "invalid_request"


async def test_worktree_create_decodes_without_creating_anything(live: HerdrClient) -> None:
    """Behavior #12's regression test, and it costs nothing.

    `worktree.create` was modelled with the *workspace's* `WorktreeInfo` rather than its
    own -- two different structs sharing one name in the schema. It passed every unit test,
    because the fake was wrong the same way, and then failed on a live server *after*
    creating the worktree and both workspaces.

    A bad `cwd` makes herdr reject the call before it creates anything, so this exercises
    the same decode path against the real server with no side effects to clean up. It
    proves the request shape reaches the handler. The successful response deliberately
    models only the fields `wq build` reads; see behavior #12.
    """
    with pytest.raises(ApiError) as caught:
        await live.request(
            "worktree.create",
            {
                "cwd": "/nonexistent/wq-integration-probe",
                "branch": "wq/probe",
                "base": "origin/main",
                "path": "/nonexistent/wq-integration-probe-worktrees/probe",
                "label": "wq-probe",
                "focus": False,
            },
        )
    # Whatever herdr calls it, the point is that it is a *handled* rejection rather than
    # `invalid_request`, which would mean wq is sending params the method does not accept.
    assert caught.value.code != "invalid_request", (
        f"herdr rejected the params themselves: {caught.value.message}"
    )
