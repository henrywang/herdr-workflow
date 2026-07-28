"""`wq ship` and `wq go`.

`go` runs against a **fake `gh` on PATH** rather than patched wrapper functions, so the
tests see the argv wq actually builds. That is what lets `--delete-branch`'s absence
(behavior #7) be asserted rather than trusted, and it is the same reasoning that put a real
socket behind the fake herdr.

The post-merge section is the most valuable set here: given a merge that has landed, every
cleanup step must be able to fail *individually* without changing the outcome.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from herdr_workflow import git
from herdr_workflow.errors import WorkflowError
from herdr_workflow.git import gh
from herdr_workflow.herdr import delivery
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.workflows import building, prompts, shipping
from tests.build_scenario import WS, Scenario, build_config, with_plan
from tests.conftest import git_run
from tests.fake_gh import FakeGh, working_gh
from tests.fake_herdr import FakeHerdr

PR = 7


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "POLL_INTERVAL", 0.001)
    monkeypatch.setattr(delivery, "SETTLE_CONFIRM_DELAY", 0.001)
    monkeypatch.setattr(gh, "CHECK_POLL_SECONDS", 0.001)


@pytest.fixture(autouse=True)
def no_inherited_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests run under Claude Code, which may itself be in a herdr pane."""
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)


async def _built(
    client: HerdrClient, fake: FakeHerdr, root: Path, repo: Path
) -> tuple[Any, Scenario, building.BuildPaths]:
    config = build_config(root)
    paths = with_plan(root)
    scenario = Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])
    await building.build(client, config, "x", repo)
    return config, scenario, paths


# -- ship --------------------------------------------------------------------


def _inbox(fake: FakeHerdr, tabs: list[dict[str, Any]] | None = None) -> None:
    fake.on(
        "session.snapshot",
        {
            "type": "session_snapshot",
            "snapshot": {
                "workspaces": [
                    {
                        "workspace_id": "w1",
                        "number": 1,
                        "label": "inbox",
                        "focused": True,
                        "pane_count": 1,
                        "tab_count": 1,
                        "active_tab_id": "w1:t1",
                        "agent_status": "idle",
                    }
                ],
                "tabs": tabs or [],
                "panes": [
                    {
                        "pane_id": "w1:p9",
                        "terminal_id": "t9",
                        "workspace_id": "w1",
                        "tab_id": "w1:t9",
                        "focused": False,
                        "agent_status": "idle",
                        "revision": 1,
                    }
                ],
                "agents": [],
                "protocol": 17,
                "version": "0.0.0-test",
            },
        },
    )


def _with_build_env(root: Path, slug: str, repo: Path) -> building.BuildPaths:
    from herdr_workflow.workflows import build_env

    paths = building.BuildPaths.for_slug(root, slug)
    paths.dir.mkdir(parents=True, exist_ok=True)
    build_env.write(
        paths.env,
        build_env.BuildEnv(
            str(repo), f"wq/{slug}", f"{repo}-worktrees/{slug}", "w5", "origin/main"
        ),
    )
    return paths


async def test_ship_types_the_command_into_a_new_tab(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    _with_build_env(root, "x", repo)
    _inbox(fake)
    fake.on(
        "tab.create",
        {
            "type": "tab_created",
            "root_pane": {
                "pane_id": "w1:p4",
                "terminal_id": "t4",
                "workspace_id": "w1",
                "tab_id": "w1:t4",
                "focused": False,
                "agent_status": "idle",
                "revision": 1,
            },
            "tab": None,
        },
    )
    fake.on("pane.send_input", {"type": "ok"})
    fake.on("pane.focus", {"type": "ok"})

    result = await shipping.ship(client, build_config(root), "x", tmp_path)

    assert result.created_tab is True
    assert result.tab_label == "ship-x"
    sent = fake.calls("pane.send_input")[0]["params"]
    assert sent["pane_id"] == "w1:p4"
    assert sent["keys"] == ["enter"]
    assert sent["text"].endswith("go x")


async def test_ship_reuses_an_existing_ship_tab(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """A second ship of the same slug must not stack up tabs."""
    root = tmp_path / "wq"
    _with_build_env(root, "x", repo)
    _inbox(
        fake,
        tabs=[
            {
                "tab_id": "w1:t9",
                "workspace_id": "w1",
                "number": 9,
                "label": "ship-x",
                "focused": False,
                "pane_count": 1,
                "agent_status": "idle",
            }
        ],
    )
    fake.on("pane.send_input", {"type": "ok"})
    fake.on("pane.focus", {"type": "ok"})

    result = await shipping.ship(client, build_config(root), "x", tmp_path)
    assert result.created_tab is False
    assert result.pane_id == "w1:p9"
    assert fake.calls("tab.create") == []


async def test_ship_refuses_a_slug_with_no_build(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    with pytest.raises(WorkflowError) as caught:
        await shipping.ship(client, build_config(tmp_path / "wq"), "never", tmp_path)
    assert "no build for never" in caught.value.message
    assert fake.calls("tab.create") == []


def test_the_shipped_command_quotes_everything(tmp_path: Path) -> None:
    """This is a shell line typed into a pane, not an argv -- and the slug comes from a
    router repeating a human's words. Nothing here is trusted."""
    command = shipping.ship_command("weird; rm -rf /", tmp_path / "a dir")
    assert "; rm -rf /" not in command.replace("'weird; rm -rf /'", "")
    assert "'weird; rm -rf /'" in command
    assert "'" + str(tmp_path / "a dir") + "'" in command


def test_the_shipped_command_carries_the_scratch_root(tmp_path: Path) -> None:
    """A deliberate divergence from Bash. The ship tab is a fresh shell, so a WQ_ROOT the
    caller set would otherwise be lost and `go` would look for a build that, from where it
    is standing, does not exist."""
    import shlex

    command = shipping.ship_command("x", tmp_path / "root")
    assert command.startswith(f"WQ_ROOT={shlex.quote(str(tmp_path / 'root'))} ")
    assert command.endswith(" go x")


# -- the agent-pane guard ----------------------------------------------------


async def test_go_refuses_to_run_in_an_agent_pane(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router's prompt says to call `wq ship` and never this. A prompt is advice; this
    command pushes, merges and deletes branches, so the rule is enforced here too."""
    root = tmp_path / "wq"
    _with_build_env(root, "x", repo)
    working_gh(tmp_path / "bin", monkeypatch)
    monkeypatch.setenv("HERDR_PANE_ID", "w3:p1")
    fake.on(
        "agent.list",
        {"type": "agent_list", "agents": [_agent_row("w3:p1")]},
    )

    with pytest.raises(WorkflowError) as caught:
        await shipping.go(client, build_config(root), "x")
    assert "cannot run in an agent pane" in caught.value.message
    assert "wq ship x" in (caught.value.fix or "")


async def test_a_plain_shell_pane_passes_straight_through(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tab `wq ship` opens has a pane id but no agent -- the sanctioned path."""
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p4")
    fake.on("agent.list", {"type": "agent_list", "agents": [_agent_row("w3:p1")]})
    assert await shipping.running_in_agent_pane(client) is False


async def test_the_guard_fails_open_when_the_agent_list_cannot_be_read(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliberate, and matching Bash. Failing closed would block a legitimate ship every
    time herdr hiccups, and this guard is a backstop for a rule the router's prompt already
    states -- not the only thing between the user and a bad merge."""
    monkeypatch.setenv("HERDR_PANE_ID", "w3:p1")
    fake.on_error("agent.list", "internal", "boom")
    assert await shipping.running_in_agent_pane(client) is False


async def test_no_pane_id_means_a_terminal_outside_herdr(
    client: HerdrClient, fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERDR_PANE_ID", raising=False)
    assert await shipping.running_in_agent_pane(client) is False


def _agent_row(pane_id: str) -> dict[str, Any]:
    return {
        "pane_id": pane_id,
        "terminal_id": "t1",
        "workspace_id": pane_id.split(":")[0],
        "tab_id": f"{pane_id.split(':')[0]}:t1",
        "focused": False,
        "agent_status": "idle",
        "revision": 1,
        "state_change_seq": 3,
    }


# -- go: the happy path ------------------------------------------------------


async def _go_ready(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, FakeGh, building.BuildPaths]:
    """A real built worktree, a fake herdr, and a fake gh that merges cleanly."""
    root = tmp_path / "wq"
    config, _scenario, paths = await _built(client, fake, root, repo)
    fake.on("worktree.remove", {"type": "ok"})
    fake.on("workspace.close", {"type": "ok"})
    fake_gh = working_gh(tmp_path / "bin", monkeypatch, pr=PR)
    return config, fake_gh, paths


async def test_go_pushes_opens_waits_merges_and_cleans_up(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, fake_gh, _paths = await _go_ready(client, fake, tmp_path, repo, monkeypatch)

    result = await shipping.go(client, config, "x")

    assert result.merged is True
    assert result.pr == PR
    # In order: open, look up the number, wait for checks, watch them, merge. Nothing
    # merges before CI is watched.
    order = [c[:2] for c in fake_gh.calls()]
    assert order[0] == ["pr", "create"]
    assert order[1] == ["pr", "view"]
    assert order[-1] == ["pr", "merge"]
    assert ["pr", "checks"] in order[2:-1]


async def test_merge_never_passes_delete_branch(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavior #7, asserted on the argv wq actually builds.

    In a worktree checkout `--delete-branch` makes gh switch the checkout to the default
    branch first -- which is already checked out in the parent repo, so git refuses and gh
    exits non-zero *after the merge has landed*, taking the rest of the command with it.
    """
    config, fake_gh, _paths = await _go_ready(client, fake, tmp_path, repo, monkeypatch)
    await shipping.go(client, config, "x")

    merge = fake_gh.calls_matching("pr", "merge")[0]
    assert "--delete-branch" not in merge
    assert "--squash" in merge


async def test_the_pr_base_comes_from_the_recorded_base(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    master_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bash hard-coded `--base main`. A master repo would have opened its PR against a
    branch that does not exist."""
    config, fake_gh, _paths = await _go_ready(client, fake, tmp_path, master_repo, monkeypatch)
    await shipping.go(client, config, "x")

    create = fake_gh.calls_matching("pr", "create")[0]
    assert create[create.index("--base") + 1] == "master"


async def test_the_branch_is_deleted_locally_and_remotely_after_the_merge(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What `--delete-branch` would have done, done safely once the worktree is gone."""
    config, _fake_gh, _paths = await _go_ready(client, fake, tmp_path, repo, monkeypatch)
    await shipping.go(client, config, "x")

    branches = subprocess.run(
        ["git", "branch", "--list"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert "wq/x" not in branches
    assert not Path(f"{repo}-worktrees/x").exists()


# -- go: CI ------------------------------------------------------------------


async def test_no_checks_reported_is_waited_out_not_treated_as_failure(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavior #13. GitHub registers check runs a few seconds after the PR opens, and
    until it does `gh pr checks --watch` exits *straight away* with "no checks reported" --
    which reads exactly like a CI failure and means the opposite."""
    root = tmp_path / "wq"
    config, _scenario, _paths = await _built(client, fake, root, repo)
    fake.on("worktree.remove", {"type": "ok"})
    fake_gh = (
        FakeGh(tmp_path / "bin")
        .on(["pr", "create"], out="url\n")
        .on(["pr", "view"], out=f"{PR}\n")
        .on(["pr", "checks", str(PR), "--watch", "--fail-fast"], out="ok\n")
        # The first two plain `pr checks` calls report nothing yet; the third has them.
        .on(["pr", "checks"], out="build\tpass\n", after=2)
        .on(["pr", "checks"], out="no checks reported on the 'wq/x' branch\n", code=1)
        .on(["pr", "merge"], out="merged\n")
        .install(monkeypatch)
    )

    result = await shipping.go(client, config, "x")
    assert result.merged is True
    # It polled rather than giving up on the first "no checks reported".
    assert len(fake_gh.calls_matching("pr", "checks")) >= 3


async def test_checks_that_never_appear_time_out_with_a_manual_remedy(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repository with no CI at all. The remedy has to be actionable, because the PR is
    already open and nothing else is going to merge it."""
    from dataclasses import replace

    root = tmp_path / "wq"
    config, _scenario, _paths = await _built(client, fake, root, repo)
    config = replace(config, loops=replace(config.loops, ci_appear_timeout=0))
    fake_gh = (
        FakeGh(tmp_path / "bin")
        .on(["pr", "create"], out="url\n")
        .on(["pr", "view"], out=f"{PR}\n")
        .on(["pr", "checks"], out="no checks reported\n", code=1)
        .install(monkeypatch)
    )

    with pytest.raises(WorkflowError) as caught:
        await shipping.go(client, config, "x")
    assert "no checks appeared" in caught.value.message
    assert "--delete-branch" in (caught.value.fix or "")  # by hand, where it is safe
    assert fake_gh.calls_matching("pr", "merge") == []


async def test_failing_ci_stops_before_the_merge(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "wq"
    config, _scenario, _paths = await _built(client, fake, root, repo)
    fake_gh = (
        FakeGh(tmp_path / "bin")
        .on(["pr", "create"], out="url\n")
        .on(["pr", "view"], out=f"{PR}\n")
        .on(["pr", "checks", str(PR), "--watch", "--fail-fast"], out="build FAIL\n", code=1)
        .on(["pr", "checks"], out="build\tpending\n")
        .install(monkeypatch)
    )

    with pytest.raises(WorkflowError) as caught:
        await shipping.go(client, config, "x")
    assert f"CI failed on PR #{PR}" in caught.value.message
    assert fake_gh.calls_matching("pr", "merge") == []
    assert fake.calls("notification.show")  # the user is told


async def test_an_existing_pull_request_is_not_an_error(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running `go` after a CI failure is the documented remedy, and by then the PR is
    already open."""
    root = tmp_path / "wq"
    config, _scenario, _paths = await _built(client, fake, root, repo)
    fake.on("worktree.remove", {"type": "ok"})
    (
        FakeGh(tmp_path / "bin")
        .on(["pr", "create"], out="a pull request for branch wq/x already exists\n", code=1)
        .on(["pr", "view"], out=f"{PR}\n")
        .on(["pr", "checks", str(PR), "--watch", "--fail-fast"], out="ok\n")
        .on(["pr", "checks"], out="build\tpass\n")
        .on(["pr", "merge"], out="merged\n")
        .install(monkeypatch)
    )

    result = await shipping.go(client, config, "x")
    assert result.merged is True


# -- go: nothing after the merge may abort -----------------------------------
# Rule of thumb #4. Each of these fails one cleanup step and asserts the command still
# reports the merge that has already landed.


@pytest.mark.parametrize(
    "break_step",
    [
        "fetch_prune",
        "can_refresh",
        "rebase",
        "worktree_remove",
        "worktree_prune",
        "delete_branch",
        "delete_remote_branch",
    ],
)
async def test_a_failing_git_cleanup_step_does_not_undo_the_merge(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    break_step: str,
) -> None:
    """The merge is not coming back, so no cleanup failure may reach the caller.

    Each helper is written not to raise, but this asserts the property rather than the
    implementation: a step added later that *does* raise must not turn a successful merge
    into a traceback.
    """
    config, _fake_gh, _paths = await _go_ready(client, fake, tmp_path, repo, monkeypatch)

    def explode(*_args: object, **_kwargs: object) -> bool:
        raise OSError(f"{break_step} is broken")

    monkeypatch.setattr(git, break_step, explode)

    result = await shipping.go(client, config, "x")
    assert result.merged is True
    assert result.pr == PR


async def test_a_failing_worktree_remove_over_the_socket_does_not_undo_the_merge(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _fake_gh, _paths = await _go_ready(client, fake, tmp_path, repo, monkeypatch)
    fake.on_error("worktree.remove", "internal", "boom")

    result = await shipping.go(client, config, "x")
    assert result.merged is True


async def test_a_failing_parent_workspace_close_does_not_undo_the_merge(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _fake_gh, _paths = await _go_ready(client, fake, tmp_path, repo, monkeypatch)
    fake.on_error("workspace.close", "internal", "boom")

    result = await shipping.go(client, config, "x")
    assert result.merged is True


async def test_a_dirty_repo_skips_the_refresh_rather_than_failing_it(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`repo` is the user's primary checkout. Rebasing a branch they happen to be sitting
    on is worse than leaving it a commit behind."""
    config, _fake_gh, _paths = await _go_ready(client, fake, tmp_path, repo, monkeypatch)
    (repo / "uncommitted.txt").write_text("work in progress\n")

    rebased: list[str] = []

    def record(_repo: Path, onto: str) -> bool:
        rebased.append(onto)
        return True

    monkeypatch.setattr(git, "rebase", record)

    result = await shipping.go(client, config, "x")
    assert result.merged is True
    assert rebased == []


async def test_a_repo_on_another_branch_skips_the_refresh(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _fake_gh, _paths = await _go_ready(client, fake, tmp_path, repo, monkeypatch)
    git_run(repo, "checkout", "-q", "-b", "someone-elses-work")

    rebased: list[str] = []

    def record(_repo: Path, onto: str) -> bool:
        rebased.append(onto)
        return True

    monkeypatch.setattr(git, "rebase", record)

    result = await shipping.go(client, config, "x")
    assert result.merged is True
    assert rebased == []


async def test_a_clean_repo_on_the_base_branch_is_refreshed(
    client: HerdrClient,
    fake: FakeHerdr,
    tmp_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _fake_gh, _paths = await _go_ready(client, fake, tmp_path, repo, monkeypatch)

    rebased: list[str] = []

    def record(_repo: Path, onto: str) -> bool:
        rebased.append(onto)
        return True

    monkeypatch.setattr(git, "rebase", record)

    await shipping.go(client, config, "x")
    assert rebased == ["origin/main"]


# -- go: the gate ------------------------------------------------------------


async def test_go_refuses_a_slug_with_no_build(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    with pytest.raises(WorkflowError) as caught:
        await shipping.go(client, build_config(tmp_path / "wq"), "never")
    assert "no build for never" in caught.value.message


def test_the_build_panes_are_not_where_go_runs(tmp_path: Path) -> None:
    """A documentation test, and the reason `ship` exists: `go`'s cleanup closes the
    workspace labelled with the slug, which is the one the build panes live in."""
    assert f"{WS}:p1" not in shipping.ship_command("x", tmp_path)
