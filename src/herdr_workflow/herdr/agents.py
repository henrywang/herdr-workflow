"""Starting agents in panes.

Two behaviours from docs/behaviors.md live here.

**#3 -- a freshly created pane is not at a shell prompt yet.** `pane.split` returns as
soon as the pane exists, and starting an agent in it fails with `agent_pane_busy`. The gap
is short but real, so retry -- on that code only. A blanket retry turns a typo in a model
name into a long hang followed by a confusing message.

**#4 -- agent names are global in herdr.** A second concurrent workspace starting its own
`review` pane collides with `agent_name_taken`. Register under a name qualified by the
pane id, which is unique by construction, and keep the short name as the pane label.
This is only safe because nothing looks agents up by name.
"""

from __future__ import annotations

import asyncio
import re

from herdr_workflow.config import AgentSpec, Config
from herdr_workflow.errors import ApiError, WorkflowError
from herdr_workflow.herdr import ops
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.output import console

# herdr rejects anything outside this set in an agent name.
_NAME_SAFE = re.compile(r"[^a-z0-9_-]")

BUSY_ATTEMPTS = 15
BUSY_DELAY = 1.0


def qualified_name(short_name: str, pane_id: str) -> str:
    """`review` + `w2:p3` -> `review-w2-p3`.

    Lowercased and filtered because herdr rejects anything outside [a-z0-9_-], and pane
    ids contain colons and may contain uppercase.
    """
    return _NAME_SAFE.sub("-", f"{short_name}-{pane_id}".lower())


def spec_args(spec: AgentSpec, config: Config) -> list[str]:
    """CLI arguments for an agent kind.

    Claude takes `--effort` and needs a permission mode to run unattended; Pi takes
    `--thinking` and gates nothing.
    """
    if spec.kind == "claude":
        return [
            "--model",
            spec.model,
            "--effort",
            spec.level,
            "--permission-mode",
            config.claude.permission_mode,
        ]
    if spec.kind == "pi":
        return ["--model", spec.model, "--thinking", spec.level]
    raise WorkflowError(
        f"unsupported agent kind {spec.kind!r}",
        why="wq knows how to start claude and pi",
    )


async def start_agent(
    client: HerdrClient,
    pane_id: str,
    spec: AgentSpec,
    short_name: str,
    config: Config,
) -> str:
    """Start an agent in a pane and label the pane with its role.

    Returns the registered agent name. Retries `agent_pane_busy`; anything else fails
    immediately.
    """
    name = qualified_name(short_name, pane_id)
    args = spec_args(spec, config)

    for attempt in range(1, BUSY_ATTEMPTS + 1):
        try:
            await client.request(
                "agent.start",
                {"name": name, "kind": spec.kind, "pane_id": pane_id, "args": args},
            )
            break
        except ApiError as exc:
            if exc.code != "agent_pane_busy":
                raise
            if attempt == BUSY_ATTEMPTS:
                raise WorkflowError(
                    f"pane {pane_id} never reached a shell prompt",
                    why=f"agent.start returned agent_pane_busy {BUSY_ATTEMPTS} times",
                    fix=f"attach and look: herdr agent attach {pane_id}",
                ) from exc
            console.detail(f"pane {pane_id} busy, retrying ({attempt}/{BUSY_ATTEMPTS})")
            await asyncio.sleep(BUSY_DELAY)

    # The short name is the pane's label, which is how every later lookup finds it.
    # Best-effort: a missing label degrades discovery, it does not break the agent.
    try:
        await ops.pane_rename(client, pane_id, short_name)
    except ApiError:
        console.detail(f"could not label pane {pane_id} as {short_name}")

    return name
