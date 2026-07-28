"""`wq build` -- the worktree, behavior #6, and the code <-> review loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from herdr_workflow import config as config_module
from herdr_workflow.errors import GitError, WorkflowError
from herdr_workflow.herdr import delivery
from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.workflows import build_env, building, listing, prompts
from tests.build_scenario import (
    WS,
    Scenario,
    build_config,
    with_plan,
    workspace,
    worktree_info,
)
from tests.fake_herdr import FakeHerdr


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "POLL_INTERVAL", 0.001)
    monkeypatch.setattr(delivery, "SETTLE_CONFIRM_DELAY", 0.001)


# -- the loop ----------------------------------------------------------------


async def test_approval_in_round_one_stops_immediately(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    paths = with_plan(root)
    scenario = Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    result = await building.build(client, build_config(root), "x", repo)
    assert result.approved is True
    assert result.rounds == 1
    assert scenario.code_turns == 1  # implement, and no fix turn
    assert paths.diff.stat().st_size > 0


async def test_a_changes_verdict_triggers_a_fix_round(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    paths = with_plan(root)
    scenario = Scenario(
        fake, paths, repo, reviews=[prompts.VERDICT_CHANGES, prompts.VERDICT_APPROVED]
    )

    result = await building.build(client, build_config(root), "x", repo)
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
    paths = with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_CHANGES, prompts.VERDICT_APPROVED])

    await building.build(client, build_config(root), "x", repo)
    text = paths.diff.read_text()
    assert "change1.py" in text
    assert "change2.py" in text


async def test_the_round_cap_stops_a_loop_that_never_converges(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    paths = with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_CHANGES] * 10)

    result = await building.build(client, build_config(root, rounds=2), "x", repo)
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
    paths = with_plan(root)
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
        await building.build(client, build_config(root), "x", repo)
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
        await building.build(client, build_config(root), "x", repo)


async def test_an_unapproved_plan_can_still_be_built(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """Deliberate, and inherited from Bash: a plan that hit its own round cap is still a
    plan, and building it anyway is the user's call rather than wq's."""
    root = tmp_path / "wq"
    paths = with_plan(root)
    (paths.dir / "review.md").write_text(f"blocking findings\n\n{prompts.VERDICT_CHANGES}\n")
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    result = await building.build(client, build_config(root), "x", repo)
    assert result.approved is True


async def test_a_non_repo_is_refused_before_anything_is_created(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path
) -> None:
    root = tmp_path / "wq"
    with_plan(root)
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError) as caught:
        await building.build(client, build_config(root), "x", plain)
    assert "not a git repository" in caught.value.message
    assert fake.calls("worktree.create") == []


async def test_an_agent_that_commits_nothing_fails_the_build(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """The reviewer reads the diff, so uncommitted work is invisible to it -- and would
    otherwise be reviewed as if nothing had been attempted."""
    root = tmp_path / "wq"
    paths = with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED], commits=False)

    with pytest.raises(WorkflowError) as caught:
        await building.build(client, build_config(root), "x", repo)
    assert "no changes committed on wq/x" in caught.value.message
    assert fake.calls("agent.prompt")  # it did try


# -- behavior #6: the workspace herdr does not report ------------------------


async def test_the_parent_workspace_is_found_and_recorded(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """`worktree.create` opens a second workspace for the parent checkout and reports only
    the first. Nothing else knows it exists, so every build would leak one."""
    root = tmp_path / "wq"
    paths = with_plan(root)
    Scenario(
        fake,
        paths,
        repo,
        reviews=[prompts.VERDICT_APPROVED],
        extra_after_create=[workspace("w10", repo.name, worktree_info(repo, repo, linked=False))],
    )

    result = await building.build(client, build_config(root), "x", repo)
    assert result.parent_workspace == "w10"
    assert build_env.read(paths.env).parent_workspace == "w10"


async def test_a_repo_root_behind_a_symlink_still_matches(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """On macOS /tmp is a symlink to /private/tmp, so herdr can report a path that is the
    same directory spelled differently. An unresolved comparison matches nothing --
    silently, and only in exactly the /tmp repositories used for live validation."""
    root = tmp_path / "wq"
    paths = with_plan(root)
    link = tmp_path / "repo-link"
    link.symlink_to(repo)
    Scenario(
        fake,
        paths,
        repo,
        reviews=[prompts.VERDICT_APPROVED],
        extra_after_create=[workspace("w10", repo.name, worktree_info(link, link, linked=False))],
    )

    result = await building.build(client, build_config(root), "x", repo)
    assert result.parent_workspace == "w10"


async def test_a_workspace_the_user_already_had_open_is_not_claimed(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """When the repository already had a workspace, herdr opens no second one -- and that
    workspace is the user's, not wq's to record or later close."""
    root = tmp_path / "wq"
    paths = with_plan(root)
    Scenario(
        fake,
        paths,
        repo,
        reviews=[prompts.VERDICT_APPROVED],
        pre_existing=[workspace("w3", repo.name, worktree_info(repo, repo, linked=False))],
    )

    result = await building.build(client, build_config(root), "x", repo)
    assert result.parent_workspace is None
    assert build_env.read(paths.env).parent_workspace is None


async def test_a_concurrent_workspace_for_another_repo_is_not_claimed(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """Another wq command opening its own workspace lands in the same diff. Picking by
    timing alone would record -- and later close -- someone else's workspace."""
    root = tmp_path / "wq"
    paths = with_plan(root)
    other = tmp_path / "other-repo"
    Scenario(
        fake,
        paths,
        repo,
        reviews=[prompts.VERDICT_APPROVED],
        extra_after_create=[workspace("w11", "other", worktree_info(other, other, linked=False))],
    )

    result = await building.build(client, build_config(root), "x", repo)
    assert result.parent_workspace is None


async def test_a_linked_worktree_is_never_the_parent(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """Another slug's build worktree has the same repo_root. `is_linked_worktree` is what
    separates the parent checkout from every worktree cut out of it."""
    root = tmp_path / "wq"
    paths = with_plan(root)
    sibling = tmp_path / "repo-worktrees" / "other-slug"
    Scenario(
        fake,
        paths,
        repo,
        reviews=[prompts.VERDICT_APPROVED],
        extra_after_create=[
            workspace("w12", "other-slug", worktree_info(repo, sibling, linked=True))
        ],
    )

    result = await building.build(client, build_config(root), "x", repo)
    assert result.parent_workspace is None


# -- outputs -----------------------------------------------------------------


async def test_the_build_env_records_everything_the_later_commands_need(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    paths = with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    await building.build(client, build_config(root), "x", repo)
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
    paths = with_plan(root)
    plan_review = paths.dir / "review.md"
    plan_review.write_text("the plan review\n")
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    await building.build(client, build_config(root), "x", repo)
    assert plan_review.read_text() == "the plan review\n"
    assert paths.review.name == "code-review.md"
    assert paths.review.is_file()


async def test_the_reviewer_is_given_paths_not_content(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    paths = with_plan(root)
    paths.plan.write_text("# Plan\n\nSECRET PLAN BODY\n")
    scenario = Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    await building.build(client, build_config(root), "x", repo)
    assert all("SECRET PLAN BODY" not in text for _pane_id, text in scenario.prompts)
    assert any(str(paths.diff) in text for _pane_id, text in scenario.prompts)


async def test_the_two_agents_are_different_models_in_their_own_panes(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    root = tmp_path / "wq"
    paths = with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    await building.build(client, build_config(root), "x", repo)
    starts = fake.calls("agent.start")
    assert len(starts) == 2
    kinds = {c["params"]["pane_id"]: c["params"]["args"] for c in starts}
    assert kinds[f"{WS}:p1"] != kinds[f"{WS}:p2"]
    assert {c["params"]["label"] for c in fake.calls("pane.rename")} == {"code", "review"}


async def test_a_finished_build_is_what_wq_list_looks_for(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """`build` writes the files, `list` reads them, and nothing else connects the two.

    `list` decides a bare-slug workspace is wq's by finding a `build.env` beside it, and
    picks the `*` most-recently-worked marker from `diff.patch` mtimes -- both router
    contracts, and both fed entirely by whatever `build` happens to leave on disk. The
    listing tests use hand-written files, so this is the one place the real producer meets
    the real consumer.
    """
    root = tmp_path / "wq"
    paths = with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    await building.build(client, build_config(root), "x", repo)

    result = await listing.collect(client, root)
    row = next(r for r in result.rows if r.label == "x")
    assert row.is_build is True
    assert result.current == "x"
    assert "  * " in listing.render(result)


async def test_the_worktree_is_branched_from_the_resolved_base(
    client: HerdrClient, fake: FakeHerdr, tmp_path: Path, repo: Path
) -> None:
    """Bash hard-coded `origin/main`. The base is resolved, then used for both the branch
    point and the diff range -- one answer, so they cannot disagree."""
    root = tmp_path / "wq"
    paths = with_plan(root)
    Scenario(fake, paths, repo, reviews=[prompts.VERDICT_APPROVED])

    result = await building.build(client, build_config(root), "x", repo)
    params = fake.calls("worktree.create")[0]["params"]
    assert params["base"] == result.base
    assert params["branch"] == "wq/x"
    assert params["label"] == "x"
    assert params["focus"] is False
