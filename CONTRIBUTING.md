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
within seconds whether you broke something. The Bash implementation this replaces could
only be tested by running it against real agents — minutes and real tokens per iteration —
and that, not line count, is what the rewrite bought.

Before pushing:

```bash
uv run ruff format . && uv run ruff check . && uv run pyright && uv run pytest
```

## How it fits together

```
cli.py            Typer commands. One asyncio.run at the edge, nowhere else.
config.py         defaults -> user config -> project config -> WQ_* env
workflows/        what each command does. No sockets in here.
herdr/            the socket client, and the reliability layer above it
  client.py       one request per connection -- see docs/protocol-framing.md
  delivery.py     getting a prompt into a TUI and knowing it landed
  ops.py          typed wrappers over the herdr methods wq uses
git/              subprocess wrappers; gh.py is the pull-request half
protocol/         wire types (msgspec)
output/           console formatting -- the router reads this, so it is a contract
```

The workflow layer never touches a socket, and `herdr/` never decides policy. If you find
yourself reaching for `HerdrClient` inside `workflows/`, that is what `ops.py` is for.

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

`wq`'s CLI surface is a compatibility contract: it is called by an agent router that reads
its output and its exit codes. Exit codes in particular are load-bearing — `build` exits 2
at its round cap where `revise` exits 0. If you change an output format or an exit code, say
so in [docs/parity.md](docs/parity.md) under "Known deviations", with the reasoning.
