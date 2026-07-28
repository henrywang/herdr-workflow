"""`wq build` -- the worktree, behavior #6, and the code <-> review loop.

The fake herdr does what herdr really does on `worktree.create`: it creates an actual git
worktree. That is what lets the diff, the round loop, and the "nothing was committed"
failure all be exercised for real, in milliseconds, with no model involved.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from herdr_workflow import config as config_module
from herdr_workflow.config import Config
from herdr_workflow.errors import GitError, WorkflowError
from herdr_workflow.herdr import delivery
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.workflows import build_env, building, prompts
from tests.conftest import git_run
from tests.fake_herdr import FakeHerdr

WS = "w9"


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "POLL_INTERVAL", 0.001)
    monkeypatch.setattr(delivery, "SETTLE_CONFIRM_DELAY", 0.001)


def _pane(pane_id: str, workspace_id: str = WS) -> dict[str, Any]:
    return {
        "pane_id": pane_id,
        "terminal_id": f"t-{pane_id}",
        "workspace_id": workspace_id,
        "tab_id": f"{workspace_id}:t1",
        "focused": False,
        "agent_status": "idle",
        "revision": 1,
    }


def _workspace(ws_id: str, label: str, worktree: dict[str, Any] | None = None) -> dict[str, Any]:
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


def _worktree_info(repo: Path, checkout: Path, *, linked: bool) -> dict[str, Any]:
    return {
        "repo_key": "k",
        "repo_name": repo.name,
        "repo_root": str(repo),
        "checkout_path": str(checkout),
        "is_linked_worktree": linked,
    }


def _agent(pane_id: str, status: str = "idle", seq: int = 4) -> dict[str, Any]:
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
        fake.on("pane.split", {"type": "pane_info", "pane": _pane(f"{WS}:p2")})
        for method in ("tab.rename", "pane.rename", "agent.start", "notification.show"):
            fake.on(method, {"type": "ok"})
        fake.on("agent.get", self._on_get)
        fake.on("agent.wait", self._on_wait)
        fake.on("agent.read", {"type": "agent_read", "read": {"pane_id": f"{WS}:p1", "text": "$ "}})
        fake.on("agent.prompt", self._on_prompt)

    # -- herdr -------------------------------------------------------------

    def _on_get(self, _params: dict[str, Any]) -> dict[str, Any]:
        return _agent(f"{WS}:p1", "idle", self.seq)

    def _on_wait(self, _params: dict[str, Any]) -> dict[str, Any]:
        return _agent(f"{WS}:p1", "done", self.seq)

    def _on_snapshot(self, _params: dict[str, Any]) -> dict[str, Any]:
        panes = [_pane(f"{WS}:p1"), _pane(f"{WS}:p2")] if self.worktree else []
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
            _workspace(WS, params["label"], _worktree_info(self.repo, path, linked=True))
        )
        self.workspaces.extend(self.extra)
        return {
            "type": "worktree_created",
            "workspace": self.workspaces[-1 - len(self.extra)],
            "root_pane": _pane(f"{WS}:p1"),
            "tab": {
                "tab_id": f"{WS}:t1",
                "workspace_id": WS,
                "number": 1,
                "label": params["label"],
                "focused": False,
                "pane_count": 1,
                "agent_status": "unknown",
            },
            "worktree": _worktree_info(self.repo, path, linked=True),
        }

    # -- agents ------------------------------------------------------------

    def _touch(self, path: Path, body: str) -> None:
        before = path.stat().st_mtime if path.exists() else 0.0
        path.write_text(body)
        os.utime(path, (before + 10, before + 10))

    def _on_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        pane, text = params["target"], params["text"]
        self.prompts.append((pane, text))
        self.seq += 1

        if "Review the diff" in text:
            verdict = self.reviews.pop(0) if self.reviews else prompts.VERDICT_APPROVED
            self._touch(self.paths.review, f"findings\n\n{verdict}")
        else:
            self.code_turns += 1
            if self.commits and self.worktree is not None:
                target = self.worktree / f"change{self.code_turns}.py"
                target.write_text(f"def v{self.code_turns}():\n    return {self.code_turns}\n")
                git_run(self.worktree, "add", ".")
                git_run(self.worktree, "commit", "-m", f"turn {self.code_turns}")
        return _agent(pane, "idle", self.seq - 1)


def _config(root: Path, rounds: int = 3) -> Config:
    base = Config()
    return replace(
        base,
        paths=replace(base.paths, root=root),
        loops=replace(base.loops, code_rounds=rounds, turn_timeout_ms=1000),
    )


def _with_plan(root: Path, slug: str = "x") -> building.BuildPaths:
    paths = building.BuildPaths.for_slug(root, slug)
    paths.dir.mkdir(parents=True, exist_ok=True)
    paths.plan.write_text("# Plan\n\nAdd a function.\n")
    return paths


# -- the loop ----------------------------------------------------------------


async def test_approval_in_round_one_stops_immediately(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    paths = _with_plan(root)
    scenario = Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    result = await building.build(client, _config(root), "x", repo)
    assert result.approved is True
    assert result.rounds == 1
    assert scenario.code_turns == 1  # implement, and no fix turn
    assert paths.diff.stat().st_size > 0


async def test_a_changes_verdict_triggers_a_fix_round(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    paths = _with_plan(root)
    scenario = Scenario(
        fake, paths, repo, reviews=[prompts.VERDICT_CHANGES, prompts.VERDICT_APPROVED]
    )

    result = await building.build(client, _config(root), "x", repo)
    assert result.approved is True
    assert result.rounds == 2
    assert scenario.code_turns == 2

    # The fix went to the code pane, not the reviewer.
    fixes = [p for p, t in scenario.prompts if "Fix every BLOCKING finding" in t]
    assert fixes == [f"{WS}:p1"]


async def test_the_diff_is_regenerated_before_every_review(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """The reviewer must see what the last fix turn committed, not the round before's."""
    root = tmp_path / "wq"
    paths = _with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_CHANGES, prompts.VERDICT_APPROVED])

    await building.build(client, _config(root), "x", repo)
    text = paths.diff.read_text()
    assert "change1.py" in text
    assert "change2.py" in text


async def test_the_round_cap_stops_a_loop_that_never_converges(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    paths = _with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_CHANGES] * 10)

    result = await building.build(client, _config(root, rounds=2), "x", repo)
    assert result.approved is False
    assert result.rounds == 2


async def test_wq_code_rounds_env_var_is_honoured(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "wq"
    monkeypatch.setenv("WQ_CODE_ROUNDS", "1")
    monkeypatch.setenv("WQ_ROOT", str(root))
    paths = _with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_CHANGES] * 10)

    result = await building.build(client, config_module.load(None), "x", repo)
    assert result.rounds == 1
    assert result.approved is False


# -- the gate ----------------------------------------------------------------


async def test_a_slug_with_no_plan_is_refused(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    with pytest.raises(WorkflowError) as caught:
        await building.build(client, _config(root), "x", repo)
    assert "no plan at" in caught.value.message
    assert fake.calls("worktree.create") == []


async def test_an_empty_plan_is_refused(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    paths = building.BuildPaths.for_slug(root, "x")
    paths.dir.mkdir(parents=True)
    paths.plan.write_text("")
    with pytest.raises(WorkflowError):
        await building.build(client, _config(root), "x", repo)


async def test_an_unapproved_plan_can_still_be_built(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """Deliberate, and inherited from Bash: a plan that hit its own round cap is still a
    plan, and building it anyway is the user's call rather than wq's."""
    root = tmp_path / "wq"
    paths = _with_plan(root)
    (paths.dir / "review.md").write_text(f"blocking findings\n\n{prompts.VERDICT_CHANGES}\n")
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    result = await building.build(client, _config(root), "x", repo)
    assert result.approved is True


async def test_a_non_repo_is_refused_before_anything_is_created(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    root = tmp_path / "wq"
    _with_plan(root)
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError) as caught:
        await building.build(client, _config(root), "x", plain)
    assert "not a git repository" in caught.value.message
    assert fake.calls("worktree.create") == []


async def test_an_agent_that_commits_nothing_fails_the_build(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """The reviewer reads the diff, so uncommitted work is invisible to it -- and would
    otherwise be reviewed as if nothing had been attempted."""
    root = tmp_path / "wq"
    paths = _with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED], commits=False)

    with pytest.raises(WorkflowError) as caught:
        await building.build(client, _config(root), "x", repo)
    assert "no changes committed on wq/x" in caught.value.message
    assert fake.calls("agent.prompt")  # it did try


# -- behavior #6: the workspace herdr does not report ------------------------


async def test_the_parent_workspace_is_found_and_recorded(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """`worktree.create` opens a second workspace for the parent checkout and reports only
    the first. Nothing else knows it exists, so every build would leak one."""
    root = tmp_path / "wq"
    paths = _with_plan(root)
    Scenario(
        fake,
        paths,
        repo,
        reviews=[prompts.VERDICT_APPROVED],
        extra_after_create=[_workspace("w10", repo.name, _worktree_info(repo, repo, linked=False))],
    )

    result = await building.build(client, _config(root), "x", repo)
    assert result.parent_workspace == "w10"
    assert build_env.read(paths.env).parent_workspace == "w10"


async def test_a_repo_root_behind_a_symlink_still_matches(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """On macOS /tmp is a symlink to /private/tmp, so herdr can report a path that is the
    same directory spelled differently. An unresolved comparison matches nothing --
    silently, and only in exactly the /tmp repositories used for live validation."""
    root = tmp_path / "wq"
    paths = _with_plan(root)
    link = tmp_path / "repo-link"
    link.symlink_to(repo)
    Scenario(
        fake,
        paths,
        repo,
        reviews=[prompts.VERDICT_APPROVED],
        extra_after_create=[_workspace("w10", repo.name, _worktree_info(link, link, linked=False))],
    )

    result = await building.build(client, _config(root), "x", repo)
    assert result.parent_workspace == "w10"


async def test_a_workspace_the_user_already_had_open_is_not_claimed(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """When the repository already had a workspace, herdr opens no second one -- and that
    workspace is the user's, not wq's to record or later close."""
    root = tmp_path / "wq"
    paths = _with_plan(root)
    Scenario(
        fake,
        paths,
        repo,
        reviews=[prompts.VERDICT_APPROVED],
        pre_existing=[_workspace("w3", repo.name, _worktree_info(repo, repo, linked=False))],
    )

    result = await building.build(client, _config(root), "x", repo)
    assert result.parent_workspace is None
    assert build_env.read(paths.env).parent_workspace is None


async def test_a_concurrent_workspace_for_another_repo_is_not_claimed(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """Another wq command opening its own workspace lands in the same diff. Picking by
    timing alone would record -- and later close -- someone else's workspace."""
    root = tmp_path / "wq"
    paths = _with_plan(root)
    other = tmp_path / "other-repo"
    Scenario(
        fake,
        paths,
        repo,
        reviews=[prompts.VERDICT_APPROVED],
        extra_after_create=[_workspace("w11", "other", _worktree_info(other, other, linked=False))],
    )

    result = await building.build(client, _config(root), "x", repo)
    assert result.parent_workspace is None


async def test_a_linked_worktree_is_never_the_parent(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """Another slug's build worktree has the same repo_root. `is_linked_worktree` is what
    separates the parent checkout from every worktree cut out of it."""
    root = tmp_path / "wq"
    paths = _with_plan(root)
    sibling = tmp_path / "repo-worktrees" / "other-slug"
    Scenario(
        fake,
        paths,
        repo,
        reviews=[prompts.VERDICT_APPROVED],
        extra_after_create=[
            _workspace("w12", "other-slug", _worktree_info(repo, sibling, linked=True))
        ],
    )

    result = await building.build(client, _config(root), "x", repo)
    assert result.parent_workspace is None


# -- outputs -----------------------------------------------------------------


async def test_the_build_env_records_everything_the_later_commands_need(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    paths = _with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    await building.build(client, _config(root), "x", repo)
    env = build_env.read(paths.env)
    assert env.repo == str(repo)
    assert env.branch == "wq/x"
    assert env.worktree == f"{repo}-worktrees/x"
    assert env.base == "origin/main"


async def test_the_code_review_does_not_overwrite_the_plan_review(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """A slug that was planned and then built has both files in one directory."""
    root = tmp_path / "wq"
    paths = _with_plan(root)
    plan_review = paths.dir / "review.md"
    plan_review.write_text("the plan review\n")
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    await building.build(client, _config(root), "x", repo)
    assert plan_review.read_text() == "the plan review\n"
    assert paths.review.name == "code-review.md"
    assert paths.review.is_file()


async def test_the_reviewer_is_given_paths_not_content(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    paths = _with_plan(root)
    paths.plan.write_text("# Plan\n\nSECRET PLAN BODY\n")
    scenario = Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    await building.build(client, _config(root), "x", repo)
    assert all("SECRET PLAN BODY" not in text for _pane_id, text in scenario.prompts)
    assert any(str(paths.diff) in text for _pane_id, text in scenario.prompts)


async def test_the_two_agents_are_different_models_in_their_own_panes(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    paths = _with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    await building.build(client, _config(root), "x", repo)
    starts = fake.calls("agent.start")
    assert len(starts) == 2
    kinds = {c["params"]["pane_id"]: c["params"]["args"] for c in starts}
    assert kinds[f"{WS}:p1"] != kinds[f"{WS}:p2"]
    assert {c["params"]["label"] for c in fake.calls("pane.rename")} == {"code", "review"}


async def test_the_worktree_is_branched_from_the_resolved_base(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """Bash hard-coded `origin/main`. The base is resolved, then used for both the branch
    point and the diff range -- one answer, so they cannot disagree."""
    root = tmp_path / "wq"
    paths = _with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    result = await building.build(client, _config(root), "x", repo)
    params = fake.calls("worktree.create")[0]["params"]
    assert params["base"] == result.base
    assert params["branch"] == "wq/x"
    assert params["label"] == "x"
    assert params["focus"] is False
