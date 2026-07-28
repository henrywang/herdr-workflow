"""A fake herdr daemon, speaking the real wire protocol over a real unix socket.

It makes retry paths testable in under a second without running live agents.

It deliberately speaks NDJSON over an actual socket rather than mocking the client, so
the framing, the id correlation, and the event interleaving are all under test.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

Handler = Callable[[dict[str, Any]], Awaitable[Any] | Any]


class FakeHerdr:
    """A scriptable stand-in for the herdr API socket.

    Register per-method behaviour, then assert on `received` afterwards::

        fake.on("ping", {"type": "pong", "version": "test", "protocol": 17})
        fake.on_error("agent.start", "agent_pane_busy", "pane is busy")
        fake.on("pane.split", lambda params: {"type": "pane", "pane": {...}})
    """

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.received: list[dict[str, Any]] = []
        self._handlers: dict[str, Handler] = {}
        self._server: asyncio.AbstractServer | None = None
        self._writers: list[asyncio.StreamWriter] = []
        # Injectable faults, so tests can reproduce the failure modes in
        # docs/behaviors.md rather than only the happy path.
        self.delay: float = 0.0
        self.drop_methods: set[str] = set()
        self.malformed_before_response = False
        self.close_on_methods: set[str] = set()

    # -- scripting ---------------------------------------------------------

    def on(self, method: str, result: Any) -> None:
        """Answer `method` with `result` -- a value, or a callable taking params."""
        self._handlers[method] = result if callable(result) else (lambda _p, r=result: r)

    def on_error(self, method: str, code: str, message: str = "error") -> None:
        def handler(_params: dict[str, Any]) -> dict[str, Any]:
            return {"__error__": {"code": code, "message": message}}

        self._handlers[method] = handler

    def on_sequence(self, method: str, results: list[Any]) -> None:
        """Answer `method` differently on each call.

        For retry paths: two `agent_pane_busy` errors then a success proves the retry
        loop both retries and stops (behavior #3).
        """
        remaining = list(results)

        def handler(_params: dict[str, Any]) -> Any:
            return remaining.pop(0) if remaining else results[-1]

        self._handlers[method] = handler

    def calls(self, method: str) -> list[dict[str, Any]]:
        return [r for r in self.received if r.get("method") == method]

    # -- events ------------------------------------------------------------

    async def push_event(self, event: str, data: dict[str, Any]) -> None:
        """Push an unsolicited event line to every connected client.

        Events carry no id. A client that reads one where it expected its own response
        will mis-attribute it -- this is what proves the read loop is correct.
        """
        line = json.dumps({"event": event, "data": data}).encode() + b"\n"
        for writer in list(self._writers):
            writer.write(line)
            await writer.drain()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))

    async def stop(self) -> None:
        for writer in list(self._writers):
            writer.close()
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer one request, then close -- exactly what herdr 0.7.5 does.

        This is not a simplification: the real server closes after a single response, and
        a fake that stayed open would let a client with a latent
        multiple-requests-per-connection assumption pass its tests and fail in the field.
        The one exception is `events.subscribe`, which holds the connection open.
        """
        self._writers.append(writer)
        try:
            line = await reader.readline()
            if not line:
                return
            subscribed = await self._handle_line(line, writer)
            if not subscribed:
                return
            # A subscription keeps the socket open until the client goes away.
            await reader.read()
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            if writer in self._writers:
                self._writers.remove(writer)
            writer.close()

    async def _handle_line(self, line: bytes, writer: asyncio.StreamWriter) -> bool:
        """Answer one request. Returns True if the connection should stay open."""
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            # The real server answers malformed input with an error carrying id "".
            writer.write(
                json.dumps(
                    {
                        "id": "",
                        "error": {"code": "invalid_request", "message": "invalid request"},
                    }
                ).encode()
                + b"\n"
            )
            await writer.drain()
            return False
        self.received.append(request)

        method = request.get("method", "")
        if method in self.close_on_methods:
            writer.close()
            return False
        if method in self.drop_methods:
            await asyncio.sleep(3600)  # never answer: exercises the client's timeout
            return False
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.malformed_before_response:
            writer.write(b"{not json at all\n")

        handler = self._handlers.get(method)
        body: dict[str, Any]
        if handler is None:
            # The real server reports an unknown method as invalid_request with id "".
            body = {
                "id": "",
                "error": {"code": "invalid_request", "message": f"unknown variant `{method}`"},
            }
        else:
            result = handler(request.get("params") or {})
            if asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, dict) and "__error__" in result:
                body = {"id": "", "error": result["__error__"]}
            else:
                body = {"id": request.get("id"), "result": result}

        writer.write(json.dumps(body).encode() + b"\n")
        await writer.drain()
        # events.subscribe is the one method that holds the connection open afterwards.
        return method == "events.subscribe" and "error" not in body
