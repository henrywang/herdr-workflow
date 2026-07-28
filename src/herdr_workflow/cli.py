"""The `wq` command line.

Typer is synchronous and the herdr client is async. The boundary is `_run` and nowhere
else -- one `asyncio.run` at the command edge. Scattering event loops through the
workflow layer is how this stops being testable.

The Bash CLI surface is the compatibility contract. Command names, aliases, argument
order, and exit codes all match, because the router prompt in devcage-macos calls them by
name and reads their output.
"""

from __future__ import annotations

import asyncio
import json
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
from herdr_workflow.workflows import cleanup, inbox, listing
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


# Aliases the Bash implementation accepted. The router and years of muscle memory use
# them, so they are part of the contract rather than a convenience.
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
