"""A fake herdr daemon, speaking the real wire protocol over a real unix socket.

This is the thing that makes the rewrite worth doing. Against the Bash implementation the
only way to exercise a retry path was to run it live against real agents -- minutes and
tokens per iteration. Here it is a sub-second test.

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
        self._writers.append(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                await self._handle_line(line, writer)
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            if writer in self._writers:
                self._writers.remove(writer)

    async def _handle_line(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            return
        self.received.append(request)

        method = request.get("method", "")
        if method in self.close_on_methods:
            writer.close()
            return
        if method in self.drop_methods:
            return  # never answer: exercises the client's request timeout
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.malformed_before_response:
            writer.write(b"{not json at all\n")

        handler = self._handlers.get(method)
        body: dict[str, Any]
        if handler is None:
            body = {
                "id": request.get("id"),
                "error": {"code": "unknown_method", "message": f"no handler for {method}"},
            }
        else:
            result = handler(request.get("params") or {})
            if asyncio.iscoroutine(result):
                result = await result
            if isinstance(result, dict) and "__error__" in result:
                body = {"id": request.get("id"), "error": result["__error__"]}
            else:
                body = {"id": request.get("id"), "result": result}

        writer.write(json.dumps(body).encode() + b"\n")
        await writer.drain()
