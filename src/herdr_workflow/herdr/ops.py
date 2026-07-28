"""Typed wrappers over the herdr methods wq uses, plus snapshot lookups.

Two conventions here are load-bearing:

**Agents are targeted by pane id, never by name.** Names are global in herdr and not
unique across concurrent workspaces; pane ids are unique by construction.

**Panes are found by label from the snapshot, not from stored ids.** `start_agent`
renames each pane to its short role name and the workspace carries the slug, so the
snapshot already knows where everything is. Nothing to persist, nothing to drift.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from herdr_workflow.errors import ApiError, HerdrError
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.protocol.messages import (
    Agent,
    AgentListResult,
    PaneResult,
    Snapshot,
    TabCreated,
    Workspace,
    WorkspaceCreated,
    WorktreeCreated,
)

# -- lookups over a snapshot -------------------------------------------------
# Taking a Snapshot rather than a client, so a command that needs several lookups pays
# for one round trip instead of one per question.


def workspace_by_label(snapshot: Snapshot, label: str) -> Workspace | None:
    return next((w for w in snapshot.workspaces if w.label == label), None)


def tab_pane_by_label(snapshot: Snapshot, workspace_id: str, label: str) -> str | None:
    """The first pane of the tab labelled `label` in `workspace_id`."""
    tab = next(
        (t for t in snapshot.tabs if t.workspace_id == workspace_id and t.label == label),
        None,
    )
    if tab is None:
        return None
    pane = next((p for p in snapshot.panes if p.tab_id == tab.tab_id), None)
    return pane.pane_id if pane else None


def workspace_pane_by_label(snapshot: Snapshot, ws_label: str, pane_label: str) -> str | None:
    """A pane by its label, inside the workspace with `ws_label`.

    How `revise` finds the `code` and `review` panes of a build.
    """
    workspace = workspace_by_label(snapshot, ws_label)
    if workspace is None:
        return None
    pane = next(
        (
            p
            for p in snapshot.panes
            if p.workspace_id == workspace.workspace_id and p.label == pane_label
        ),
        None,
    )
    return pane.pane_id if pane else None


def agent_on_pane(snapshot: Snapshot, pane_id: str) -> Agent | None:
    return next((a for a in snapshot.agents if a.pane_id == pane_id), None)


def tab_by_label(snapshot: Snapshot, label: str) -> str | None:
    tab = next((t for t in snapshot.tabs if t.label == label), None)
    return tab.tab_id if tab else None


# -- workspaces --------------------------------------------------------------


async def workspace_create(client: HerdrClient, label: str, cwd: Path) -> WorkspaceCreated:
    return await client.call(
        "workspace.create",
        WorkspaceCreated,
        {"label": label, "cwd": str(cwd), "focus": False},
    )


async def workspace_close(client: HerdrClient, workspace_id: str) -> None:
    await client.request("workspace.close", {"workspace_id": workspace_id})


async def workspace_focus(client: HerdrClient, workspace_id: str) -> None:
    await client.request("workspace.focus", {"workspace_id": workspace_id})


async def worktree_create(
    client: HerdrClient,
    *,
    repo: Path,
    branch: str,
    base: str,
    path: Path,
    label: str,
) -> WorktreeCreated:
    return await client.call(
        "worktree.create",
        WorktreeCreated,
        {
            "cwd": str(repo),
            "branch": branch,
            "base": base,
            "path": str(path),
            "label": label,
            "focus": False,
        },
    )


async def worktree_remove(client: HerdrClient, workspace_id: str) -> None:
    """Ask herdr to drop a worktree workspace and its checkout."""
    await client.request("worktree.remove", {"workspace_id": workspace_id, "force": True})


async def workspace_ids(client: HerdrClient) -> set[str]:
    """Every open workspace id.

    Used to diff around `worktree.create`, which opens a workspace it does not report --
    behavior #6.
    """
    snapshot = await client.snapshot()
    return {w.workspace_id for w in snapshot.workspaces}


# -- tabs --------------------------------------------------------------------


async def tab_create(client: HerdrClient, workspace_id: str, label: str, cwd: Path) -> TabCreated:
    return await client.call(
        "tab.create",
        TabCreated,
        {"workspace_id": workspace_id, "label": label, "cwd": str(cwd), "focus": False},
    )


async def tab_close(client: HerdrClient, tab_id: str) -> None:
    await client.request("tab.close", {"tab_id": tab_id})


async def tab_rename(client: HerdrClient, tab_id: str, label: str) -> None:
    await client.request("tab.rename", {"tab_id": tab_id, "label": label})


# -- panes -------------------------------------------------------------------


async def pane_split(client: HerdrClient, pane_id: str, cwd: Path) -> str:
    result = await client.call(
        "pane.split",
        PaneResult,
        {"target_pane_id": pane_id, "direction": "right", "cwd": str(cwd), "focus": False},
    )
    return result.pane.pane_id


async def pane_focus(client: HerdrClient, pane_id: str) -> None:
    await client.request("pane.focus", {"pane_id": pane_id})


async def pane_rename(client: HerdrClient, pane_id: str, label: str) -> None:
    await client.request("pane.rename", {"pane_id": pane_id, "label": label})


async def pane_get(client: HerdrClient, pane_id: str) -> PaneResult:
    return await client.call("pane.get", PaneResult, {"pane_id": pane_id})


async def pane_run(client: HerdrClient, pane_id: str, command: str) -> None:
    """Type a command into a pane's shell and press Enter.

    What the `herdr pane run` CLI does, in one socket call -- verified live: text and keys
    together execute the command. Returns as soon as the keystrokes are delivered, which is
    the point: `wq ship` hands a long-running command to a tab and frees its caller.

    **The command is a shell line, so quoting is the caller's job.** There is no argv here
    to keep arguments apart.

    Socket failures arrive as error responses.
    """
    await client.request(
        "pane.send_input", {"pane_id": pane_id, "text": command, "keys": ["enter"]}
    )


# -- agents ------------------------------------------------------------------


async def agent_list(client: HerdrClient) -> list[Agent]:
    result = await client.call("agent.list", AgentListResult)
    return result.agents


async def agent_by_pane(client: HerdrClient, pane_id: str) -> Agent | None:
    return next((a for a in await agent_list(client) if a.pane_id == pane_id), None)


# -- notifications -----------------------------------------------------------


async def notify(client: HerdrClient, title: str, body: str) -> None:
    """Tell the user a long-running loop finished. Never fatal.

    A blocking command that has already done its work must not fail because a toast could
    not be shown.
    """
    with suppress(ApiError, HerdrError):
        await client.request("notification.show", {"title": title, "body": body, "sound": "done"})
