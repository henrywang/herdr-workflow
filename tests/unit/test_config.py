from __future__ import annotations

from pathlib import Path

import pytest

from herdr_workflow import config as config_module
from herdr_workflow.config import AgentSpec
from herdr_workflow.errors import ConfigError


def test_agent_spec_keeps_slashes_in_model_names() -> None:
    spec = AgentSpec.parse("pi:openai-codex/gpt-5.6-sol:high")
    assert (spec.kind, spec.model, spec.level) == ("pi", "openai-codex/gpt-5.6-sol", "high")


def test_agent_spec_round_trips() -> None:
    text = "claude:opus:high"
    assert str(AgentSpec.parse(text)) == text


def test_agent_spec_rejects_two_part_specs() -> None:
    with pytest.raises(ConfigError) as caught:
        AgentSpec.parse("claude:opus", role="plan")
    assert caught.value.fix is not None
    assert "kind:model:level" in (caught.value.why or "")


def test_agent_spec_rejects_unknown_kind() -> None:
    with pytest.raises(ConfigError):
        AgentSpec.parse("cursor:whatever:high")


def test_agent_spec_rejects_empty_fields() -> None:
    with pytest.raises(ConfigError):
        AgentSpec.parse("claude::high")
    with pytest.raises(ConfigError):
        AgentSpec.parse("claude:opus:")


def test_defaults_pair_a_different_model_for_review() -> None:
    """The one rule the loop depends on: the reviewer is not the model that wrote."""
    cfg = config_module.Config()
    assert cfg.agents.review.model != cfg.agents.code.model
    assert cfg.agents.review.model != cfg.agents.plan.model


def test_toml_overrides_defaults(tmp_path: Path) -> None:
    path = tmp_path / "wq.toml"
    path.write_text(
        """
        [agents]
        code = "claude:opus:high"

        [loops]
        code_rounds = 7

        [paths]
        root = "/tmp/scratch"
        """
    )
    cfg = config_module.load(path)
    assert cfg.agents.code.model == "opus"
    assert cfg.loops.code_rounds == 7
    assert cfg.paths.root == Path("/tmp/scratch")
    # Untouched keys keep their defaults.
    assert cfg.loops.plan_rounds == 3


def test_env_beats_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`WQ_AGENT_CODE=claude:opus:high wq build ...` is documented usage in the router
    prompt, so env must win over configuration files."""
    path = tmp_path / "wq.toml"
    path.write_text('[agents]\ncode = "claude:sonnet:high"\n')
    monkeypatch.setenv("WQ_AGENT_CODE", "claude:opus:high")
    assert config_module.load(path).agents.code.model == "opus"


def test_every_bash_env_var_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WQ_ROOT", "/tmp/root")
    monkeypatch.setenv("WQ_VAULT", "/tmp/vault")
    monkeypatch.setenv("WQ_PLAN_ROUNDS", "9")
    monkeypatch.setenv("WQ_CODE_ROUNDS", "8")
    monkeypatch.setenv("WQ_TURN_TIMEOUT_MS", "60000")
    monkeypatch.setenv("WQ_PROMPT_ATTEMPTS", "2")
    monkeypatch.setenv("WQ_CI_APPEAR_TIMEOUT", "30")
    monkeypatch.setenv("WQ_INBOX_LABEL", "mailbox")
    monkeypatch.setenv("WQ_CLAUDE_PERMISSION_MODE", "acceptEdits")
    monkeypatch.setenv("WQ_AGENT_ROUTER", "pi:some-model:low")

    cfg = config_module.load(None)
    assert cfg.paths.root == Path("/tmp/root")
    assert cfg.paths.notes == Path("/tmp/vault")
    assert cfg.loops.plan_rounds == 9
    assert cfg.loops.code_rounds == 8
    assert cfg.loops.turn_timeout_ms == 60000
    assert cfg.loops.prompt_attempts == 2
    assert cfg.loops.ci_appear_timeout == 30
    assert cfg.herdr.inbox_label == "mailbox"
    assert cfg.claude.permission_mode == "acceptEdits"
    assert cfg.agents.router.model == "some-model"


@pytest.mark.parametrize("value", ["0", "-1"])
def test_nonpositive_loop_values_are_rejected(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WQ_CODE_ROUNDS", value)
    with pytest.raises(ConfigError, match="positive integer"):
        config_module.load(None)


def test_config_sections_must_be_tables(tmp_path: Path) -> None:
    path = tmp_path / "wq.toml"
    path.write_text('loops = "oops"\n')
    with pytest.raises(ConfigError, match=r"bad \[loops\] configuration"):
        config_module.load(path)


def test_empty_root_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WQ_ROOT", "")
    with pytest.raises(ConfigError, match="WQ_ROOT cannot be empty"):
        config_module.load(None)


def test_non_numeric_env_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WQ_CODE_ROUNDS", "lots")
    with pytest.raises(ConfigError) as caught:
        config_module.load(None)
    assert "WQ_CODE_ROUNDS" in caught.value.message


def test_missing_explicit_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        config_module.load(tmp_path / "absent.toml")


def test_no_default_notes_path() -> None:
    """A notes location is personal and must be configured explicitly."""
    assert config_module.Config().paths.notes is None
