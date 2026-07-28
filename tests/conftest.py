from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator
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


@pytest.fixture
def client(fake: FakeHerdr) -> HerdrClient:
    """A client pointed at the fake. It holds no connection -- each request makes one."""
    return HerdrClient(fake.socket_path, timeout=2.0)


@pytest.fixture
def threaded_fake(socket_dir: Path) -> Iterator[FakeHerdr]:
    """A fake daemon running in its own thread and event loop.

    CLI tests drive `wq` synchronously through Typer's CliRunner, and `wq` starts its own
    `asyncio.run`. A fake server living in the *test's* loop is frozen for the duration of
    that call, so the client connects to something that never answers and the test burns a
    full request timeout before failing for the wrong reason.

    Anything that invokes the CLI needs this fixture; anything awaiting the client
    directly can use `fake`.
    """
    server = FakeHerdr(socket_dir / "h.sock")
    server.on("ping", PONG)

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.start())
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    try:
        yield server
    finally:
        asyncio.run_coroutine_threadsafe(server.stop(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def git_run(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repository with one commit on `main`, to act as a remote.

    Real repositories rather than mocks: `build` shells out to git for the branch point and
    the diff range, and those are exactly the behaviours worth testing for real.
    """
    work = tmp_path / "seed"
    work.mkdir()
    git_run(work, "init", "--initial-branch=main")
    git_run(work, "config", "user.email", "t@example.com")
    git_run(work, "config", "user.name", "Test")
    (work / "README.md").write_text("seed\n")
    git_run(work, "add", ".")
    git_run(work, "commit", "-m", "initial")

    bare = tmp_path / "origin.git"
    git_run(tmp_path, "clone", "--bare", str(work), str(bare))
    return bare


@pytest.fixture
def repo(tmp_path: Path, origin: Path) -> Path:
    clone = tmp_path / "repo"
    git_run(tmp_path, "clone", str(origin), str(clone))
    git_run(clone, "config", "user.email", "t@example.com")
    git_run(clone, "config", "user.name", "Test")
    return clone.resolve()


@pytest.fixture
def master_repo(tmp_path: Path) -> Path:
    """A clone of a repository whose default branch is `master`, with no published HEAD.

    The case Bash's hard-coded `origin/main` could never build in. `build` resolves the
    base in the parent checkout and records it; `revise` reads it back and diffs in the
    linked worktree -- a different directory, so it is worth proving end to end rather than
    assuming a shared `.git` makes it work.
    """
    work = tmp_path / "old-seed"
    work.mkdir()
    git_run(work, "init", "--initial-branch=master")
    git_run(work, "config", "user.email", "t@example.com")
    git_run(work, "config", "user.name", "Test")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    git_run(work, "add", ".")
    git_run(work, "commit", "-m", "initial")

    bare = tmp_path / "old-origin.git"
    git_run(tmp_path, "clone", "--bare", str(work), str(bare))
    clone = tmp_path / "old-repo"
    git_run(tmp_path, "clone", str(bare), str(clone))
    git_run(clone, "config", "user.email", "t@example.com")
    git_run(clone, "config", "user.name", "Test")
    git_run(clone, "remote", "set-head", "origin", "--delete")
    return clone.resolve()


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
