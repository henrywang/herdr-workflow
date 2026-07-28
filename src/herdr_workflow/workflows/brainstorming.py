"""`wq brainstorm <slug> "<idea>"` -- a workspace, a note, and one agent to think with.

**Interactive by design, and the only command here that is.** Everything else in wq blocks
until agents finish; this one delivers the opening prompt and hands you the pane. A critic
at this stage kills the divergence you came for, so there is no review loop and no second
model -- just somewhere to think, with the note kept current as you talk.

The note lives in a directory *you* choose. The Bash implementation hard-coded an iCloud
Obsidian path, which is exactly the personal assumption a shared tool must not make, so
`paths.notes` has no default and the error when it is unset says how to set it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from herdr_workflow.config import Config
from herdr_workflow.errors import ConfigError, WorkflowError
from herdr_workflow.herdr import ops
from herdr_workflow.herdr.agents import start_agent
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.herdr.delivery import deliver
from herdr_workflow.output import console

INBOX_DIR = "inbox"


@dataclass(frozen=True)
class BrainstormResult:
    slug: str
    workspace_id: str
    pane_id: str
    note: Path
    created_note: bool


def note_path(vault: Path, slug: str, today: date | None = None) -> Path:
    stamp = (today or date.today()).isoformat()
    return vault / INBOX_DIR / f"{stamp}-{slug}.md"


def _frontmatter(slug: str, idea: str, today: date) -> str:
    """The note wq creates. Frontmatter first, because the prompt tells the agent to
    preserve it and an agent cannot preserve what was never there."""
    return (
        "---\n"
        "tags: [brainstorm]\n"
        f"date: {today.isoformat()}\n"
        "status: seed\n"
        "---\n"
        "\n"
        f"# {slug}\n"
        "\n"
        f"{idea}\n"
    )


def opening_prompt(note: Path, idea: str) -> str:
    return (
        f"We are brainstorming. Keep the running note at {note} current as we talk: "
        "structure it, keep the good branches, prune the dead ones, and record open "
        "questions. Do not ask permission before each edit. Preserve the YAML frontmatter. "
        "Start by expanding on this idea and offering three angles I have not "
        f"considered:\n\n{idea}"
    )


def resolve_vault(config: Config) -> Path:
    vault = config.paths.notes
    if vault is None:
        raise ConfigError(
            "no notes directory configured",
            why="brainstorm keeps a running note, and wq does not guess where you keep notes",
            fix='set WQ_VAULT=<dir>, or [paths] notes = "<dir>" in ~/.config/wq/config.toml',
        )
    vault = vault.expanduser()
    if not vault.is_dir():
        raise WorkflowError(
            f"notes directory not found: {vault}",
            why="the configured notes directory does not exist",
            fix="create it, or point WQ_VAULT somewhere that exists",
        )
    return vault


async def brainstorm(client: HerdrClient, config: Config, slug: str, idea: str) -> BrainstormResult:
    if not idea.strip():
        raise WorkflowError(
            "describe the idea",
            why="brainstorm needs something to start from",
            fix=f'run: wq brainstorm {slug} "..."',
        )

    vault = resolve_vault(config)
    note = note_path(vault, slug)
    note.parent.mkdir(parents=True, exist_ok=True)

    # Never overwrite. Re-running against the same slug on the same day is how you come
    # back to an idea, and clobbering the note would be the worst possible response.
    created = not note.exists()
    if created:
        note.write_text(_frontmatter(slug, idea, date.today()))

    workspace = await ops.workspace_create(client, f"bs-{slug}", vault)
    workspace_id = workspace.workspace.workspace_id
    pane = workspace.root_pane.pane_id
    await start_agent(client, pane, config.agents.idea, "idea", config)

    # `deliver`, not `ask`: confirm the prompt landed, then hand the pane over. Waiting for
    # the turn to finish would defeat the point of an interactive command.
    await deliver(client, pane, opening_prompt(note, idea), attempts=config.loops.prompt_attempts)

    # Focus last, and never fatally: the pane is ready either way, and a focus that fails
    # is a nuisance rather than a failure.
    try:
        await ops.workspace_focus(client, workspace_id)
    except Exception:
        console.detail(f"could not focus workspace {workspace_id}")

    console.log(f"brainstorm pane ready — note: {note}")
    return BrainstormResult(
        slug=slug,
        workspace_id=workspace_id,
        pane_id=pane,
        note=note,
        created_note=created,
    )
