# Contributing

## Start

```bash
git clone https://github.com/henrywang/herdr-workflow
cd herdr-workflow
uv sync
uv run pytest        # ~30s, and does not need herdr installed
```

That last part is the point. The unit suite runs against a fake herdr daemon and a fake
`gh`, so you can change the retry logic, the round loops, or the cleanup ordering and know
within seconds whether you broke something.

The alternative is to verify every change by running it against real agents, at minutes
and real tokens per iteration. If you find
yourself about to do that, it usually means a fake needs extending rather than that the
change is untestable.

Before pushing:

```bash
uv run ruff format . && uv run ruff check . && uv run pyright && uv run pytest
```

## How it fits together

```
src/herdr_workflow/
  __main__.py          `python -m herdr_workflow` entry point
  cli.py               Typer commands, output selection, and process exit codes
  config.py            defaults -> user config -> project config -> WQ_* environment
  errors.py            user-facing errors with causes and remedies

  git/
    __init__.py        local Git operations: repositories, bases, diffs, pushes, branches
    gh.py              GitHub CLI operations: pull requests, checks, and merge

  herdr/
    agents.py          start agents reliably and handle pane/name races
    client.py          NDJSON socket transport; one connection per request
    delivery.py        deliver prompts and confirm that the TUI received them
    ops.py             typed wrappers and snapshot lookups for herdr API methods
    socket_path.py     resolve the active herdr socket from config and environment

  output/
    console.py         stable human-readable output consumed by the router

  protocol/
    messages.py        msgspec types for the herdr wire messages wq reads

  workflows/
    brainstorming.py   interactive brainstorm workspace and note management
    build_env.py       persisted metadata shared by build, revise, ship, and clean
    building.py        worktree creation and the code/review loop
    cleanup.py         workspace, tab, parent-workspace, and scratch-directory cleanup
    doctor.py          environment, configuration, daemon, and protocol checks
    inbox.py           idempotent inbox and router startup
    listing.py         discover and render active plans and builds
    loops.py           shared round outcomes and output-file validation
    planning.py        planner/reviewer loop
    prompts.py         agent instructions and machine-readable review verdicts
    revising.py        one additional code turn and review turn
    shipping.py        push, PR, CI, merge, and post-merge cleanup
    tabs.py            chat, ask, and tidy inbox tabs
```

The package `__init__.py` files only mark package boundaries or export names. Workflow
modules decide policy and use `HerdrClient` only through its high-level operations;
`herdr/client.py` alone owns socket framing. The `herdr/` modules handle transport and
reliability without deciding workflow policy.

## The one rule that is not style

**Do not simplify anything in [docs/behaviors.md](docs/behaviors.md) without reproducing
it first.**

Every entry there is a failure mode found by losing an afternoon to it. They all look like
defensive over-engineering until they happen to you: the readiness check that waits for a
signal herdr often never sends, the retry that only fires on one error code, the file mtime
compared before and after a turn. The catalogue exists so the reasoning survives a reader
who has not hit them yet.

If one of them *is* obsolete — herdr fixes something, an agent CLI changes — the way to
show it is a test that fails against the old behaviour and passes against the new, plus an
edit to the entry saying when and why it changed. Not a deletion.

## Tests

**Unit tests** (`tests/unit/`) run against fakes and must never need a network, a daemon,
or an API key.

- `tests/fake_herdr.py` speaks the real NDJSON protocol over a real unix socket, and closes
  after each response exactly as herdr does.
- `tests/fake_gh.py` is a real executable on `PATH`, so tests see the argv wq actually
  builds.
- `tests/build_scenario.py` gets you to a built state — real git worktree, real commits —
  in one call.

Fakes are deliberately not mocks. A mocked wrapper asserts that the code calls the code the
way the code expects to call it; twice now a fake that agreed with a misreading let a real
bug through 160+ green tests. Both times the fix was to make the fake match the real thing
and add an integration test. See behaviors #11 and #12.

**Integration tests** (`tests/integration/`) need a running daemon and are skipped without
one:

```bash
uv run pytest -m integration
```

Add one whenever you model a new response shape. They are the only thing that can tell you
your reading of the protocol is wrong.

## Changing prompts

`workflows/prompts.py` is product, not configuration. The review protocol — BLOCKING /
NON-BLOCKING, one machine-readable verdict line — is the most reusable idea in the project.
Two constraints on anything you change there:

- **Reviewers get a path, never pasted content.** It is the largest token sink in a loop
  like this and it grows every round.
- **Convergence stays a grep.** `approved()` matches a whole line and nothing else, so a
  reviewer musing about approval has not approved.

## Behaviour changes

`wq`'s CLI surface is a contract, not a convenience: it is called by an agent router that
reads its output and branches on its exit codes. Exit codes in particular are load-bearing
— `build` exits **2** at its round cap, meaning "unreviewed code is sitting on a branch",
where `revise` exits **0** because findings are what you asked it for, and both are
distinct from the **1** of a real failure.

The same goes for the `*` marker in `wq list`, which is how a router picks the build to
revise. If you change an output format or an exit code, put it in
[CHANGELOG.md](CHANGELOG.md) with the reasoning — someone's automation is reading it.
