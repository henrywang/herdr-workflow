"""Configuration: built-in defaults, then user config, then project config, then env.

The `WQ_*` environment variables are not a legacy shim -- they are how the Bash
implementation was configured, they are documented in devcage-macos/WORKFLOW.md, and the
router prompt uses them inline (`WQ_AGENT_CODE=claude:opus:high wq build <slug>`). They
stay as the final override layer, above files, so a one-off on the command line still
wins.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from herdr_workflow.errors import ConfigError

USER_CONFIG = Path("~/.config/wq/config.toml").expanduser()
PROJECT_CONFIG = Path(".wq.toml")


@dataclass(frozen=True)
class AgentSpec:
    """A role, as `kind:model:level`.

    Either side of a review loop can be either agent. The rule that matters is that the
    reviewer is not the model that wrote -- which way round you assign them is a free
    choice, not a constraint.
    """

    kind: str
    model: str
    level: str

    @classmethod
    def parse(cls, spec: str, *, role: str = "agent") -> AgentSpec:
        parts = spec.split(":")
        # Model names contain slashes and may themselves contain colons in future
        # ("openai-codex/gpt-5.6-sol"), so split off kind and level from the ends and
        # leave whatever remains as the model.
        if len(parts) < 3:
            raise ConfigError(
                f"bad agent spec for {role}: {spec!r}",
                why="an agent spec must be kind:model:level",
                fix="e.g. claude:opus:high  or  pi:openai-codex/gpt-5.6-sol:high",
            )
        kind, level = parts[0], parts[-1]
        model = ":".join(parts[1:-1])
        if kind not in ("claude", "pi"):
            raise ConfigError(
                f"unsupported agent kind {kind!r} in {role} spec {spec!r}",
                why="wq knows how to start claude and pi",
                fix="use claude or pi as the kind",
            )
        return cls(kind=kind, model=model, level=level)

    def __str__(self) -> str:
        return f"{self.kind}:{self.model}:{self.level}"


@dataclass(frozen=True)
class Agents:
    plan: AgentSpec = AgentSpec("claude", "opus", "high")
    code: AgentSpec = AgentSpec("claude", "sonnet", "high")
    review: AgentSpec = AgentSpec("pi", "openai-codex/gpt-5.6-sol", "high")
    idea: AgentSpec = AgentSpec("claude", "opus", "high")
    ask: AgentSpec = AgentSpec("pi", "openai-codex/gpt-5.6-sol", "medium")
    router: AgentSpec = AgentSpec("pi", "openai-codex/gpt-5.6-sol", "medium")


@dataclass(frozen=True)
class Loops:
    plan_rounds: int = 3
    code_rounds: int = 3
    turn_timeout_ms: int = 1_800_000  # 30 min per agent turn
    prompt_attempts: int = 5
    ci_appear_timeout: int = 120  # wait for CI to register on a new PR


@dataclass(frozen=True)
class Paths:
    root: Path = Path("~/Workspace/.wq")
    # Note sink for `wq brainstorm`. No default: the Bash version hard-coded an iCloud
    # Obsidian path, which is exactly the personal assumption a public tool must not make.
    notes: Path | None = None


@dataclass(frozen=True)
class Herdr:
    socket: str = "auto"
    session: str | None = None
    inbox_label: str = "inbox"


@dataclass(frozen=True)
class Claude:
    # Claude Code prompts for permission on first tool use, which stalls an unattended
    # loop immediately. wq's panes work only in a scratch dir or a throwaway worktree, so
    # they run unattended.
    permission_mode: str = "auto"


@dataclass(frozen=True)
class Config:
    agents: Agents = field(default_factory=Agents)
    loops: Loops = field(default_factory=Loops)
    paths: Paths = field(default_factory=Paths)
    herdr: Herdr = field(default_factory=Herdr)
    claude: Claude = field(default_factory=Claude)

    @property
    def root(self) -> Path:
        return self.paths.root.expanduser()


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            loaded: dict[str, Any] = tomllib.load(fh)
            return loaded
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"could not parse {path}", why=str(exc), fix="fix the TOML syntax and retry"
        ) from exc


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge overlay onto base, so a project config can override one key of a table
    without having to restate the rest of it."""
    out = dict(base)
    for key, value in overlay.items():
        existing = out.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            # tomllib hands back dict[str, Any]; narrowing on isinstance loses that.
            out[key] = _merge(cast("dict[str, Any]", existing), cast("dict[str, Any]", value))
        else:
            out[key] = value
    return out


def _apply_agents(agents: Agents, table: dict[str, Any]) -> Agents:
    updates: dict[str, AgentSpec] = {}
    for role in ("plan", "code", "review", "idea", "ask", "router"):
        raw = table.get(role)
        if raw is not None:
            updates[role] = AgentSpec.parse(str(raw), role=role)
    return replace(agents, **updates)  # type: ignore[arg-type]


def _env_overrides(config: Config) -> Config:
    """Apply WQ_* environment variables. These win over every file."""
    env = os.environ

    agent_env = {
        "plan": "WQ_AGENT_PLAN",
        "code": "WQ_AGENT_CODE",
        "review": "WQ_AGENT_REVIEW",
        "idea": "WQ_AGENT_IDEA",
        "ask": "WQ_AGENT_ASK",
        "router": "WQ_AGENT_ROUTER",
    }
    agents = _apply_agents(
        config.agents, {role: env[var] for role, var in agent_env.items() if var in env}
    )

    loop_env = {
        "plan_rounds": "WQ_PLAN_ROUNDS",
        "code_rounds": "WQ_CODE_ROUNDS",
        "turn_timeout_ms": "WQ_TURN_TIMEOUT_MS",
        "prompt_attempts": "WQ_PROMPT_ATTEMPTS",
        "ci_appear_timeout": "WQ_CI_APPEAR_TIMEOUT",
    }
    loop_updates: dict[str, int] = {}
    for attr, var in loop_env.items():
        if var in env:
            try:
                loop_updates[attr] = int(env[var])
            except ValueError as exc:
                raise ConfigError(
                    f"{var} must be an integer, got {env[var]!r}",
                    why="loop bounds and timeouts are numeric",
                ) from exc
    loops = replace(config.loops, **loop_updates)  # type: ignore[arg-type]

    paths = config.paths
    if "WQ_ROOT" in env:
        paths = replace(paths, root=Path(env["WQ_ROOT"]))
    if "WQ_VAULT" in env:
        paths = replace(paths, notes=Path(env["WQ_VAULT"]))

    herdr = config.herdr
    if "WQ_INBOX_LABEL" in env:
        herdr = replace(herdr, inbox_label=env["WQ_INBOX_LABEL"])

    claude = config.claude
    if "WQ_CLAUDE_PERMISSION_MODE" in env:
        claude = replace(claude, permission_mode=env["WQ_CLAUDE_PERMISSION_MODE"])

    return Config(agents=agents, loops=loops, paths=paths, herdr=herdr, claude=claude)


def load(explicit: Path | None = None) -> Config:
    """Built-in defaults -> user config -> project config -> environment."""
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(
                f"config file not found: {explicit}",
                fix="check the path passed to --config",
            )
        data = _load_toml(explicit)
    else:
        data = _merge(_load_toml(USER_CONFIG), _load_toml(PROJECT_CONFIG))

    config = Config()
    if "agents" in data:
        config = replace(config, agents=_apply_agents(config.agents, data["agents"]))
    if "loops" in data:
        known = {f for f in Loops.__dataclass_fields__}
        config = replace(
            config,
            loops=replace(config.loops, **{k: v for k, v in data["loops"].items() if k in known}),
        )
    if "paths" in data:
        table = data["paths"]
        paths = config.paths
        if table.get("root"):
            paths = replace(paths, root=Path(str(table["root"])))
        if table.get("notes"):
            paths = replace(paths, notes=Path(str(table["notes"])))
        config = replace(config, paths=paths)
    if "herdr" in data:
        known = {f for f in Herdr.__dataclass_fields__}
        config = replace(
            config,
            herdr=replace(config.herdr, **{k: v for k, v in data["herdr"].items() if k in known}),
        )
    if "claude" in data:
        known = {f for f in Claude.__dataclass_fields__}
        config = replace(
            config,
            claude=replace(
                config.claude, **{k: v for k, v in data["claude"].items() if k in known}
            ),
        )

    return _env_overrides(config)
