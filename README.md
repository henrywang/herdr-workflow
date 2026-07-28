# wq — agent workflows on top of [herdr](https://herdr.dev)

`wq` runs opinionated multi-agent loops in real terminal panes: a planner drafts, a
*different model* reviews adversarially, they iterate under a hard round cap, and the
result ships as a merged pull request.

> **Status: early.** Phase 1 (`list`, `doctor`) works. The remaining commands are being
> ported from a Bash implementation that has been in daily use — see
> [PLAN.md](PLAN.md) for the order.

## Two ideas worth stealing, even if you never run this

**1. Cross-model adversarial review with bounded rounds.**
The reviewer is deliberately not the model that wrote. Findings are classified BLOCKING or
NON-BLOCKING, and the reviewer ends its file with exactly one machine-readable line:

```
VERDICT: APPROVED     |     VERDICT: CHANGES
```

Convergence becomes a grep, not an interpretation. Rounds are capped so a disagreeing pair
cannot loop forever. Content moves between panes **by path, never pasted** — the single
largest token sink in a loop like this.

**2. [`docs/behaviors.md`](docs/behaviors.md) — what actually breaks when you drive
coding-agent TUIs unattended.**
Ten failure modes discovered the hard way. A pane reports "ready" while it will silently
swallow your prompt. `agent prompt` returning OK does not mean the agent took the text.
`done` is not a state that persists. If you are scripting Claude Code, Codex, or Amp,
you will hit these whether or not you use herdr.

## Install

```bash
uv tool install herdr-workflow
wq doctor
```

## Commands

```bash
wq up                              # bring up inbox + router (idempotent)
wq chat       "<message>"          # reuse the inbox chat tab
wq ask        "<question>"         # new inbox tab, scoped to $PWD
wq tidy                            # close finished ask tabs
wq brainstorm <slug> "<idea>"      # interactive; note lands in your notes sink
wq plan       <slug> "<request>"   # plan <-> review loop -> plan.md
wq build      <slug> [repo]        # worktree, code <-> review loop, commit
wq revise     <slug> "<comment>"   # one more code + review round on a build
wq ship       <slug>               # run `wq go` in an inbox shell tab
wq go         <slug>               # push, PR, wait for CI, merge, clean up
wq list                            # show active wq workspaces
wq clean      <slug>               # drop the workspace and scratch dir
wq doctor                          # check the environment
```

Global: `--json`, `--verbose`, `--debug`, `--config`, `--version`.

## Configuration

`~/.config/wq/config.toml`, overridden by `.wq.toml` in the project, overridden by
`WQ_*` environment variables.

```toml
[agents]
plan   = "claude:opus:high"
code   = "claude:sonnet:high"
review = "pi:openai-codex/gpt-5.6-sol:high"   # not the model that wrote

[loops]
plan_rounds = 3
code_rounds = 3

[paths]
root  = "~/Workspace/.wq"
notes = ""    # note sink for `wq brainstorm`

[herdr]
socket = "auto"
```

Roles are `kind:model:level`. Either side of a loop can be either agent — the rule that
matters is that the reviewer is not the model that wrote.

## Development

```bash
git clone https://github.com/henrywang/herdr-workflow
cd herdr-workflow
uv sync
uv run pytest          # no herdr installation required
```

Tests run against a fake herdr daemon (`tests/fake_herdr.py`) that speaks the real
newline-delimited JSON protocol over a real unix socket, so framing, id correlation, and
event interleaving are all under test.

```bash
uv run ruff format . && uv run ruff check . && uv run pyright && uv run pytest
```

Integration tests need a running daemon: `uv run pytest -m integration`.

- [docs/protocol-framing.md](docs/protocol-framing.md) — the socket API, established
- [docs/parity.md](docs/parity.md) — how Bash/Python parity is checked, and what has been
- [docs/behaviors.md](docs/behaviors.md) — the failure-mode catalogue
- [PLAN.md](PLAN.md) — architecture and migration order

## License

MIT
