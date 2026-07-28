"""A built state, on demand.

`build` and `revise` both need a repository with a worktree, two labelled panes, and
agents that really commit -- so the scenario that produces one lives here rather than in
whichever test file happened to need it first.

The fake does what herdr really does on `worktree.create`: it creates an actual git
worktree. That is what lets the diff, the round loop, and the "nothing was committed"
failure all be exercised for real, in milliseconds, with no model involved.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from herdr_workflow.config import Config
from herdr_workflow.workflows import building, prompts
from tests.conftest import git_run
from tests.fake_herdr import FakeHerdr

WS = "w9"


def pane(pane_id: str, workspace_id: str = WS, label: str | None = None) -> dict[str, Any]:
    """A pane. `label` is what `revise` finds its panes by, so the snapshot must carry it."""
    body: dict[str, Any] = {
        "pane_id": pane_id,
        "terminal_id": f"t-{pane_id}",
        "workspace_id": workspace_id,
        "tab_id": f"{workspace_id}:t1",
        "focused": False,
        "agent_status": "idle",
        "revision": 1,
    }
    if label is not None:
        body["label"] = label
    return body


def workspace(ws_id: str, label: str, worktree: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "workspace_id": ws_id,
        "number": 1,
        "label": label,
        "focused": False,
        "pane_count": 1,
        "tab_count": 1,
        "active_tab_id": f"{ws_id}:t1",
        "agent_status": "idle",
    }
    if worktree is not None:
        body["worktree"] = worktree
    return body


def worktree_info(repo: Path, checkout: Path, *, linked: bool) -> dict[str, Any]:
    """The worktree hanging off a *workspace*."""
    return {
        "repo_key": "k",
        "repo_name": repo.name,
        "repo_root": str(repo),
        "checkout_path": str(checkout),
        "is_linked_worktree": linked,
    }


def created_worktree(checkout: Path, label: str) -> dict[str, Any]:
    """The worktree `worktree.create` reports -- a different struct with the same name.

    Copied from a real herdr 0.7.5 response. It shares only `is_linked_worktree` with the
    workspace's version, and getting the two confused is what broke the first live run of
    this command (behavior #12). The fake sends the real shape so the tests can disagree
    with wq rather than agreeing with its mistakes.
    """
    return {
        "path": str(checkout),
        "label": label,
        "branch": f"wq/{label}",
        "is_bare": False,
        "is_detached": False,
        "is_prunable": False,
        "is_linked_worktree": True,
        "open_workspace_id": WS,
    }


def agent_info(pane_id: str, status: str = "idle", seq: int = 4) -> dict[str, Any]:
    return {
        "type": "agent_info",
        "agent": {
            "pane_id": pane_id,
            "terminal_id": "t1",
            "workspace_id": WS,
            "tab_id": f"{WS}:t1",
            "focused": False,
            "agent_status": status,
            "revision": 1,
            "state_change_seq": seq,
            "interactive_ready": True,
        },
    }


class Scenario:
    """A fake herdr that really creates the worktree, and fake agents that really commit.

    `extra_after_create` is how behavior #6 is set up: the workspaces herdr opens *besides*
    the one it reports.
    """

    def __init__(
        self,
        fake: FakeHerdr,
        paths: building.BuildPaths,
        repo: Path,
        *,
        reviews: list[str],
        extra_after_create: list[dict[str, Any]] | None = None,
        pre_existing: list[dict[str, Any]] | None = None,
        commits: bool = True,
    ) -> None:
        self.fake = fake
        self.paths = paths
        self.repo = repo
        self.reviews = list(reviews)
        self.extra = list(extra_after_create or [])
        self.commits = commits
        self.seq = 4
        self.prompts: list[tuple[str, str]] = []
        self.code_turns = 0
        self.worktree: Path | None = None
        self.workspaces: list[dict[str, Any]] = list(pre_existing or [])

        fake.on("session.snapshot", self._on_snapshot)
        fake.on("worktree.create", self._on_worktree_create)
        fake.on("pane.split", {"type": "pane_info", "pane": pane(f"{WS}:p2")})
        for method in ("tab.rename", "pane.rename", "agent.start", "notification.show"):
            fake.on(method, {"type": "ok"})
        fake.on("agent.get", self._on_get)
        fake.on("agent.wait", self._on_wait)
        fake.on("agent.read", {"type": "agent_read", "read": {"pane_id": f"{WS}:p1", "text": "$ "}})
        fake.on("agent.prompt", self._on_prompt)

    # -- herdr -------------------------------------------------------------

    def _on_get(self, _params: dict[str, Any]) -> dict[str, Any]:
        return agent_info(f"{WS}:p1", "idle", self.seq)

    def _on_wait(self, _params: dict[str, Any]) -> dict[str, Any]:
        return agent_info(f"{WS}:p1", "done", self.seq)

    def _on_snapshot(self, _params: dict[str, Any]) -> dict[str, Any]:
        # Labelled once the agents are started, which is what `revise` looks them up by.
        panes = (
            [pane(f"{WS}:p1", label="code"), pane(f"{WS}:p2", label="review")]
            if self.worktree
            else []
        )
        return {
            "type": "session_snapshot",
            "snapshot": {
                "workspaces": self.workspaces,
                "tabs": [],
                "panes": panes,
                "agents": [],
                "protocol": 17,
                "version": "0.0.0-test",
            },
        }

    def _on_worktree_create(self, params: dict[str, Any]) -> dict[str, Any]:
        """Create the worktree for real, as herdr does."""
        path = Path(params["path"])
        git_run(self.repo, "worktree", "add", "-b", params["branch"], str(path), params["base"])
        self.worktree = path
        self.workspaces.append(
            workspace(WS, params["label"], worktree_info(self.repo, path, linked=True))
        )
        self.workspaces.extend(self.extra)
        return {
            "type": "worktree_created",
            "workspace": self.workspaces[-1 - len(self.extra)],
            "root_pane": pane(f"{WS}:p1"),
            "tab": {
                "tab_id": f"{WS}:t1",
                "workspace_id": WS,
                "number": 1,
                "label": params["label"],
                "focused": False,
                "pane_count": 1,
                "agent_status": "unknown",
            },
            "worktree": created_worktree(path, params["label"]),
        }

    # -- agents ------------------------------------------------------------

    def _touch(self, path: Path, body: str) -> None:
        before = path.stat().st_mtime if path.exists() else 0.0
        path.write_text(body)
        os.utime(path, (before + 10, before + 10))

    def _on_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        # `target`, not `pane`: `pane` is a function in this module.
        target, text = params["target"], params["text"]
        self.prompts.append((target, text))
        self.seq += 1

        # "Write your review to ..." is what both review prompts have in common and no code
        # prompt has -- `fix_findings` names the review file too, but only to read it.
        if "Write your review to" in text:
            verdict = self.reviews.pop(0) if self.reviews else prompts.VERDICT_APPROVED
            self._touch(self.paths.review, f"findings\n\n{verdict}")
        else:
            self.code_turns += 1
            if self.commits and self.worktree is not None:
                changed = self.worktree / f"change{self.code_turns}.py"
                changed.write_text(f"def v{self.code_turns}():\n    return {self.code_turns}\n")
                git_run(self.worktree, "add", ".")
                git_run(self.worktree, "commit", "-m", f"turn {self.code_turns}")
        return agent_info(target, "idle", self.seq - 1)


def build_config(root: Path, rounds: int = 3) -> Config:
    base = Config()
    return replace(
        base,
        paths=replace(base.paths, root=root),
        loops=replace(base.loops, code_rounds=rounds, turn_timeout_ms=1000),
    )


def with_plan(root: Path, slug: str = "x") -> building.BuildPaths:
    paths = building.BuildPaths.for_slug(root, slug)
    paths.dir.mkdir(parents=True, exist_ok=True)
    paths.plan.write_text("# Plan\n\nAdd a function.\n")
    return paths
