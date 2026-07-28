"""`wq doctor` -- verify the environment, and say how to fix what is broken.

This is the one command that does not exist in the Bash implementation, and it earns its
place the first time herdr bumps its protocol. A shared tool against a young API breaks
for strangers in ways it never breaks for its author; without this the symptom is a
decode error deep inside a workflow, which reads like a wq bug.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from herdr_workflow.config import Config
from herdr_workflow.errors import HerdrError
from herdr_workflow.herdr import socket_path
from herdr_workflow.herdr.client import HerdrClient, connect
from herdr_workflow.protocol.messages import PINNED_PROTOCOL


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    fix: str | None = None


def _binary(name: str, *, required: bool, fix: str) -> Check:
    path = shutil.which(name)
    if path:
        return Check(name, Status.OK, path)
    return Check(name, Status.FAIL if required else Status.WARN, "not found", fix)


def _git_repo(cwd: Path) -> Check:
    if shutil.which("git") is None:
        return Check("repository", Status.WARN, "skipped (git not installed)")
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("repository", Status.WARN, f"could not run git: {exc}")
    if out.returncode != 0:
        # Not an error: `wq list`, `wq chat`, and `wq ask` are all useful outside a repo.
        return Check(
            "repository",
            Status.WARN,
            f"{cwd} is not a git repository",
            "cd into a repository before: wq build",
        )
    return Check("repository", Status.OK, out.stdout.strip())


def _config_check(config: Config) -> list[Check]:
    checks = [
        Check(
            "agents",
            Status.OK,
            ", ".join(
                f"{role}={getattr(config.agents, role)}"
                for role in ("plan", "code", "review", "idea", "ask", "router")
            ),
        )
    ]

    # The rule that makes a review loop worth running: the reviewer must not be the model
    # that wrote. If they are the same, the loop still runs and still reports approvals --
    # it just stops being adversarial, which is the failure you would never notice.
    if config.agents.review.model == config.agents.code.model:
        checks.append(
            Check(
                "review independence",
                Status.WARN,
                f"code and review are both {config.agents.code.model}",
                "set a different model for [agents] review -- a reviewer that is the "
                "writer approves its own work",
            )
        )
    if config.agents.review.model == config.agents.plan.model:
        checks.append(
            Check(
                "plan independence",
                Status.WARN,
                f"plan and review are both {config.agents.plan.model}",
                "set a different model for [agents] review",
            )
        )

    root = config.root
    if root.is_dir():
        checks.append(Check("scratch root", Status.OK, str(root)))
    else:
        checks.append(Check("scratch root", Status.OK, f"{root} (will be created)"))

    if config.paths.notes is None:
        checks.append(
            Check(
                "notes sink",
                Status.WARN,
                "not configured",
                "set [paths] notes in your config to use: wq brainstorm",
            )
        )
    else:
        notes = config.paths.notes.expanduser()
        checks.append(
            Check(
                "notes sink",
                Status.OK if notes.is_dir() else Status.WARN,
                str(notes),
                None if notes.is_dir() else "the configured notes directory does not exist",
            )
        )
    return checks


async def _server_checks(client: HerdrClient) -> list[Check]:
    pong = await client.ping()
    checks = [Check("herdr server", Status.OK, f"version {pong.version}")]

    if pong.protocol == PINNED_PROTOCOL:
        checks.append(Check("protocol", Status.OK, f"{pong.protocol} (pinned {PINNED_PROTOCOL})"))
    else:
        # Deliberately not fatal. A newer server is usually compatible, and refusing to
        # run would be worse than a warning that names the risk. The decode errors this
        # predicts are what make it worth printing at all.
        checks.append(
            Check(
                "protocol",
                Status.WARN,
                f"server speaks {pong.protocol}, wq is pinned to {PINNED_PROTOCOL}",
                "upgrade wq if commands start failing to decode responses: "
                "uv tool upgrade herdr-workflow",
            )
        )

    snapshot = await client.snapshot()
    checks.append(
        Check(
            "session",
            Status.OK,
            f"{len(snapshot.workspaces)} workspace(s), {len(snapshot.agents)} agent(s)",
        )
    )
    return checks


async def run(config: Config, cwd: Path) -> list[Check]:
    checks = [
        _binary("herdr", required=True, fix="install herdr: https://herdr.dev"),
        _binary("git", required=True, fix="install git"),
        _binary("gh", required=False, fix="install the GitHub CLI to use: wq ship / wq go"),
    ]

    path = socket_path.resolve(config.herdr.session, config.herdr.socket)
    if not path.exists():
        checks.append(
            Check("herdr socket", Status.FAIL, f"{path} does not exist", "start it with: herdr")
        )
    else:
        checks.append(Check("herdr socket", Status.OK, str(path)))
        try:
            async with connect(path) as client:
                checks.extend(await _server_checks(client))
        except HerdrError as exc:
            checks.append(
                Check("herdr server", Status.FAIL, exc.message, exc.fix or "start it with: herdr")
            )

    checks.append(_git_repo(cwd))
    checks.extend(_config_check(config))
    return checks


_SYMBOL = {Status.OK: "ok  ", Status.WARN: "warn", Status.FAIL: "FAIL"}


def render(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines: list[str] = []
    for check in checks:
        lines.append(f"[{_SYMBOL[check.status]}] {check.name.ljust(width)}  {check.detail}")
        if check.fix and check.status is not Status.OK:
            lines.append(f"{' ' * (width + 9)}fix: {check.fix}")
    return "\n".join(lines)


def worst(checks: list[Check]) -> Status:
    if any(c.status is Status.FAIL for c in checks):
        return Status.FAIL
    if any(c.status is Status.WARN for c in checks):
        return Status.WARN
    return Status.OK
