"""`wq brainstorm` -- the one interactive command.

Two things carry the tests: it must **not** wait for the agent's turn (that would defeat
the point), and it must never clobber a note you have already been writing in.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from herdr_workflow.config import Config
from herdr_workflow.errors import ConfigError, WorkflowError
from herdr_workflow.herdr import delivery
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.workflows import brainstorming
from tests.fake_herdr import FakeHerdr


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "POLL_INTERVAL", 0.001)
    monkeypatch.setattr(delivery, "SETTLE_CONFIRM_DELAY", 0.001)


def _agent(seq: int) -> dict[str, Any]:
    return {
        "type": "agent_info",
        "agent": {
            "pane_id": "w4:p1",
            "terminal_id": "t1",
            "workspace_id": "w4",
            "tab_id": "w4:t1",
            "focused": False,
            "agent_status": "idle",
            "revision": 1,
            "state_change_seq": seq,
            "interactive_ready": True,
        },
    }


class Vault:
    """A fake herdr with a workspace to brainstorm in, and a counter for prompts sent."""

    def __init__(self, fake: FakeHerdr) -> None:
        self.fake = fake
        self.seq = 4
        self.prompts: list[str] = []

        fake.on(
            "workspace.create",
            {
                "type": "workspace_created",
                "workspace": {
                    "workspace_id": "w4",
                    "number": 4,
                    "label": "bs-x",
                    "focused": False,
                    "pane_count": 1,
                    "tab_count": 1,
                    "active_tab_id": "w4:t1",
                    "agent_status": "unknown",
                },
                "tab": None,
                "root_pane": {
                    "pane_id": "w4:p1",
                    "terminal_id": "t1",
                    "workspace_id": "w4",
                    "tab_id": "w4:t1",
                    "focused": False,
                    "agent_status": "idle",
                    "revision": 1,
                },
            },
        )
        for method in ("agent.start", "pane.rename", "workspace.focus"):
            fake.on(method, {"type": "ok"})
        fake.on("agent.get", self._on_get)
        fake.on("agent.read", {"type": "agent_read", "read": {"pane_id": "w4:p1", "text": "$ "}})
        fake.on("agent.prompt", self._on_prompt)

    def _on_get(self, _params: dict[str, Any]) -> dict[str, Any]:
        return _agent(self.seq)

    def _on_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        self.prompts.append(params["text"])
        self.seq += 1
        return _agent(self.seq - 1)


def _config(vault: Path | None) -> Config:
    base = Config()
    return replace(base, paths=replace(base.paths, notes=vault))


# -- the note ----------------------------------------------------------------


async def test_the_note_is_created_with_frontmatter(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    Vault(fake)
    result = await brainstorming.brainstorm(client, _config(tmp_path), "x", "a better cache")

    assert result.created_note is True
    body = result.note.read_text()
    assert body.startswith("---\n")
    assert "tags: [brainstorm]" in body
    assert "status: seed" in body
    assert "a better cache" in body
    assert result.note.parent.name == "inbox"


async def test_an_existing_note_is_never_overwritten(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    """Re-running the same slug on the same day is how you come back to an idea. Clobbering
    what you have written since would be the worst possible response."""
    note = brainstorming.note_path(tmp_path, "x")
    note.parent.mkdir(parents=True)
    note.write_text("---\ntags: [brainstorm]\n---\n\n# x\n\nhours of thinking\n")

    Vault(fake)
    result = await brainstorming.brainstorm(client, _config(tmp_path), "x", "a better cache")

    assert result.created_note is False
    assert "hours of thinking" in note.read_text()


def test_the_note_is_dated_and_slugged(tmp_path: Path) -> None:
    path = brainstorming.note_path(tmp_path, "cache", date(2026, 7, 28))
    assert path == tmp_path / "inbox" / "2026-07-28-cache.md"


# -- interactive by design ---------------------------------------------------


async def test_it_hands_back_the_pane_without_waiting_for_a_turn(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    """`deliver`, not `ask`. Waiting for the agent to finish thinking would defeat the
    entire point of the one command you are supposed to stay in the room for."""
    Vault(fake)
    await brainstorming.brainstorm(client, _config(tmp_path), "x", "an idea")

    assert fake.calls("agent.prompt")
    assert fake.calls("agent.wait") == []


async def test_there_is_no_reviewer(client: HerdrClient, fake: FakeHerdr, tmp_path: Path) -> None:
    """A critic at this stage kills the divergence you came for."""
    Vault(fake)
    await brainstorming.brainstorm(client, _config(tmp_path), "x", "an idea")

    assert len(fake.calls("agent.start")) == 1
    assert fake.calls("pane.split") == []


async def test_the_prompt_names_the_note_and_the_idea(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    vault = Vault(fake)
    result = await brainstorming.brainstorm(client, _config(tmp_path), "x", "a better cache")

    prompt = vault.prompts[0]
    assert str(result.note) in prompt
    assert "a better cache" in prompt
    assert "Preserve the YAML frontmatter" in prompt


async def test_the_workspace_is_labelled_and_rooted_in_the_vault(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    Vault(fake)
    await brainstorming.brainstorm(client, _config(tmp_path), "x", "an idea")

    params = fake.calls("workspace.create")[0]["params"]
    assert params["label"] == "bs-x"
    assert params["cwd"] == str(tmp_path)


async def test_a_focus_that_fails_does_not_fail_the_command(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    Vault(fake)
    fake.on_error("workspace.focus", "internal", "no")

    result = await brainstorming.brainstorm(client, _config(tmp_path), "x", "an idea")
    assert result.pane_id == "w4:p1"


# -- configuration -----------------------------------------------------------


async def test_an_unconfigured_notes_directory_says_how_to_set_it(
    client: HerdrClient, fake: FakeHerdr
) -> None:
    """The Bash implementation hard-coded an iCloud Obsidian path. That is exactly the
    personal assumption a shared tool must not ship with, so there is no default -- which
    makes this error the command's real front door."""
    with pytest.raises(ConfigError) as caught:
        await brainstorming.brainstorm(client, _config(None), "x", "an idea")
    assert "WQ_VAULT" in (caught.value.fix or "")
    assert "notes" in (caught.value.fix or "")
    assert fake.calls("workspace.create") == []


async def test_a_missing_notes_directory_is_not_created_silently(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    """Creating it would happily write notes into a typo."""
    with pytest.raises(WorkflowError) as caught:
        await brainstorming.brainstorm(client, _config(tmp_path / "nope"), "x", "an idea")
    assert "notes directory not found" in caught.value.message


async def test_an_empty_idea_is_refused(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    with pytest.raises(WorkflowError) as caught:
        await brainstorming.brainstorm(client, _config(tmp_path), "x", "   ")
    assert "describe the idea" in caught.value.message
    assert fake.calls("workspace.create") == []
