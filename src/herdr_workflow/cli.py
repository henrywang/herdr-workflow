"""The `wq` command line.

Typer is synchronous and the herdr client is async. The boundary is `_run` and nowhere
else -- one `asyncio.run` at the command edge. Scattering event loops through the
workflow layer is how this stops being testable.

**This surface is a contract, not a convenience.** wq is designed to be driven by an agent
router as much as by a person, and a router calls these commands by name, reads their
output, and branches on their exit codes. Command names, aliases, argument order, output
format and exit codes are all load-bearing -- see CONTRIBUTING.md before changing any of
them.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer

from herdr_workflow import config as config_module
from herdr_workflow.errors import WqError
from herdr_workflow.herdr import socket_path
from herdr_workflow.herdr.client import connect
from herdr_workflow.output import console
from herdr_workflow.workflows import (
    brainstorming,
    building,
    cleanup,
    inbox,
    listing,
    planning,
    revising,
    shipping,
    tabs,
)
from herdr_workflow.workflows import doctor as doctor_workflow

app = typer.Typer(
    name="wq",
    help="One-command agent workflows on top of herdr.",
    no_args_is_help=True,
    add_completion=False,
)


class Context:
    """Global options, resolved once and shared by every command."""

    def __init__(self) -> None:
        self.config = config_module.Config()
        self.json = False
        self.debug = False


_ctx = Context()


def reset_context() -> None:
    """Discard resolved global options.

    A process normally runs one command and exits, so this exists for tests: without it
    one invocation's `--json` leaks into the next case through module state.
    """
    global _ctx
    _ctx = Context()


def _run[R](coro: Coroutine[Any, Any, R]) -> R:
    """The one place an event loop is started."""
    return asyncio.run(coro)


def _fail(exc: WqError) -> None:
    console.error(exc.message, why=exc.why, fix=exc.fix)
    raise typer.Exit(exc.exit_code)


def _guard(fn: Callable[[], None]) -> None:
    """Run a command body, turning WqError into a clean message and exit code.

    Python tracebacks are for --debug. Someone mid-workflow needs the next command, not a
    stack.
    """
    try:
        fn()
    except WqError as exc:
        if _ctx.debug:
            raise
        _fail(exc)


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import version

        typer.echo(version("herdr-workflow"))
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True)
    ] = False,
    config: Annotated[Path | None, typer.Option("--config", help="Path to a config file.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", help="Show extra detail.")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Show Python tracebacks.")] = False,
) -> None:
    _ctx.json = as_json
    _ctx.debug = debug
    console.set_verbose(verbose or debug)
    try:
        _ctx.config = config_module.load(config)
    except WqError as exc:
        if debug:
            raise
        _fail(exc)


@app.command("list")
def cmd_list() -> None:
    """Show active wq workspaces."""

    def body() -> None:
        cfg = _ctx.config
        path = socket_path.resolve(cfg.herdr.session, cfg.herdr.socket)

        async def go() -> listing.Listing:
            async with connect(path) as client:
                return await listing.collect(client, cfg.root)

        result = _run(go())
        if _ctx.json:
            print(
                json.dumps(
                    {
                        "live": [
                            {
                                "workspace_id": r.workspace_id,
                                "label": r.label,
                                "agent_status": r.agent_status,
                                "is_build": r.is_build,
                                "current": r.current,
                            }
                            for r in result.rows
                        ],
                        "current": result.current,
                        "scratch": result.scratch,
                        "root": str(result.root),
                    },
                    indent=2,
                )
            )
        else:
            print(listing.render(result))

    _guard(body)


@app.command("doctor")
def cmd_doctor() -> None:
    """Check that the environment is ready, and explain anything that is not."""

    def body() -> None:
        checks = _run(doctor_workflow.run(_ctx.config, Path.cwd()))
        if _ctx.json:
            print(
                json.dumps(
                    {
                        "status": doctor_workflow.worst(checks).value,
                        "checks": [
                            {
                                "name": c.name,
                                "status": c.status.value,
                                "detail": c.detail,
                                "fix": c.fix,
                            }
                            for c in checks
                        ],
                    },
                    indent=2,
                )
            )
        else:
            print(doctor_workflow.render(checks))

        if doctor_workflow.worst(checks) is doctor_workflow.Status.FAIL:
            raise typer.Exit(1)

    _guard(body)


@app.command("up")
def cmd_up() -> None:
    """Bring up the inbox and its router. Idempotent."""

    def body() -> None:
        cfg = _ctx.config
        path = socket_path.resolve(cfg.herdr.session, cfg.herdr.socket)

        async def go() -> inbox.UpResult:
            async with connect(path) as client:
                return await inbox.up(client, cfg, Path.home())

        result = _run(go())
        if _ctx.json:
            print(
                json.dumps(
                    {
                        "workspace_id": result.workspace_id,
                        "pane_id": result.pane_id,
                        "created_workspace": result.created_workspace,
                        "started_router": result.started_router,
                    },
                    indent=2,
                )
            )
            return
        if result.created_workspace:
            console.log(f"created workspace '{cfg.herdr.inbox_label}'")
        console.log("router started" if result.started_router else "router already running")

    _guard(body)


@app.command("chat")
def cmd_chat(
    message: Annotated[list[str], typer.Argument(help="The message to send.")],
) -> None:
    """Send a message to the reusable inbox chat tab."""

    def body() -> None:
        cfg = _ctx.config
        path = socket_path.resolve(cfg.herdr.session, cfg.herdr.socket)

        async def go() -> tabs.TabResult:
            async with connect(path) as client:
                return await tabs.chat(client, cfg, " ".join(message), Path.home())

        result = _run(go())
        _emit_tab(result)

    _guard(body)


@app.command("ask")
def cmd_ask(
    question: Annotated[list[str], typer.Argument(help="The question to ask.")],
) -> None:
    """Ask a question in a fresh inbox tab, scoped to the current directory."""

    def body() -> None:
        cfg = _ctx.config
        path = socket_path.resolve(cfg.herdr.session, cfg.herdr.socket)
        # WQ_ASK_CWD is documented usage in the router prompt:
        # `WQ_ASK_CWD=<dir> wq ask "..."`.
        cwd = Path(os.environ.get("WQ_ASK_CWD", "")).expanduser() or Path.cwd()

        async def go() -> tabs.TabResult:
            async with connect(path) as client:
                return await tabs.ask(client, cfg, " ".join(question), cwd)

        result = _run(go())
        _emit_tab(result)
        if not _ctx.json:
            console.log(f"tab {result.tab_label} — close it with: wq tidy")

    _guard(body)


@app.command("tidy")
def cmd_tidy() -> None:
    """Close finished ask tabs."""

    def body() -> None:
        cfg = _ctx.config
        path = socket_path.resolve(cfg.herdr.session, cfg.herdr.socket)

        async def go() -> tabs.TidyResult:
            async with connect(path) as client:
                return await tabs.tidy(client, cfg)

        result = _run(go())
        if _ctx.json:
            print(
                json.dumps({"closed": result.closed, "kept_working": result.kept_working}, indent=2)
            )
            return
        console.log(f"closed {len(result.closed)} ask tab(s)")
        for label in result.kept_working:
            console.detail(f"  kept {label} — still working")

    _guard(body)


def _emit_tab(result: tabs.TabResult) -> None:
    if _ctx.json:
        print(
            json.dumps(
                {
                    "workspace_id": result.workspace_id,
                    "tab_label": result.tab_label,
                    "pane_id": result.pane_id,
                    "created_tab": result.created_tab,
                },
                indent=2,
            )
        )


@app.command("plan")
def cmd_plan(
    slug: Annotated[str, typer.Argument(help="Short name for this piece of work.")],
    request: Annotated[list[str], typer.Argument(help="What you want planned.")],
) -> None:
    """Run the plan <-> review loop and write plan.md."""

    def body() -> None:
        cfg = _ctx.config
        path = socket_path.resolve(cfg.herdr.session, cfg.herdr.socket)

        async def go() -> planning.PlanResult:
            async with connect(path) as client:
                return await planning.plan(client, cfg, slug, " ".join(request))

        result = _run(go())
        if _ctx.json:
            print(
                json.dumps(
                    {
                        "slug": result.slug,
                        "workspace_id": result.workspace_id,
                        "plan": str(result.plan_file),
                        "review": str(result.review_file),
                        "rounds": result.rounds,
                        "approved": result.approved,
                    },
                    indent=2,
                )
            )
            return
        console.log(f"plan:   {result.plan_file}")
        console.log(f"review: {result.review_file}")
        console.log(f"next:   wq build {result.slug} <repo>")

    _guard(body)


@app.command("build")
def cmd_build(
    slug: Annotated[str, typer.Argument(help="The slug whose plan to implement.")],
    repo: Annotated[
        Path | None, typer.Argument(help="Repository to build in. Defaults to the cwd.")
    ] = None,
) -> None:
    """Create a worktree and run the code <-> review loop."""

    def body() -> None:
        cfg = _ctx.config
        path = socket_path.resolve(cfg.herdr.session, cfg.herdr.socket)

        async def go() -> building.BuildResult:
            async with connect(path) as client:
                return await building.build(client, cfg, slug, repo or Path.cwd())

        result = _run(go())
        if _ctx.json:
            print(
                json.dumps(
                    {
                        "slug": result.slug,
                        "workspace_id": result.workspace_id,
                        "worktree": str(result.worktree),
                        "branch": result.branch,
                        "base": result.base,
                        "diff": str(result.diff_file),
                        "review": str(result.review_file),
                        "rounds": result.rounds,
                        "approved": result.approved,
                    },
                    indent=2,
                )
            )
        elif result.approved:
            # Only on approval. A build that stopped at its round cap has already said so,
            # and pointing at `wq ship` for unreviewed code would be the wrong advice.
            console.log(f"worktree: {result.worktree}")
            console.log(f"diff:     {result.diff_file}")
            console.log(f'next:     wq revise {result.slug} "..."  |  wq ship {result.slug}')

        # Exit 2, not 1: the router's contract is to stop on any non-zero exit, and this
        # one means "unreviewed code is sitting on a branch", not "wq broke". The loop has
        # already printed the round-limit lines, so this adds no error block of its own.
        if not result.approved:
            raise typer.Exit(2)

    _guard(body)


@app.command("revise")
def cmd_revise(
    slug: Annotated[str, typer.Argument(help="The build to revise.")],
    comment: Annotated[list[str], typer.Argument(help="The change you want.")],
) -> None:
    """Run one more code + review round on an existing build."""

    def body() -> None:
        cfg = _ctx.config
        path = socket_path.resolve(cfg.herdr.session, cfg.herdr.socket)

        async def go() -> revising.ReviseResult:
            async with connect(path) as client:
                return await revising.revise(client, cfg, slug, " ".join(comment))

        result = _run(go())
        if _ctx.json:
            print(
                json.dumps(
                    {
                        "slug": result.slug,
                        "worktree": str(result.worktree),
                        "branch": result.branch,
                        "delta": str(result.delta_file),
                        "diff": str(result.diff_file),
                        "review": str(result.review_file),
                        "approved": result.approved,
                    },
                    indent=2,
                )
            )
            return
        console.log(f"delta:  {result.delta_file}")
        console.log(f"diff:   {result.diff_file}")
        console.log(f"review: {result.review_file}")
        console.log(f'next:   wq revise {result.slug} "..."  |  wq ship {result.slug}')
        # Exit 0 whether or not the reviewer approved. Unlike `build`'s round cap, findings
        # here are the thing you asked for -- reading them is the next step, not an error.

    _guard(body)


@app.command("brainstorm")
def cmd_brainstorm(
    slug: Annotated[str, typer.Argument(help="Short name for the idea.")],
    idea: Annotated[list[str], typer.Argument(help="The idea to explore.")],
) -> None:
    """Open a brainstorming pane with a running note."""

    def body() -> None:
        cfg = _ctx.config
        path = socket_path.resolve(cfg.herdr.session, cfg.herdr.socket)

        async def go() -> brainstorming.BrainstormResult:
            async with connect(path) as client:
                return await brainstorming.brainstorm(client, cfg, slug, " ".join(idea))

        result = _run(go())
        if _ctx.json:
            print(
                json.dumps(
                    {
                        "slug": result.slug,
                        "workspace_id": result.workspace_id,
                        "pane_id": result.pane_id,
                        "note": str(result.note),
                        "created_note": result.created_note,
                    },
                    indent=2,
                )
            )

    _guard(body)


@app.command("ship")
def cmd_ship(slug: Annotated[str, typer.Argument(help="The build to ship.")]) -> None:
    """Run `wq go` in a shell tab in the inbox."""

    def body() -> None:
        cfg = _ctx.config
        path = socket_path.resolve(cfg.herdr.session, cfg.herdr.socket)

        async def go() -> shipping.ShipResult:
            async with connect(path) as client:
                return await shipping.ship(client, cfg, slug, Path.home())

        result = _run(go())
        if _ctx.json:
            print(
                json.dumps(
                    {
                        "slug": result.slug,
                        "workspace_id": result.workspace_id,
                        "tab_label": result.tab_label,
                        "pane_id": result.pane_id,
                        "command": result.command,
                        "created_tab": result.created_tab,
                    },
                    indent=2,
                )
            )

    _guard(body)


@app.command("go")
def cmd_go(
    slug: Annotated[str, typer.Argument(help="The build to push, merge and clean up.")],
) -> None:
    """Push, open a PR, wait for CI, merge, and clean up.

    Blocks for the length of CI. Use `wq ship` unless you are already in a plain shell.
    """

    def body() -> None:
        cfg = _ctx.config
        path = socket_path.resolve(cfg.herdr.session, cfg.herdr.socket)

        async def run() -> shipping.GoResult:
            async with connect(path) as client:
                return await shipping.go(client, cfg, slug)

        result = _run(run())
        if _ctx.json:
            print(
                json.dumps(
                    {
                        "slug": result.slug,
                        "pr": result.pr,
                        "branch": result.branch,
                        "merged": result.merged,
                    },
                    indent=2,
                )
            )

    _guard(body)


@app.command("clean")
def cmd_clean(slug: Annotated[str, typer.Argument(help="The slug to drop.")]) -> None:
    """Drop a slug's workspaces and its scratch directory."""

    def body() -> None:
        cfg = _ctx.config
        path = socket_path.resolve(cfg.herdr.session, cfg.herdr.socket)

        async def go() -> cleanup.CleanResult:
            async with connect(path) as client:
                return await cleanup.clean(client, slug, cfg.root)

        result = _run(go())
        if _ctx.json:
            print(
                json.dumps(
                    {
                        "closed_workspaces": result.closed_workspaces,
                        "closed_tabs": result.closed_tabs,
                        "removed_dir": str(result.removed_dir) if result.removed_dir else None,
                    },
                    indent=2,
                )
            )
            return
        console.log(f"cleaned {slug}")
        for label in result.closed_workspaces:
            console.detail(f"  closed workspace {label}")
        for label in result.closed_tabs:
            console.detail(f"  closed tab {label}")
        if result.removed_dir:
            console.detail(f"  removed {result.removed_dir}")

    _guard(body)


# The router and existing scripts use these aliases, so they are part of the contract.
@app.command("ls", hidden=True)
def cmd_ls() -> None:
    """Alias for `list`."""
    cmd_list()


@app.command("rm", hidden=True)
def cmd_rm(slug: Annotated[str, typer.Argument()]) -> None:
    """Alias for `clean`."""
    cmd_clean(slug)


def entrypoint() -> None:
    try:
        app()
    except KeyboardInterrupt:
        print(file=sys.stderr)
        raise SystemExit(130) from None
