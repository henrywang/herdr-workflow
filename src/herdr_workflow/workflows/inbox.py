"""`wq up` -- bring up the inbox and its router.

Idempotent by design: every step checks before it acts, so running this when everything is
already fine costs one snapshot and changes nothing. That is what makes it safe to bind to
a key or a shell startup, which is exactly what devcage-macos does.

The router only classifies and dispatches, so its context stays flat no matter how many
questions you ask. Answers -- and the file reads behind them -- land in tabs you can close.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from herdr_workflow.config import Config
from herdr_workflow.errors import WorkflowError
from herdr_workflow.herdr import ops
from herdr_workflow.herdr.agents import start_agent
from herdr_workflow.herdr.client import HerdrClient

ROUTER_TAB = "router"


@dataclass(frozen=True)
class UpResult:
    workspace_id: str
    pane_id: str
    created_workspace: bool
    started_router: bool


async def inbox_workspace_id(client: HerdrClient, config: Config) -> str:
    """The inbox workspace, or a message explaining how to make one."""
    snapshot = await client.snapshot()
    workspace = ops.workspace_by_label(snapshot, config.herdr.inbox_label)
    if workspace is None:
        raise WorkflowError(
            f"no workspace labelled '{config.herdr.inbox_label}'",
            why="chat, ask, tidy and ship all run in tabs of the inbox workspace",
            fix="create it with: wq up",
        )
    return workspace.workspace_id


async def up(client: HerdrClient, config: Config, home: Path) -> UpResult:
    snapshot = await client.snapshot()
    workspace = ops.workspace_by_label(snapshot, config.herdr.inbox_label)

    created_workspace = False
    if workspace is None:
        created = await ops.workspace_create(client, config.herdr.inbox_label, home)
        workspace_id = created.workspace.workspace_id
        pane_id = created.root_pane.pane_id
        created_workspace = True
        # The root tab is named after the workspace; the router wants its own name so
        # `tab_pane_by_label` can find it next time.
        if created.tab is not None:
            await ops.tab_rename(client, created.tab.tab_id, ROUTER_TAB)
        else:
            pane = await ops.pane_get(client, pane_id)
            await ops.tab_rename(client, pane.pane.tab_id, ROUTER_TAB)
    else:
        workspace_id = workspace.workspace_id
        found = ops.tab_pane_by_label(snapshot, workspace_id, ROUTER_TAB)
        if found is None:
            tab = await ops.tab_create(client, workspace_id, ROUTER_TAB, home)
            pane_id = tab.root_pane.pane_id
        else:
            pane_id = found

    # Check for a live agent rather than assuming: `up` is meant to be run repeatedly, and
    # starting a second router in an occupied pane is not idempotent.
    started_router = await ops.agent_by_pane(client, pane_id) is None
    if started_router:
        await start_agent(client, pane_id, config.agents.router, ROUTER_TAB, config)

    await ops.workspace_focus(client, workspace_id)
    await ops.pane_focus(client, pane_id)

    return UpResult(
        workspace_id=workspace_id,
        pane_id=pane_id,
        created_workspace=created_workspace,
        started_router=started_router,
    )
