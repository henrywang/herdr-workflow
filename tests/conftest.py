from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from herdr_workflow.herdr.client import HerdrClient
from tests.fake_herdr import FakeHerdr

PONG = {"type": "pong", "version": "0.0.0-test", "protocol": 17}


@pytest_asyncio.fixture
async def socket_dir() -> AsyncIterator[Path]:
    """A directory short enough to hold a unix socket.

    AF_UNIX paths are capped near 104 bytes on macOS and pytest's `tmp_path` -- which
    embeds the test name -- blows straight past it. Anything binding a socket needs this
    rather than tmp_path.
    """
    path = Path(tempfile.mkdtemp(prefix="wq-t", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest_asyncio.fixture
async def fake(socket_dir: Path) -> AsyncIterator[FakeHerdr]:
    server = FakeHerdr(socket_dir / "h.sock")
    server.on("ping", PONG)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest_asyncio.fixture
async def client(fake: FakeHerdr) -> AsyncIterator[HerdrClient]:
    c = HerdrClient(fake.socket_path, timeout=2.0)
    await c.connect()
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
def snapshot_result() -> dict[str, Any]:
    def ws(label: str, ws_id: str, status: str = "idle") -> dict[str, Any]:
        return {
            "workspace_id": ws_id,
            "number": 1,
            "label": label,
            "focused": False,
            "pane_count": 1,
            "tab_count": 1,
            "active_tab_id": f"{ws_id}:t1",
            "agent_status": status,
        }

    return {
        "type": "session_snapshot",
        "snapshot": {
            "workspaces": [
                ws("inbox", "w1"),
                ws("plan-alpha", "w2", "working"),
                ws("beta", "w3"),
                ws("bs-gamma", "w4"),
                ws("some-repo", "w5"),
            ],
            "tabs": [],
            "panes": [],
            "agents": [],
            "protocol": 17,
            "version": "0.0.0-test",
        },
    }
