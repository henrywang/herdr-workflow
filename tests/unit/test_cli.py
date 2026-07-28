"""CLI tests -- commands invoked exactly as users invoke them.

These cover what the workflow-level tests cannot: that `--json` emits valid JSON, that
exit codes match the Bash contract the router depends on, and that global options are not
leaking between invocations.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from herdr_workflow import cli
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
    """The Bash dispatch accepted `ls`; the router and muscle memory both use it."""
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


def test_bad_config_reports_cleanly(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[agents]\nplan = 'claude:opus'\n")
    result = runner.invoke(cli.app, ["--config", str(bad), "list"])
    assert result.exit_code == 1
    assert "kind:model:level" in result.output
