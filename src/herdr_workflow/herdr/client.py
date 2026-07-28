"""Client for the herdr socket API.

Newline-delimited JSON over a unix socket, no handshake.

**The server answers exactly one request per connection, then closes it.** Verified
against herdr 0.7.5 -- a second write raises BrokenPipeError, and pipelining two requests
yields one response followed by a close. That makes a request a self-contained
connect/write/read/close, with no id correlation to do: whatever comes back on this
connection is the answer to the one thing we sent.

The single exception is `events.subscribe`, which acks and then holds the connection open
streaming event lines. That is `subscribe()`, which owns its own connection for as long as
the caller iterates it.

See docs/protocol-framing.md for the full set of verified behaviours.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import msgspec

from herdr_workflow.errors import ApiError, HerdrUnavailable, ProtocolError
from herdr_workflow.protocol.messages import Envelope, Pong, Snapshot, SnapshotResult

_DEFAULT_TIMEOUT = 30.0

_decoder = msgspec.json.Decoder(Envelope)
_encoder = msgspec.json.Encoder()


def _decode_line(line: bytes, method: str) -> Envelope:
    try:
        return _decoder.decode(line)
    except msgspec.DecodeError as exc:
        raise ProtocolError(
            f"could not parse herdr's response to {method}",
            why=f"{exc}: {line[:200]!r}",
        ) from exc


def _check(envelope: Envelope, method: str) -> msgspec.Raw:
    """Turn a response envelope into a result, or raise.

    Note the server sets `id` to "" on requests it could not parse, so the id is not
    usable for correlation on the error path. With one request per connection there is
    nothing to correlate anyway.
    """
    if envelope.error is not None:
        raise ApiError(envelope.error.code, envelope.error.message, method=method)
    if not envelope.has_result:
        raise ProtocolError(
            f"herdr answered {method} with neither result nor error",
            why="the response envelope violated the protocol contract",
        )
    return envelope.result


class HerdrClient:
    """Talks to the herdr API socket, one connection per request.

    Cheap to construct and hold; it owns no connection between calls.
    """

    def __init__(self, socket_path: Path, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    # -- connection --------------------------------------------------------

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if not self.socket_path.exists():
            raise HerdrUnavailable(
                f"herdr socket not found at {self.socket_path}",
                why="the herdr server does not appear to be running",
            )
        try:
            return await asyncio.open_unix_connection(self.socket_path)
        except (ConnectionRefusedError, FileNotFoundError) as exc:
            # A stale socket file outlives the process that made it, so "the file exists"
            # is not the same as "someone is listening".
            raise HerdrUnavailable(
                f"could not connect to herdr at {self.socket_path}",
                why=f"{type(exc).__name__}: the socket exists but nothing is listening",
            ) from exc
        except OSError as exc:
            raise HerdrUnavailable(
                f"could not connect to herdr at {self.socket_path}", why=str(exc)
            ) from exc

    @staticmethod
    async def _shutdown(writer: asyncio.StreamWriter) -> None:
        writer.close()
        # The server has usually closed first; there is nothing to do about it.
        with suppress(OSError, asyncio.CancelledError):
            await writer.wait_closed()

    @staticmethod
    def _frame(request_id: str, method: str, params: dict[str, Any] | None) -> bytes:
        # `params` is required by the schema even when the method takes none. Verified:
        # omitting it returns invalid_request "missing field `params`".
        return _encoder.encode({"id": request_id, "method": method, "params": params or {}}) + b"\n"

    # -- requests ----------------------------------------------------------

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> msgspec.Raw:
        """Send one request on its own connection and return the raw result.

        Raises ApiError when the server answers with an error, so callers that branch on a
        specific code (`agent_pane_busy`, `agent_name_taken`) can match the code rather
        than parse a message.
        """
        limit = timeout or self.timeout
        try:
            return await asyncio.wait_for(self._exchange(method, params), limit)
        except TimeoutError as exc:
            raise ProtocolError(
                f"herdr did not answer {method} within {limit:.0f}s",
                why="the request was sent but no response arrived",
            ) from exc

    async def _exchange(self, method: str, params: dict[str, Any] | None) -> msgspec.Raw:
        reader, writer = await self._open()
        try:
            # The id is echoed back but never needed: one request per connection means
            # there is nothing else the response could belong to. It is still sent
            # because the schema requires it, and it shows up in herdr's server log,
            # where a descriptive value is worth more than a unique one.
            writer.write(self._frame(f"wq:{method}", method, params))
            await writer.drain()
            line = await reader.readline()
        except OSError as exc:
            raise HerdrUnavailable(f"failed to send {method}", why=str(exc)) from exc
        finally:
            await self._shutdown(writer)

        if not line:
            raise HerdrUnavailable(
                f"herdr closed the connection without answering {method}",
                why="the server accepted the request then went away",
            )
        return _check(_decode_line(line, method), method)

    async def call[T](
        self,
        method: str,
        result_type: type[T],
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> T:
        """`request`, then decode the result into a typed struct."""
        raw = await self.request(method, params, timeout=timeout)
        try:
            return msgspec.json.decode(raw, type=result_type)
        except msgspec.ValidationError as exc:
            # Almost always protocol drift: herdr changed a shape we hard-coded. Say so,
            # and name the command that diagnoses it.
            raise ProtocolError(
                f"could not decode the result of {method}: {exc}",
                why="the server's response did not match the shape wq expects",
                fix="run: wq doctor  (this usually means a herdr protocol mismatch)",
            ) from exc

    # -- events ------------------------------------------------------------

    @asynccontextmanager
    async def subscribe(
        self, subscriptions: Sequence[dict[str, Any]]
    ) -> AsyncGenerator[AsyncIterator[tuple[str, msgspec.Raw]]]:
        """Subscribe to events, yielding an iterator of `(event_name, data)`.

        The one method that keeps its connection: the ack is
        `{"type": "subscription_started"}` and event lines follow on the same socket,
        each carrying `event` and `data` but no `id`.

        Subscription names are **dotted** -- `workspace.created`,
        `pane.agent_status_changed` -- and are a different vocabulary from the
        underscored names `events.wait` matches on. Some require extra keys; a
        `pane.agent_status_changed` subscription without `pane_id` is rejected.
        """
        reader, writer = await self._open()
        try:
            writer.write(
                self._frame(
                    "wq:events.subscribe",
                    "events.subscribe",
                    {"subscriptions": list(subscriptions)},
                )
            )
            await writer.drain()
            ack = await asyncio.wait_for(reader.readline(), self.timeout)
            if not ack:
                raise HerdrUnavailable(
                    "herdr closed the connection instead of starting a subscription",
                    why="the server went away before acknowledging",
                )
            _check(_decode_line(ack, "events.subscribe"), "events.subscribe")

            async def events() -> AsyncIterator[tuple[str, msgspec.Raw]]:
                while True:
                    line = await reader.readline()
                    if not line:
                        return  # server closed the stream
                    envelope = _decode_line(line, "events.subscribe")
                    if envelope.event is not None and envelope.has_data:
                        yield envelope.event, envelope.data

            yield events()
        finally:
            await self._shutdown(writer)

    # -- convenience -------------------------------------------------------

    async def ping(self) -> Pong:
        return await self.call("ping", Pong)

    async def snapshot(self) -> Snapshot:
        result = await self.call("session.snapshot", SnapshotResult)
        return result.snapshot


@asynccontextmanager
async def connect(
    socket_path: Path, *, timeout: float = _DEFAULT_TIMEOUT
) -> AsyncGenerator[HerdrClient]:
    """Yield a client.

    There is no connection to set up or tear down -- each request makes its own -- but
    call sites read better as a scope, and this is where pooling would go if the server
    ever keeps connections alive.
    """
    yield HerdrClient(socket_path, timeout=timeout)
