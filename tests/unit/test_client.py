"""Client tests, run against a real unix socket speaking the real framing."""

from __future__ import annotations

import asyncio
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
    """The schema marks `params` required even for EmptyParams methods."""
    await client.ping()
    assert fake.calls("ping")[0]["params"] == {}


async def test_request_ids_are_unique(client: HerdrClient, fake: FakeHerdr) -> None:
    """We hold one connection open, so unlike the herdr CLI we cannot reuse ids."""
    await client.ping()
    await client.ping()
    ids = [r["id"] for r in fake.calls("ping")]
    assert len(set(ids)) == 2


async def test_error_response_becomes_api_error(client: HerdrClient, fake: FakeHerdr) -> None:
    fake.on_error("agent.start", "agent_pane_busy", "pane is busy")
    with pytest.raises(ApiError) as caught:
        await client.request("agent.start", {"target": "w1:p1"})
    # The code is kept as a field because retry logic branches on it, and matching
    # against a human-readable message is how that breaks on the next herdr release.
    assert caught.value.code == "agent_pane_busy"


async def test_event_between_request_and_response_is_not_mistaken_for_it(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """The test that justifies the client's whole shape.

    After events.subscribe the server pushes event lines onto the same connection. A
    write-then-read-one-line client reads the event where it expects its own response and
    mis-attributes it. Here the event lands first and the response must still arrive
    intact.
    """
    seen: list[str] = []
    client.on_event(lambda name, _data: seen.append(name))

    async def answer(_params: object) -> dict[str, Any]:
        await fake.push_event("workspace_created", {"workspace_id": "w9"})
        await asyncio.sleep(0.01)
        return {"type": "pong", "version": "0.0.0-test", "protocol": 17}

    fake.on("ping", answer)
    pong = await client.ping()

    assert pong.protocol == 17
    assert seen == ["workspace_created"]


async def test_concurrent_requests_correlate_by_id(client: HerdrClient, fake: FakeHerdr) -> None:
    """Two requests in flight; the slower one answers first."""
    order = {"slow": 0.05, "fast": 0.0}

    async def slow(_p: object) -> dict[str, Any]:
        await asyncio.sleep(order["slow"])
        return {"type": "slow"}

    fake.on("a.slow", slow)
    fake.on("a.fast", {"type": "fast"})

    slow_task = asyncio.create_task(client.request("a.slow"))
    await asyncio.sleep(0.01)
    fast = await client.request("a.fast")
    assert b"fast" in bytes(fast)
    assert b"slow" in bytes(await slow_task)


async def test_malformed_line_does_not_kill_the_connection(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """A shape we cannot parse is survivable; dying on it is not."""
    fake.malformed_before_response = True
    pong = await client.ping()
    assert pong.protocol == 17


async def test_unanswered_request_times_out_with_a_useful_message(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    fake.drop_methods.add("ping")
    client.timeout = 0.2
    with pytest.raises(ProtocolError) as caught:
        await client.ping()
    assert "did not answer ping" in caught.value.message


async def test_server_disconnect_fails_pending_requests(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    fake.close_on_methods.add("ping")
    with pytest.raises(HerdrUnavailable):
        await client.ping()


async def test_missing_socket_explains_how_to_start_herdr(tmp_path: object) -> None:
    from pathlib import Path

    c = HerdrClient(Path(str(tmp_path)) / "nope.sock")
    with pytest.raises(HerdrUnavailable) as caught:
        await c.connect()
    assert caught.value.fix == "start it with: herdr"


async def test_decode_failure_points_at_doctor(client: HerdrClient, fake: FakeHerdr) -> None:
    """Protocol drift should name the command that diagnoses it."""
    fake.on("ping", {"type": "pong"})  # missing required version/protocol
    with pytest.raises(ProtocolError) as caught:
        await client.ping()
    assert caught.value.fix is not None
    assert "wq doctor" in caught.value.fix
