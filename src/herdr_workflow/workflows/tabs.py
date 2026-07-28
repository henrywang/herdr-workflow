"""`wq chat`, `wq ask`, `wq tidy` -- the inbox tabs.

chat and ask run in their own tab inside the inbox workspace, never in the router pane.
The router only classifies, so its context stays flat no matter how many questions you
ask; answers -- and the file reads behind them -- land in a tab you can close.

Neither blocks. You want to watch the answer arrive, not wait on a script. But delivery is
still confirmed: a dropped prompt in a tab you are not watching is exactly the failure
that hides.
"""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from herdr_workflow.config import Config
from herdr_workflow.errors import ApiError, WorkflowError
from herdr_workflow.herdr import ops
from herdr_workflow.herdr.agents import start_agent
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.herdr.delivery import deliver
from herdr_workflow.output import console
from herdr_workflow.workflows.inbox import inbox_workspace_id

CHAT_TAB = "chat"
ASK_PREFIX = "ask-"


@dataclass(frozen=True)
class TabResult:
    workspace_id: str
    tab_label: str
    pane_id: str
    created_tab: bool


async def chat(client: HerdrClient, config: Config, message: str, home: Path) -> TabResult:
    """Send a message to the long-lived chat tab, creating it if needed.

    One tab, reused. Follow-ups ("what about tomorrow?") only make sense with the previous
    turn still in context, and a fresh tab per throwaway question would bury the workspace
    in tabs.
    """
    if not message.strip():
        raise WorkflowError('usage: wq chat "<message>"')

    workspace_id = await inbox_workspace_id(client, config)
    snapshot = await client.snapshot()
    pane = ops.tab_pane_by_label(snapshot, workspace_id, CHAT_TAB)

    created = pane is None
    if pane is None:
        tab = await ops.tab_create(client, workspace_id, CHAT_TAB, home)
        pane = tab.root_pane.pane_id
        await start_agent(client, pane, config.agents.ask, CHAT_TAB, config)

    await deliver(client, pane, message, attempts=config.loops.prompt_attempts)
    await _focus(client, workspace_id, pane)
    return TabResult(workspace_id, CHAT_TAB, pane, created)


async def ask(client: HerdrClient, config: Config, question: str, cwd: Path) -> TabResult:
    """Ask a question in a fresh tab scoped to a directory.

    A new tab per question, because the file reads behind an answer are what would
    otherwise accumulate. Close them with `wq tidy`.
    """
    if not question.strip():
        raise WorkflowError('usage: wq ask "<question>"')

    workspace_id = await inbox_workspace_id(client, config)
    label = f"{ASK_PREFIX}{time.strftime('%H%M%S')}"

    tab = await ops.tab_create(client, workspace_id, label, cwd)
    pane = tab.root_pane.pane_id
    await start_agent(client, pane, config.agents.ask, label, config)

    await deliver(client, pane, question, attempts=config.loops.prompt_attempts)
    await _focus(client, workspace_id, pane)
    return TabResult(workspace_id, label, pane, True)


@dataclass(frozen=True)
class TidyResult:
    closed: list[str]
    kept_working: list[str]


async def tidy(client: HerdrClient, config: Config) -> TidyResult:
    """Close finished ask tabs; leave anything still working.

    ask tabs accumulate. `working` is the one status that means "do not touch this" -- a
    tab in `idle` or `done` has either answered or never started, and both are yours to
    close.
    """
    workspace_id = await inbox_workspace_id(client, config)
    snapshot = await client.snapshot()

    closed: list[str] = []
    kept: list[str] = []
    for tab in snapshot.tabs:
        if tab.workspace_id != workspace_id or not tab.label.startswith(ASK_PREFIX):
            continue
        if tab.agent_status == "working":
            kept.append(tab.label)
            continue
        try:
            await ops.tab_close(client, tab.tab_id)
            closed.append(tab.label)
        except ApiError as exc:
            console.detail(f"could not close {tab.label}: {exc.message}")

    return TidyResult(closed=closed, kept_working=kept)


async def _focus(client: HerdrClient, workspace_id: str, pane: str) -> None:
    """Bring the answer into view. Never fatal -- the prompt already landed."""
    try:
        await ops.pane_focus(client, pane)
    except ApiError:
        with suppress(ApiError):
            await ops.workspace_focus(client, workspace_id)
