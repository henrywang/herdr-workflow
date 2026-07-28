"""`wq clean <slug>` -- drop a slug's workspaces, tabs, and scratch dir.

Also home to `close_parent_ws`, which `wq go` reuses. It lives here rather than in the
ship workflow because cleanup is where it was first needed and both callers want the same
guards.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from herdr_workflow.errors import ApiError
from herdr_workflow.herdr import ops
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.output import console


def _empty() -> list[str]:
    return []


@dataclass
class CleanResult:
    closed_workspaces: list[str] = field(default_factory=_empty)
    closed_tabs: list[str] = field(default_factory=_empty)
    removed_dir: Path | None = None


def build_env_path(root: Path, slug: str) -> Path:
    return root / slug / "build.env"


def read_build_env(path: Path) -> tuple[str, str, str, str | None]:
    """Parse build.env: repo, branch, worktree path, parent workspace id.

    **The format is frozen for v0.1.** Bash and Python run side by side during the
    cutover, and an in-flight build written by one must be readable by the other.

    The fourth line is optional -- files written before wq recorded the parent workspace
    have only three, and the Bash reader tolerates that.
    """
    lines = path.read_text().splitlines()
    while len(lines) < 3:
        lines.append("")
    parent = lines[3].strip() if len(lines) > 3 and lines[3].strip() else None
    return lines[0].strip(), lines[1].strip(), lines[2].strip(), parent


async def close_parent_ws(client: HerdrClient, workspace_id: str | None) -> bool:
    """Close the parent-repo workspace `worktree.create` opened, if it is still ours.

    Behavior #8. Two guards, both load-bearing:

    - **Never close the workspace this command is running in.** `wq go` started by hand
      from the repo's own tab lives in that pane, and closing it mid-cleanup is the same
      self-destruct `wq ship` exists to avoid for the code pane.
    - **Only close it if it is still what wq left behind**: one tab, one pane, no agent.
      Once it has been split, given a second tab, or had an agent started in it, someone
      has adopted it and it is no longer wq's to close.

    Returns True if it closed something. Never raises: this runs after irreversible work.
    """
    if not workspace_id:
        return False

    try:
        snapshot = await client.snapshot()
    except Exception:
        return False

    current_pane = os.environ.get("HERDR_PANE_ID")
    if current_pane:
        here = next((p for p in snapshot.panes if p.pane_id == current_pane), None)
        if here is not None and here.workspace_id == workspace_id:
            console.detail(f"not closing {workspace_id}: this command is running in it")
            return False

    workspace = next((w for w in snapshot.workspaces if w.workspace_id == workspace_id), None)
    if workspace is None:
        return False

    if workspace.tab_count != 1 or workspace.pane_count != 1:
        console.detail(f"not closing {workspace_id}: it has been split or given more tabs")
        return False

    if any(a.workspace_id == workspace_id for a in snapshot.agents):
        console.detail(f"not closing {workspace_id}: an agent is running in it")
        return False

    try:
        await ops.workspace_close(client, workspace_id)
    except (ApiError, OSError):
        return False
    return True


async def clean(client: HerdrClient, slug: str, root: Path) -> CleanResult:
    result = CleanResult()
    snapshot = await client.snapshot()

    # `plan-` and `bs-` announce themselves by label; a build workspace is labelled with
    # the bare slug.
    for label in (f"plan-{slug}", f"bs-{slug}", slug):
        workspace = ops.workspace_by_label(snapshot, label)
        if workspace is None:
            continue
        try:
            await ops.workspace_close(client, workspace.workspace_id)
            result.closed_workspaces.append(label)
        except ApiError as exc:
            console.detail(f"could not close workspace {label}: {exc.message}")

    # The ship tab is a tab in the inbox, not a workspace of its own, so the loop above
    # never sees it.
    ship_tab = ops.tab_by_label(snapshot, f"ship-{slug}")
    if ship_tab is not None:
        try:
            await ops.tab_close(client, ship_tab)
            result.closed_tabs.append(f"ship-{slug}")
        except ApiError as exc:
            console.detail(f"could not close ship tab: {exc.message}")

    # The parent-repo workspace is labelled with the repo, not the slug, so the loop
    # cannot see it either -- its id is in build.env.
    env_path = build_env_path(root, slug)
    if env_path.is_file():
        _repo, _branch, _wt, parent = read_build_env(env_path)
        if await close_parent_ws(client, parent):
            result.closed_workspaces.append(f"{parent} (parent repo)")

    scratch = root / slug
    if scratch.is_dir():
        shutil.rmtree(scratch, ignore_errors=True)
        result.removed_dir = scratch

    return result
