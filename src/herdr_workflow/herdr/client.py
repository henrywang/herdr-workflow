"""Async client for the herdr socket API.

Newline-delimited JSON over a unix socket, no handshake. See docs/protocol-framing.md for
how that was established.

The shape of this class is forced by one fact: after `events.subscribe`, the server
pushes event lines onto the same connection, interleaved with responses. A
write-then-read-one-line client would read an event where it expected its own response
and mis-attribute it. So there is a background reader, a pending-futures map keyed on
request id, and a separate path for lines that carry no id.

That structure also gives concurrent in-flight requests, whether or not the server needs
them.
"""

from __future__ import annotations

import asyncio
import itertools
import os
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, TypeVar

import msgspec

from herdr_workflow.errors import ApiError, HerdrUnavailable, ProtocolError
from herdr_workflow.protocol.messages import Envelope, Pong, Snapshot, SnapshotResult

T = TypeVar("T")

_DEFAULT_TIMEOUT = 30.0

_decoder = msgspec.json.Decoder(Envelope)
_encoder = msgspec.json.Encoder()


class HerdrClient:
    """One connection, many requests.

    Not reentrant across event loops; construct one per `asyncio.run`.
    """

    def __init__(self, socket_path: Path, *, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[Envelope]] = {}
        self._event_handlers: list[Callable[[str, msgspec.Raw], None]] = []
        self._ids = itertools.count(1)
        # Unique per connection. The herdr CLI reuses ids like "cli:agent:start" because
        # it opens a connection per request; we hold one open, so uniqueness is ours to
        # guarantee.
        self._prefix = f"wq{os.getpid()}"

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        if not self.socket_path.exists():
            raise HerdrUnavailable(
                f"herdr socket not found at {self.socket_path}",
                why="the herdr server does not appear to be running",
            )
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(self.socket_path)
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

        self._read_task = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._read_task
            self._read_task = None

        if self._writer is not None:
            self._writer.close()
            # Closing a connection the server already dropped raises; there is nothing
            # left to do about it either way.
            with suppress(OSError, asyncio.CancelledError):
                await self._writer.wait_closed()
            self._writer = None
        self._reader = None

        self._fail_pending(HerdrUnavailable("connection closed", why="client shut down"))

    # -- reading -----------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    self._fail_pending(
                        HerdrUnavailable(
                            "herdr closed the connection",
                            why="the server went away mid-request",
                        )
                    )
                    return
                self._dispatch(line)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            self._fail_pending(HerdrUnavailable("lost connection to herdr", why=str(exc)))

    def _dispatch(self, line: bytes) -> None:
        try:
            envelope = _decoder.decode(line)
        except msgspec.DecodeError:
            # One unparseable line must not take the connection down: a future protocol
            # may add a message shape we do not model, and dropping it is survivable
            # where dying is not. Requests still in flight will time out and say so.
            return

        if envelope.id is None:
            # No id means a pushed event, not a response to anything we sent.
            if envelope.event is not None and envelope.has_data:
                for handler in self._event_handlers:
                    handler(envelope.event, envelope.data)
            return

        future = self._pending.pop(envelope.id, None)
        if future is None or future.done():
            # A response to a request that already timed out. Nothing to do with it.
            return
        future.set_result(envelope)

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    def on_event(self, handler: Callable[[str, msgspec.Raw], None]) -> None:
        self._event_handlers.append(handler)

    # -- requests ----------------------------------------------------------

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> msgspec.Raw:
        """Send a request, wait for its response, return the raw result.

        Raises ApiError when the server answers with an error, so callers that care about
        a specific code (`agent_pane_busy`, `agent_name_taken`) can catch it by code
        rather than by parsing a message.
        """
        if self._writer is None:
            raise HerdrUnavailable("not connected to herdr", why="connect() was not called")

        request_id = f"{self._prefix}-{next(self._ids)}"
        future: asyncio.Future[Envelope] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future

        # `params` is required by the schema even when the method takes none: send {},
        # never omit the key.
        payload = _encoder.encode({"id": request_id, "method": method, "params": params or {}})
        self._writer.write(payload + b"\n")

        try:
            await self._writer.drain()
            envelope = await asyncio.wait_for(future, timeout or self.timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise ProtocolError(
                f"herdr did not answer {method} within {timeout or self.timeout:.0f}s",
                why="the request was written but no matching response arrived",
            ) from exc
        except OSError as exc:
            self._pending.pop(request_id, None)
            raise HerdrUnavailable(f"failed to send {method}", why=str(exc)) from exc

        if envelope.error is not None:
            raise ApiError(envelope.error.code, envelope.error.message, method=method)
        if not envelope.has_result:
            raise ProtocolError(
                f"herdr answered {method} with neither result nor error",
                why="the response envelope violated the protocol contract",
            )
        return envelope.result

    async def call(
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
            # Almost always a protocol drift: herdr changed a shape we hard-coded. Say so,
            # and point at the check that would have caught it earlier.
            raise ProtocolError(
                f"could not decode the result of {method}: {exc}",
                why="the server's response did not match the shape wq expects",
                fix="run: wq doctor  (this usually means a herdr protocol mismatch)",
            ) from exc

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
    client = HerdrClient(socket_path, timeout=timeout)
    await client.connect()
    try:
        yield client
    finally:
        await client.close()
