"""CLI tests -- commands invoked exactly as users invoke them.

These cover what the workflow-level tests cannot: that `--json` emits valid JSON, that
exit codes match the router contract, and that global options do not leak between
invocations.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from herdr_workflow import cli
from herdr_workflow.workflows import building, prompts
from tests.build_scenario import Scenario
from tests.fake_herdr import FakeHerdr

runner = CliRunner()


@pytest.fixture(autouse=True)
def reset_cli_context() -> Iterator[None]:
    """Global CLI options are module state, so one invocation's --json would otherwise
    leak into the next test."""
    cli.reset_context()
    yield
    cli.reset_context()


@pytest.fixture
def wq_env(
    threaded_fake: FakeHerdr,
    snapshot_result: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Point wq at the fake daemon and an empty scratch root."""
    threaded_fake.on("session.snapshot", snapshot_result)
    root = tmp_path / "wq"
    root.mkdir()
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(threaded_fake.socket_path))
    monkeypatch.setenv("WQ_ROOT", str(root))
    return root


def test_list_renders_plain_text(wq_env: Path) -> None:
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 0
    assert result.stdout.startswith("live:")
    assert "plan-alpha" in result.stdout


def test_list_json_is_valid_json(wq_env: Path) -> None:
    result = runner.invoke(cli.app, ["--json", "list"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert {row["label"] for row in payload["live"]} == {"plan-alpha", "bs-gamma"}
    assert payload["current"] is None


def test_ls_is_an_alias_for_list(wq_env: Path) -> None:
    """The router and existing scripts use the short alias."""
    assert runner.invoke(cli.app, ["ls"]).stdout == runner.invoke(cli.app, ["list"]).stdout


def test_json_does_not_leak_into_the_next_invocation(wq_env: Path) -> None:
    runner.invoke(cli.app, ["--json", "list"])
    plain = runner.invoke(cli.app, ["list"])
    assert plain.stdout.startswith("live:")


def test_missing_daemon_is_a_clean_message_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "absent.sock"))
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "start it with: herdr" in result.output


def test_debug_surfaces_the_traceback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "absent.sock"))
    result = runner.invoke(cli.app, ["--debug", "list"])
    assert result.exit_code != 0
    assert result.exception is not None


def test_doctor_exits_nonzero_when_a_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(tmp_path / "absent.sock"))
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL" in result.stdout


def test_doctor_passes_against_a_live_daemon(wq_env: Path) -> None:
    result = runner.invoke(cli.app, ["--json", "doctor"])
    payload = json.loads(result.stdout)
    names = {c["name"]: c for c in payload["checks"]}
    assert names["herdr socket"]["status"] == "ok"
    assert names["protocol"]["status"] == "ok"


def test_doctor_warns_on_protocol_mismatch(
    threaded_fake: FakeHerdr, snapshot_result: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check that earns doctor its place: a server ahead of our pin."""
    threaded_fake.on("ping", {"type": "pong", "version": "9.9.9", "protocol": 99})
    threaded_fake.on("session.snapshot", snapshot_result)
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(threaded_fake.socket_path))

    result = runner.invoke(cli.app, ["--json", "doctor"])
    payload = json.loads(result.stdout)
    protocol = next(c for c in payload["checks"] if c["name"] == "protocol")
    assert protocol["status"] == "warn"
    assert "99" in protocol["detail"]
    # A newer server is usually fine, so this must not be fatal.
    assert result.exit_code == 0


def test_doctor_reports_invalid_config_with_the_rest_of_its_checks(
    wq_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WQ_CODE_ROUNDS", "0")

    result = runner.invoke(cli.app, ["--json", "doctor"])

    payload = json.loads(result.stdout)
    checks = {check["name"]: check for check in payload["checks"]}
    assert result.exit_code == 1
    assert checks["configuration"]["status"] == "fail"
    assert "positive integer" in checks["configuration"]["detail"]
    assert checks["protocol"]["status"] == "ok"


def test_doctor_reports_invalid_config_file_as_a_check(wq_env: Path, tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text('[herdr]\ninbox_label = ""\n')

    result = runner.invoke(cli.app, ["--config", str(path), "--json", "doctor"])

    payload = json.loads(result.stdout)
    configuration = next(check for check in payload["checks"] if check["name"] == "configuration")
    assert result.exit_code == 1
    assert configuration["status"] == "fail"
    assert "inbox_label" in configuration["detail"]


def test_doctor_flags_a_reviewer_that_wrote_the_code(
    wq_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer that is the writer approves its own work -- silently."""
    monkeypatch.setenv("WQ_AGENT_REVIEW", "claude:sonnet:high")
    monkeypatch.setenv("WQ_AGENT_CODE", "claude:sonnet:high")
    result = runner.invoke(cli.app, ["--json", "doctor"])
    payload = json.loads(result.stdout)
    names = {c["name"] for c in payload["checks"] if c["status"] == "warn"}
    assert "review independence" in names


# -- build's exit codes ------------------------------------------------------
# A router contract, not an implementation detail: the router prompt stops on any non-zero
# exit, and `2` has to stay distinguishable from the `1` of a real failure.


def _build_scenario(
    fake: FakeHerdr, root: Path, repo: Path, reviews: list[str]
) -> building.BuildPaths:
    paths = building.BuildPaths.for_slug(root, "x")
    paths.dir.mkdir(parents=True, exist_ok=True)
    paths.plan.write_text("# Plan\n\nAdd a function.\n")
    Scenario(fake, paths, repo, reviews=reviews)
    return paths


def test_build_exits_two_when_the_round_limit_is_reached(
    wq_env: Path, threaded_fake: FakeHerdr, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreviewed code is sitting on a branch. That is a different outcome from `wq plan`
    stopping at its cap, which exits 0, and from wq breaking, which exits 1."""
    monkeypatch.setenv("WQ_CODE_ROUNDS", "1")
    paths = _build_scenario(threaded_fake, wq_env, repo, [prompts.VERDICT_CHANGES] * 5)

    result = runner.invoke(cli.app, ["build", "x", str(repo)])
    assert result.exit_code == 2
    assert "round limit (1) reached" in result.output
    # No `next: wq ship` -- pointing at ship for unreviewed code is the wrong advice.
    assert "wq ship" not in result.output
    assert paths.review.is_file()


def test_build_exits_zero_and_points_at_ship_when_approved(
    wq_env: Path, threaded_fake: FakeHerdr, repo: Path
) -> None:
    _build_scenario(threaded_fake, wq_env, repo, [prompts.VERDICT_APPROVED])

    result = runner.invoke(cli.app, ["build", "x", str(repo)])
    assert result.exit_code == 0
    assert "wq ship x" in result.output


def test_build_json_reports_the_outcome_with_the_exit_code(
    wq_env: Path, threaded_fake: FakeHerdr, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The router reads this. It still gets the payload on a round-limit stop, so it can
    say *what* stopped rather than only that something did."""
    monkeypatch.setenv("WQ_CODE_ROUNDS", "1")
    _build_scenario(threaded_fake, wq_env, repo, [prompts.VERDICT_CHANGES] * 5)

    result = runner.invoke(cli.app, ["--json", "build", "x", str(repo)])
    assert result.exit_code == 2
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    assert payload["approved"] is False
    assert payload["rounds"] == 1
    assert payload["branch"] == "wq/x"
    assert payload["base"] == "origin/main"


def test_build_without_a_plan_exits_one(wq_env: Path, threaded_fake: FakeHerdr, repo: Path) -> None:
    """A missing plan is wq refusing to start, not a loop that ran and did not converge."""
    result = runner.invoke(cli.app, ["build", "nothing-here", str(repo)])
    assert result.exit_code == 1
    assert "no plan at" in result.output


def test_revise_exits_zero_even_when_the_reviewer_has_findings(
    wq_env: Path, threaded_fake: FakeHerdr, repo: Path
) -> None:
    """The contract `build` does not share. Findings from a revise are the thing you asked
    for -- reading them is the next step, not a failure. A refactor for "consistency" with
    `build`'s exit 2 would break the router's handling of a perfectly normal outcome."""
    paths = building.BuildPaths.for_slug(wq_env, "x")
    paths.dir.mkdir(parents=True, exist_ok=True)
    paths.plan.write_text("# Plan\n\nAdd a function.\n")
    Scenario(
        threaded_fake,
        paths,
        repo,
        reviews=[prompts.VERDICT_APPROVED, prompts.VERDICT_CHANGES],
    )
    assert runner.invoke(cli.app, ["build", "x", str(repo)]).exit_code == 0

    result = runner.invoke(cli.app, ["revise", "x", "rename", "the", "function"])
    assert result.exit_code == 0
    assert "reviewer has findings" in result.output
    assert "revise.patch" in result.output


def test_revise_without_a_build_exits_one(wq_env: Path, threaded_fake: FakeHerdr) -> None:
    result = runner.invoke(cli.app, ["revise", "nothing-here", "change something"])
    assert result.exit_code == 1
    assert "no build for nothing-here" in result.output


def test_ask_defaults_to_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []

    async def ask(client: object, config: object, question: str, cwd: Path) -> cli.tabs.TabResult:
        seen.append(cwd)
        return cli.tabs.TabResult("w1", "ask-1", "p1", True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WQ_ASK_CWD", raising=False)
    monkeypatch.setattr(cli.tabs, "ask", ask)

    result = runner.invoke(cli.app, ["ask", "question"])

    assert result.exit_code == 0
    assert seen == [tmp_path]


def test_path_traversal_slug_is_rejected_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WQ_ROOT", str(tmp_path / "root"))
    result = runner.invoke(cli.app, ["clean", "../outside"])
    assert result.exit_code == 1
    assert "invalid slug" in result.output


def test_bad_config_reports_cleanly(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[agents]\nplan = 'claude:opus'\n")
    result = runner.invoke(cli.app, ["--config", str(bad), "list"])
    assert result.exit_code == 1
    assert "kind:model:level" in result.output
