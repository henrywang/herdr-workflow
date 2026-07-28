"""`wq up` idempotence and `wq clean`, including the close_parent_ws guards (#8)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from herdr_workflow.config import Config
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.workflows import build_env, cleanup, inbox
from tests.fake_herdr import FakeHerdr


def _ws(label: str, ws_id: str, *, tabs: int = 1, panes: int = 1) -> dict[str, Any]:
    return {
        "workspace_id": ws_id,
        "number": 1,
        "label": label,
        "focused": False,
        "pane_count": panes,
        "tab_count": tabs,
        "active_tab_id": f"{ws_id}:t1",
        "agent_status": "unknown",
    }


def _tab(tab_id: str, ws_id: str, label: str) -> dict[str, Any]:
    return {
        "tab_id": tab_id,
        "workspace_id": ws_id,
        "number": 1,
        "label": label,
        "focused": False,
        "pane_count": 1,
        "agent_status": "unknown",
    }


def _pane(pane_id: str, ws_id: str, tab_id: str, label: str | None = None) -> dict[str, Any]:
    return {
        "pane_id": pane_id,
        "terminal_id": f"t-{pane_id}",
        "workspace_id": ws_id,
        "tab_id": tab_id,
        "focused": False,
        "agent_status": "unknown",
        "revision": 1,
        "label": label,
    }


def _agent(pane_id: str, ws_id: str, tab_id: str) -> dict[str, Any]:
    return {
        "pane_id": pane_id,
        "terminal_id": f"t-{pane_id}",
        "workspace_id": ws_id,
        "tab_id": tab_id,
        "focused": False,
        "agent_status": "idle",
        "revision": 1,
        "interactive_ready": True,
        "state_change_seq": 7,
    }


def _snapshot(**parts: Any) -> dict[str, Any]:
    return {
        "type": "session_snapshot",
        "snapshot": {
            "workspaces": parts.get("workspaces", []),
            "tabs": parts.get("tabs", []),
            "panes": parts.get("panes", []),
            "agents": parts.get("agents", []),
            "protocol": 17,
            "version": "0.0.0-test",
        },
    }


def _ok(fake: FakeHerdr, *methods: str) -> None:
    for method in methods:
        fake.on(method, {"type": "ok"})


# -- wq up -------------------------------------------------------------------


async def test_up_creates_the_workspace_when_absent(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    fake.on("session.snapshot", _snapshot())
    fake.on(
        "workspace.create",
        {
            "type": "workspace_created",
            "workspace": _ws("inbox", "w1"),
            "tab": _tab("w1:t1", "w1", "inbox"),
            "root_pane": _pane("w1:p1", "w1", "w1:t1"),
        },
    )
    fake.on("agent.list", {"type": "agent_list", "agents": []})
    _ok(fake, "tab.rename", "agent.start", "pane.rename", "workspace.focus", "pane.focus")

    result = await inbox.up(client, Config(), tmp_path)
    assert result.created_workspace is True
    assert result.started_router is True
    # The root tab is named after the workspace; the router needs its own name so the
    # next `up` can find it.
    assert fake.calls("tab.rename")[0]["params"]["label"] == "router"


async def test_up_is_idempotent_when_the_router_is_already_running(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    """The whole point of `up`: safe to bind to a key or a shell startup."""
    fake.on(
        "session.snapshot",
        _snapshot(
            workspaces=[_ws("inbox", "w1")],
            tabs=[_tab("w1:t1", "w1", "router")],
            panes=[_pane("w1:p1", "w1", "w1:t1", "router")],
        ),
    )
    fake.on("agent.list", {"type": "agent_list", "agents": [_agent("w1:p1", "w1", "w1:t1")]})
    _ok(fake, "workspace.focus", "pane.focus")

    result = await inbox.up(client, Config(), tmp_path)
    assert result.created_workspace is False
    assert result.started_router is False
    assert fake.calls("agent.start") == []
    assert fake.calls("workspace.create") == []


async def test_up_recreates_only_a_missing_router_tab(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    fake.on("session.snapshot", _snapshot(workspaces=[_ws("inbox", "w1")]))
    fake.on(
        "tab.create",
        {
            "type": "tab_created",
            "tab": _tab("w1:t2", "w1", "router"),
            "root_pane": _pane("w1:p2", "w1", "w1:t2"),
        },
    )
    fake.on("agent.list", {"type": "agent_list", "agents": []})
    _ok(fake, "agent.start", "pane.rename", "workspace.focus", "pane.focus")

    result = await inbox.up(client, Config(), tmp_path)
    assert result.created_workspace is False
    assert result.started_router is True
    assert fake.calls("workspace.create") == []


async def test_up_honours_a_custom_inbox_label(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WQ_INBOX_LABEL is what lets this be tested without touching a real inbox."""
    from herdr_workflow import config as config_module

    monkeypatch.setenv("WQ_INBOX_LABEL", "scratch-inbox")
    config = config_module.load(None)

    fake.on("session.snapshot", _snapshot())
    fake.on(
        "workspace.create",
        {
            "type": "workspace_created",
            "workspace": _ws("scratch-inbox", "w9"),
            "tab": _tab("w9:t1", "w9", "scratch-inbox"),
            "root_pane": _pane("w9:p1", "w9", "w9:t1"),
        },
    )
    fake.on("agent.list", {"type": "agent_list", "agents": []})
    _ok(fake, "tab.rename", "agent.start", "pane.rename", "workspace.focus", "pane.focus")

    await inbox.up(client, config, tmp_path)
    assert fake.calls("workspace.create")[0]["params"]["label"] == "scratch-inbox"


# -- build.env ---------------------------------------------------------------
# The first four lines are frozen for v0.1: Bash and Python run side by side during the
# cutover, so a build started by one has to be finishable by the other.


def test_build_env_reads_the_bash_four_line_form(tmp_path: Path) -> None:
    path = tmp_path / "build.env"
    path.write_text("/repo\nwq/slug\n/wt\nw7\n")
    env = build_env.read(path)
    assert (env.repo, env.branch, env.worktree, env.parent_workspace) == (
        "/repo",
        "wq/slug",
        "/wt",
        "w7",
    )


def test_a_bash_written_file_means_origin_main(tmp_path: Path) -> None:
    """Bash had no base line because it always meant `origin/main`. A missing line is that
    answer, not an unknown one -- anything else would diff a Bash-started build against a
    commit its branch was never cut from."""
    path = tmp_path / "build.env"
    path.write_text("/repo\nwq/slug\n/wt\nw7\n")
    assert build_env.read(path).base == "origin/main"


def test_build_env_tolerates_the_legacy_three_line_form(tmp_path: Path) -> None:
    """Files written before wq recorded the parent workspace have three lines, and the
    Bash reader tolerates that."""
    path = tmp_path / "build.env"
    path.write_text("/repo\nwq/slug\n/wt\n")
    env = build_env.read(path)
    assert env.parent_workspace is None
    assert env.worktree == "/wt"


def test_the_base_ref_survives_a_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "build.env"
    written = build_env.BuildEnv("/repo", "wq/slug", "/wt", "w7", "origin/develop")
    build_env.write(path, written)
    assert build_env.read(path) == written


def test_a_missing_parent_keeps_the_base_on_line_five(tmp_path: Path) -> None:
    """A positional format cannot afford an optional line in the middle of it, so an
    absent parent workspace is written as an empty line rather than skipped."""
    path = tmp_path / "build.env"
    build_env.write(path, build_env.BuildEnv("/repo", "wq/slug", "/wt", None, "origin/master"))
    assert path.read_text().splitlines() == ["/repo", "wq/slug", "/wt", "", "origin/master"]
    assert build_env.read(path).base == "origin/master"


def test_bash_can_still_read_a_python_written_file(tmp_path: Path) -> None:
    """Bash reads exactly four lines and ignores the rest, which is what makes a fifth
    line safe to add mid-cutover."""
    path = tmp_path / "build.env"
    build_env.write(path, build_env.BuildEnv("/repo", "wq/slug", "/wt", "w7", "origin/develop"))
    first_four = path.read_text().splitlines()[:4]
    assert first_four == ["/repo", "wq/slug", "/wt", "w7"]


# -- close_parent_ws (behavior #8) -------------------------------------------


async def test_parent_workspace_is_closed_when_untouched(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    fake.on("session.snapshot", _snapshot(workspaces=[_ws("some-repo", "w5")]))
    _ok(fake, "workspace.close")
    assert await cleanup.close_parent_ws(client, "w5") is True


async def test_parent_workspace_is_kept_once_it_has_been_split(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """Split, given a second tab, or given an agent: someone has adopted it."""
    fake.on("session.snapshot", _snapshot(workspaces=[_ws("some-repo", "w5", panes=2)]))
    assert await cleanup.close_parent_ws(client, "w5") is False
    assert fake.calls("workspace.close") == []


async def test_parent_workspace_is_kept_once_it_has_a_second_tab(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    fake.on("session.snapshot", _snapshot(workspaces=[_ws("some-repo", "w5", tabs=2)]))
    assert await cleanup.close_parent_ws(client, "w5") is False


async def test_parent_workspace_is_kept_when_an_agent_runs_in_it(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    fake.on(
        "session.snapshot",
        _snapshot(
            workspaces=[_ws("some-repo", "w5")],
            agents=[_agent("w5:p1", "w5", "w5:t1")],
        ),
    )
    assert await cleanup.close_parent_ws(client, "w5") is False


async def test_never_closes_the_workspace_the_command_runs_in(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wq go` run by hand from the repo's own tab lives in that pane. Closing it
    mid-cleanup is the self-destruct `wq ship` exists to avoid."""
    monkeypatch.setenv("HERDR_PANE_ID", "w5:p1")
    fake.on(
        "session.snapshot",
        _snapshot(workspaces=[_ws("some-repo", "w5")], panes=[_pane("w5:p1", "w5", "w5:t1")]),
    )
    assert await cleanup.close_parent_ws(client, "w5") is False
    assert fake.calls("workspace.close") == []


async def test_closing_a_different_workspace_from_a_pane_is_allowed(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is about *this* workspace, not about running in a pane at all."""
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    fake.on(
        "session.snapshot",
        _snapshot(
            workspaces=[_ws("some-repo", "w5")],
            panes=[_pane("w1:p1", "w1", "w1:t1"), _pane("w5:p1", "w5", "w5:t1")],
        ),
    )
    _ok(fake, "workspace.close")
    assert await cleanup.close_parent_ws(client, "w5") is True


async def test_close_parent_ws_never_raises(client: HerdrClient, fake: FakeHerdr) -> None:
    """It runs after irreversible work, so nothing below it may abort."""
    fake.on("session.snapshot", _snapshot(workspaces=[_ws("some-repo", "w5")]))
    fake.on_error("workspace.close", "not_found", "gone")
    assert await cleanup.close_parent_ws(client, "w5") is False


async def test_close_parent_ws_ignores_a_missing_id(client: HerdrClient) -> None:
    assert await cleanup.close_parent_ws(client, None) is False


# -- wq clean ----------------------------------------------------------------


async def test_clean_closes_every_shape_of_workspace_and_the_scratch_dir(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    root = tmp_path / "wq"
    (root / "alpha").mkdir(parents=True)
    (root / "alpha" / "plan.md").write_text("plan")

    fake.on(
        "session.snapshot",
        _snapshot(
            workspaces=[_ws("plan-alpha", "w1"), _ws("bs-alpha", "w2"), _ws("alpha", "w3")],
            tabs=[_tab("w9:t4", "w9", "ship-alpha")],
        ),
    )
    _ok(fake, "workspace.close", "tab.close")

    result = await cleanup.clean(client, "alpha", root)
    assert set(result.closed_workspaces) == {"plan-alpha", "bs-alpha", "alpha"}
    assert result.closed_tabs == ["ship-alpha"]
    assert result.removed_dir == root / "alpha"
    assert not (root / "alpha").exists()


async def test_clean_also_closes_the_parent_repo_workspace_from_build_env(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    """The parent workspace is labelled with the repo, not the slug, so the label loop
    cannot see it -- its id is line 4 of build.env."""
    root = tmp_path / "wq"
    (root / "alpha").mkdir(parents=True)
    (root / "alpha" / "build.env").write_text("/repo\nwq/alpha\n/wt\nw5\n")

    fake.on(
        "session.snapshot",
        _snapshot(workspaces=[_ws("alpha", "w3"), _ws("some-repo", "w5")]),
    )
    _ok(fake, "workspace.close")

    result = await cleanup.clean(client, "alpha", root)
    assert "w5 (parent repo)" in result.closed_workspaces


async def test_clean_of_an_unknown_slug_is_harmless(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    fake.on("session.snapshot", _snapshot())
    result = await cleanup.clean(client, "nothing-here", tmp_path)
    assert result.closed_workspaces == []
    assert result.removed_dir is None
