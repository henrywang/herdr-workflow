"""Client tests, run against a real unix socket speaking the real framing.

The fake closes the connection after each response, because herdr 0.7.5 does. A fake that
stayed open would let a client with a latent multiple-requests-per-connection assumption
pass here and fail against the real daemon -- which is exactly what happened once.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from herdr_workflow.errors import ApiError, HerdrUnavailable, ProtocolError
from herdr_workflow.herdr.client import HerdrClient
from tests.fake_herdr import FakeHerdr


async def test_ping_round_trip(client: HerdrClient) -> None:
    pong = await client.ping()
    assert pong.protocol == 17
    assert pong.type == "pong"


async def test_params_always_sent_even_when_empty(client: HerdrClient, fake: FakeHerdr) -> None:
    """Verified against herdr: omitting `params` returns invalid_request
    "missing field `params`", even for methods that take none."""
    await client.ping()
    assert fake.calls("ping")[0]["params"] == {}


async def test_each_request_uses_its_own_connection(client: HerdrClient, fake: FakeHerdr) -> None:
    """The server closes after one response, so a second call must reconnect.

    This is the regression test for the bug that shipped: a long-lived client answered
    the first request and then failed every one after it.
    """
    await client.ping()
    await client.ping()
    snapshot_calls = len(fake.calls("ping"))
    assert snapshot_calls == 2


async def test_sequential_requests_all_succeed(client: HerdrClient, fake: FakeHerdr) -> None:
    fake.on("a.one", {"type": "one"})
    fake.on("a.two", {"type": "two"})
    assert b"one" in bytes(await client.request("a.one"))
    assert b"two" in bytes(await client.request("a.two"))
    assert b"one" in bytes(await client.request("a.one"))


async def test_concurrent_requests_each_get_their_own_answer(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """Separate connections, so concurrency needs no id correlation."""

    async def slow(_p: object) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        return {"type": "slow"}

    fake.on("a.slow", slow)
    fake.on("a.fast", {"type": "fast"})

    slow_task = asyncio.create_task(client.request("a.slow"))
    await asyncio.sleep(0.01)
    assert b"fast" in bytes(await client.request("a.fast"))
    assert b"slow" in bytes(await slow_task)


async def test_error_response_becomes_api_error(client: HerdrClient, fake: FakeHerdr) -> None:
    fake.on_error("agent.start", "agent_pane_busy", "pane is busy")
    with pytest.raises(ApiError) as caught:
        await client.request("agent.start", {"target": "w1:p1"})
    # The code is kept as a field because retry logic branches on it, and matching against
    # a human-readable message is how that breaks on the next herdr release.
    assert caught.value.code == "agent_pane_busy"


async def test_error_with_empty_id_still_raises(client: HerdrClient, fake: FakeHerdr) -> None:
    """herdr sets id to "" on requests it could not parse.

    With one request per connection there is nothing to correlate, so an unusable id must
    not stop the error surfacing.
    """
    with pytest.raises(ApiError) as caught:
        await client.request("no.such.method")
    assert caught.value.code == "invalid_request"


async def test_subscription_keeps_its_connection_and_streams_events(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """events.subscribe is the one method that does not close after its ack."""
    fake.on("events.subscribe", {"type": "subscription_started"})

    async with client.subscribe([{"type": "workspace.created"}]) as events:
        await asyncio.sleep(0.05)
        await fake.push_event("workspace.created", {"workspace_id": "w9"})
        name, _data = await anext(aiter(events))

    assert name == "workspace.created"


async def test_subscription_error_is_raised_not_swallowed(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """A bad subscription is rejected at the ack; herdr then closes the connection."""
    fake.on_error("events.subscribe", "invalid_request", "missing field `pane_id`")
    with pytest.raises(ApiError):
        async with client.subscribe([{"type": "pane.agent_status_changed"}]):
            pass


async def test_unparseable_response_is_a_protocol_error(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """One request per connection means a garbled line is the answer, not noise to skip."""
    fake.malformed_before_response = True
    with pytest.raises(ProtocolError):
        await client.ping()


async def test_unanswered_request_times_out_with_a_useful_message(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    fake.drop_methods.add("ping")
    client.timeout = 0.2
    with pytest.raises(ProtocolError) as caught:
        await client.ping()
    assert "did not answer ping" in caught.value.message


async def test_server_closing_without_answering_is_explained(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    fake.close_on_methods.add("ping")
    with pytest.raises(HerdrUnavailable) as caught:
        await client.ping()
    assert "closed the connection" in caught.value.message


async def test_missing_socket_explains_how_to_start_herdr(socket_dir: Path) -> None:
    c = HerdrClient(socket_dir / "nope.sock")
    with pytest.raises(HerdrUnavailable) as caught:
        await c.ping()
    assert "herdr" in (caught.value.fix or "")


async def test_decode_failure_points_at_doctor(client: HerdrClient, fake: FakeHerdr) -> None:
    """Protocol drift should name the command that diagnoses it."""
    fake.on("ping", {"type": "pong"})  # missing required version/protocol
    with pytest.raises(ProtocolError) as caught:
        await client.ping()
    assert caught.value.fix is not None
    assert "wq doctor" in caught.value.fix
