# Herdr Workflow (`wq`) Project Plan

## Vision

Port the existing `wq` Bash script to Python as an open-source workflow layer for
[herdr](https://herdr.dev), and share it.

`wq` runs opinionated multi-agent loops — plan ↔ review, code ↔ review — driving real
coding-agent TUIs unattended, all the way through to a merged pull request.

The implementation language (Python) should be invisible to end users.

---

## What this rewrite is, and what it is not

The Bash script is **591 lines of code and 212 lines of comments**. Of the code, only
~167 lines are the herdr helper layer; the remaining ~402 lines are workflow logic.

That matters, because it sets honest expectations:

- **The socket API will not make this shorter.** It removes shell-outs, `jq` parsing, and
  quoting. It does not remove the reliability logic, which survives the port unchanged.
  Expect roughly **800–1200 lines** of Python.
- **"Shorter" is not the success criterion. "Typed and testable" is.** Today the only way
  to verify a change is to run it live against real agents — minutes and tokens per
  iteration. With a fake socket server, the retry logic, the `idle`-vs-`done` handling,
  and the workspace-diffing in `build` become sub-second unit tests. That step change is
  the whole justification for the rewrite.
- **The comments are the most valuable part of the file.** They are empirically discovered
  failure modes, not style. See [Known Behaviors](#known-behaviors-the-core-deliverable).

---

## Project Naming

| Component | Name |
|-----------|------|
| Repository | `herdr-workflow` |
| Python package | `herdr_workflow` |
| CLI | `wq` |

`wq` is the product's CLI brand. Users interact only with `wq ...`.

---

## Goals

### Primary Goals

- Replace the existing Bash implementation, **command for command**.
- Communicate with herdr using the socket API.
- Preserve the current user experience exactly.
- Make the reliability knowledge testable, then documented, then shareable.
- Keep the architecture maintainable enough for outside contributors.

### Non-Goals (v0.1)

- No GUI.
- No herdr plugin.
- **No generic workflow framework.** This is an opinionated plan→review→build→ship loop
  for herdr. Hold this line — the generic version never ships.
- No support for runtimes beyond herdr.
- No new commands beyond `doctor`.

---

## Design Principles

1. CLI-first.
2. Workflow logic is independent from transport.
3. Herdr integration is isolated.
4. Strong typing throughout the codebase.
5. Small modules with clear responsibilities.
6. Configuration over hard-coded behavior.
7. Tests are first-class.
8. The Bash CLI surface is the compatibility contract.

---

## The Router Premise

`wq` is designed to be invoked **by an agent**, not only by a human. A router agent
(`pi`, in the `inbox` workspace) classifies a request and calls the matching `wq`
command. All orchestration lives in `wq` so it is deterministic and costs no tokens.

This premise explains three things that otherwise look arbitrary, and it must survive
the port:

- **`wq up`** exists to bring the inbox and router up idempotently — safe to bind to a
  key or shell startup.
- **`wq ship` exists because `wq go` cannot run just anywhere.** `go` blocks for the
  length of CI and its cleanup closes the workspace the build panes live in. Run from the
  code pane it deletes itself; run from the router it blocks the one pane that must stay
  responsive; run from any agent pane its bash tool times out. `ship` opens a plain shell
  tab with no agent in it and starts `go` there.
- **`cmd_go` enforces that rule in code**, not just in the router's prompt: if
  `HERDR_PANE_ID` is set and an agent is registered against that pane, it refuses. **This
  guard must be ported.** A clean-room reimplementation will omit it.

`--json` as a global flag exists for the same reason: the caller is often a program.

---

## High-Level Architecture

```text
                +------------------+
                |      wq CLI      |
                +------------------+
                         |
                 Workflow Engine
                         |
                 Herdr API Interface
                         |
              SocketHerdrClient
                         |
               Unix Domain Socket
                         |
                  Herdr Daemon
```

The workflow engine must never depend directly on sockets. Only the herdr client talks
to the daemon.

---

## Repository Layout

```text
herdr-workflow/
├── src/
│   └── herdr_workflow/
│       ├── __main__.py
│       ├── cli.py
│       ├── config.py
│       ├── errors.py
│       ├── models.py          # generated from the herdr API schema
│       ├── state.py
│       ├── protocol/
│       ├── herdr/
│       ├── workflows/
│       ├── git/
│       └── output/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/              # captured real TUI output (trust dialog, etc.)
│
├── docs/
│   ├── behaviors.md           # the failure-mode catalogue
│   └── architecture.md
│
├── .github/workflows/
├── pyproject.toml
├── README.md
├── CONTRIBUTING.md
└── LICENSE
```

---

## Technology Stack

- Python 3.12+
- uv
- Typer
- asyncio — **one `asyncio.run` at the command edge in `cli.py`.** Typer is synchronous;
  the async boundary lives there and nowhere else, or contributors will scatter them.
- msgspec (or pydantic v2) for typed models and protocol validation. **Decide before
  Phase 1.** Hand-rolling 89 request/response pairs is exactly the maintenance cost this
  rewrite exists to avoid.
- Unix domain socket
- pytest, Ruff, Pyright

---

## Known Behaviors (the core deliverable)

**`docs/behaviors.md` is written before Phase 1, not after.** One entry per failure mode,
each becoming a named test against the fake server.

This is the project's differentiator, not an implementation detail. Anyone scripting
Claude Code, Codex, or Amp hits this class of problem; almost nobody has solved it.

Every item below was discovered live, at cost. **"Treat Bash as the spec, do not
translate line-by-line" applies to structure, never to these.**

| # | Behavior | Bash ref |
|---|----------|----------|
| 1 | **Trust dialog.** Claude Code asks "Is this a project you trust?" in every new directory. herdr reads readiness off the prompt box and the dialog draws one, so the pane reports `interactive_ready` while accepting only a dialog answer. A prompt sent there is swallowed **and its Enter confirms the highlighted option.** | `wq:173-197` |
| 2 | **Prompt delivery must be confirmed, not assumed.** `agent prompt` returning OK means herdr accepted the request, not that the agent took the text. Claude Code draws a composer for tens of seconds after the trust dialog while discarding everything typed into it. `state_change_seq` is the receipt. Includes comparing against the *starting* status, since a late-starting turn looks identical to a dropped prompt. | `wq:199-244` |
| 3 | **`agent_pane_busy` retry.** A freshly created pane is not at an interactive prompt yet; starting an agent fails. Short gap, but real — retry. | `wq:103-129` |
| 4 | **Agent names are global in herdr.** Concurrent workspaces starting their own `idea` or `review` pane collide with `agent_name_taken`. Qualify by pane id, lowercased, `[a-z0-9_-]` only; keep the short name for the pane label. **This is only safe because nothing looks agents up by name** — panes are found by label from the snapshot, agents are targeted by pane id. Do not reintroduce name-based lookup. | `wq:105-111` |
| 5 | **`idle` vs `done`.** `done` is where a finished turn lands but it does not persist — a pane that has been read settles back to `idle`. Waiting on `done` alone hangs for the full turn timeout on work that finished minutes ago. Idle must be re-confirmed, because panes flash idle mid-turn. | `wq:250-281` |
| 6 | **`worktree create` opens *two* workspaces** when the repo has none open: the linked worktree and one for the parent checkout. Only the first is in the response. The second is found by diffing workspace ids around the call, or it leaks. Recorded as line 4 of `build.env`. | `wq:531-553` |
| 7 | **`gh pr merge --delete-branch` exits non-zero *after* the merge lands** in a worktree checkout — gh switches to the default branch first, git refuses because main is checked out in the parent. Merge only; delete both branches afterwards, once the worktree is gone. | `wq:775-782` |
| 8 | **Parent-workspace close guards.** Never close the workspace the command is running in. Only close if it is still what `wq` left behind: 1 tab, 1 pane, no agent. Anything else means someone adopted it. | `wq:76-94` |
| 9 | **Terminal state is a heuristic; a file on disk is not.** Every turn expected to produce output is confirmed by the file's mtime actually changing. | `wq:299-304` |
| 10 | **Capture, don't pipe.** `herdr status \| grep -q` exits early, herdr takes SIGPIPE, and `pipefail` turns a healthy server into a failed check. (Bash-specific, but the lesson — read the whole response — carries.) | `wq:51-56` |

**Important:** `events.subscribe` / `events.wait` and `agent.prompt`'s inline `wait`
option do **not** replace items 1 and 2. Those are *screen* states, not event states.

### Fixture capture — do this first, while Bash still runs

Capture real `agent read --source visible` output for the trust dialog and the
post-dialog dead composer into `tests/fixtures/`. Once Bash is retired these are
expensive to reproduce.

**Done for the trust dialog** (`tests/fixtures/claude-trust-dialog.txt`, 2026-07-28), and
it earned its keep immediately: it is the only thing proving `TRUST_DIALOG` matches what
Claude Code actually draws, rather than matching a string a test wrote itself. The capture
also confirmed behavior #1's central claim — the pane reports `interactive_ready: true`
with the dialog up, and reports *exactly the same state* once the dialog is gone.

---

## Herdr Layer

Responsible for: socket connection, request/response, event subscriptions, reconnect,
protocol validation, timeouts. Everything herdr-specific lives here.

### Generate models from the shipped schema

`herdr api schema --json` emits the full contract — **protocol 17, schema_version 1, 89
methods, ~248 KB**. Generate `models.py` from it rather than hand-writing types.

- **Pin the protocol version** in the package.
- **`wq doctor` compares the running server's protocol against the pinned one** and says
  so plainly when they diverge. A shared tool against a young, fast-moving API will break
  for strangers in ways it never breaks for you.
- Regenerating models becomes a single command when herdr bumps the protocol.

### Use the API that Bash could not

- `events.subscribe` / `events.wait` replace polling loops for workspace and agent
  lifecycle.
- `agent.prompt` accepts inline `wait { until: [...], timeout_ms }`.

Both are real wins. Neither removes the `state_change_seq` receipt or trust-dialog
detection.

### Known CLI/socket gaps

**`herdr pane run` has no socket method.** `wq ship` depends on it (`wq:705`). The socket
surface has `pane.send_text`, `pane.send_keys`, `pane.send_input`
(`{pane_id, text, keys}`), and `pane.wait_for_output`. `pane run` is a client-side
convenience — text plus an Enter key. Consequences to handle:

- **Shell quoting becomes `wq`'s responsibility.** The Bash version handed argv to herdr.
- The "check the returned JSON for an error" logic in `cmd_ship` changes meaning, because
  there is no longer a server-side call that can report one.

**Pre-Phase-2 task: audit which `herdr` CLI conveniences lack a direct socket method.**
`pane run` is the one already found; assume it is not the only one.

### Unvalidated assumption

The schema documents messages but **not the framing or handshake**. See Phase 0.

---

## Workflow State

Persist enough to recover interrupted workflows: workflow id, workspace id, pane ids,
worktree paths, phase, timestamps. Use atomic writes.

Note what Bash already gets right and keep it: **the filesystem is the index.** Build
panes are found by label from the snapshot, not from stored ids, so nothing drifts.
`diff.patch`'s mtime already records which build was worked on last. Do not add an index
that has to be kept in sync when the answer can be read out of state that had to exist
anyway.

Concurrency is real — several `wq` commands can run at once, and several of the behaviors
above exist only because of it. Say explicitly how state files handle that.

### `build.env` is a compatibility surface

Four newline-separated lines: `repo`, `branch`, `wt_path`, `parent` — **the fourth
optional**, because files written before `wq` recorded it have only three, and the Bash
reader tolerates that (`wq:736-739`).

Because the cutover runs Bash and Python side by side, **v0.1 reads and writes this exact
format, unchanged.** Any richer state format waits until Bash is retired. Otherwise
in-flight builds break at precisely the moment both implementations are live.

---

## Configuration

Three layers: built-in defaults → user config → project config, with **environment
variables as a final override layer** (the Bash `WQ_*` vars are a de-facto config API and
must keep working).

Roles are `kind:model:level`, so either side of a loop can be either agent. The rule that
matters is that the reviewer is not the model that wrote.

```toml
[agents]
plan   = "claude:opus:high"
code   = "claude:sonnet:high"
review = "pi:openai-codex/gpt-5.6-sol:high"
idea   = "claude:opus:high"
ask    = "pi:openai-codex/gpt-5.6-sol:medium"
router = "pi:openai-codex/gpt-5.6-sol:medium"

[loops]
plan_rounds      = 3
code_rounds      = 3
turn_timeout_ms  = 1800000
prompt_attempts  = 5
ci_appear_timeout = 120

[paths]
root = "~/Workspace/.wq"
# Note sink for `wq brainstorm`. No default vault path.
notes = ""

[herdr]
socket      = "auto"
inbox_label = "inbox"

[claude]
permission_mode = "auto"   # unattended panes work only in scratch dirs / throwaway worktrees
```

Environment overrides to preserve: `WQ_ROOT`, `WQ_VAULT`, `WQ_PLAN_ROUNDS`,
`WQ_CODE_ROUNDS`, `WQ_TURN_TIMEOUT_MS`, `WQ_CI_APPEAR_TIMEOUT`, `WQ_AGENT_PLAN`,
`WQ_AGENT_CODE`, `WQ_AGENT_REVIEW`, `WQ_AGENT_IDEA`, `WQ_AGENT_ASK`, `WQ_AGENT_ROUTER`,
`WQ_CLAUDE_PERMISSION_MODE`, `WQ_PROMPT_ATTEMPTS`, `WQ_INBOX_LABEL`, `WQ_ASK_CWD`.

### Prompts and the review protocol

Agent prompt texts and `REVIEW_PROTOCOL` are **core product**, not private
configuration. Externalize them as overridable templates and ship the defaults.

The review protocol is one of the most copyable ideas here — findings classified
BLOCKING / NON-BLOCKING, an adversarial instruction, and a machine-readable terminal
line so convergence is a grep and not an interpretation:

```
VERDICT: APPROVED   |   VERDICT: CHANGES
```

---

## CLI

The Bash surface is the contract. **All twelve commands ship**, plus `doctor`.

```bash
wq up                              # bring up inbox + router (idempotent)
wq chat       "<message>"          # reuse the inbox chat tab
wq ask        "<question>"         # new inbox tab, scoped to $PWD
wq tidy                            # close finished ask tabs
wq brainstorm <slug> "<idea>"      # interactive; note lands in the configured sink
wq plan       <slug> "<request>"   # plan <-> review loop -> plan.md
wq build      <slug> [repo]        # worktree, code <-> review loop, commit
wq revise     <slug> "<comment>"   # one more code + review round on a build
wq ship       <slug>               # run `wq go` in an inbox shell tab
wq go         <slug>               # push, PR, wait for CI, merge, clean up
wq list                            # show active wq workspaces
wq clean      <slug>               # drop the workspace and scratch dir

wq doctor                          # new: verify the environment
```

Aliases to preserve: `bs`, `rev`, `ls`, `rm`.

**There is no `wq review` command.** Review is a loop *phase* inside `plan` and `build`.
The user-facing command is `revise`, and it deliberately runs exactly one code turn and
one review turn, then hands back — by that point the reviewer has had its rounds, so a
new finding deserves your eyes rather than an automatic rewrite.

**`wq status` is not in scope.** It does not exist in Bash; `list` covers it.

Global options: `--help`, `--version`, `--json`, `--verbose`, `--debug`, `--config`.

Note: Bash generates `--help` by `sed`-ing its own header comment block (`wq:921`), so
the current help text is exactly those usage lines. Typer generates its own.

**Resolved:** the router does not depend on the format — `go.md` carries its own command
table and never shells out to `wq --help`. Typer's help is taken as-is.

---

## Doctor Command

Checks:

- herdr installed
- herdr daemon running
- socket reachable
- **server protocol version matches the pinned one**
- git installed, `gh` installed
- current repository
- configuration valid
- configured agents available

Output explains how to fix each failure.

---

## Error Handling

Errors explain: what failed, why, how to fix it, where to look. No Python tracebacks
unless `--debug`.

Keep the Bash habit of naming the escape hatch in the message itself — e.g. *"attach with:
`herdr agent attach <pane>`"*, *"if the repo has no CI, merge by hand: `gh pr merge ...`"*.

---

## Testing

### Unit tests — no herdr required

Workflow logic, retry logic, configuration, state, git helpers.

### Fake socket server

Supports success, errors, reconnects, malformed messages, delayed responses, events —
**and every behavior in the catalogue above**, replayed from `tests/fixtures/`. Each
numbered behavior gets a named test.

### Integration tests

Against a real herdr daemon: workspace creation, pane creation, prompts, workflow
lifecycle.

### CLI tests

Invoke commands exactly as users do.

---

## GitHub Actions

Ruff, Pyright, unit tests, package build. Integration tests run against a herdr daemon
when one is available.

---

## Migration Plan

Treat the Bash implementation as the specification. Do not translate line-by-line —
**except for the behavior catalogue, which is ported deliberately and tested.**

### Phase 0 — de-risk before writing the app

1. **Socket framing spike.** The schema documents messages, not the handshake or framing.
   One focused session; everything downstream depends on it.
2. **Capture TUI fixtures** while Bash is still driving real panes.
3. **Write `docs/behaviors.md`.**
4. **Pick the validation library**; generate `models.py` from `herdr api schema --json`.
5. **Audit CLI conveniences with no socket method** (starting with `pane run`).

**Status:** Phases 0–7 are done. **All 13 commands are ported**, and every one has been
driven live except the push-to-merge half of `go`, which needs a real GitHub repository
and the user's authorisation — see docs/parity.md.

### Phase 1 — read-only
`list`, `doctor`

### Phase 2 — workspace lifecycle
`up`, `clean`, plus the workspace/tab helpers.

Parent-workspace handling (behavior #8) lands here with `clean` and is **reused by `go`
in Phase 7** — `close_parent_ws` has two callers, and `clean` reads the parent id from
`build.env` line 4 because the label-based loop cannot see a workspace labelled with the
repo rather than the slug.

### Phase 3 — inbox commands
`chat`, `ask`, `tidy`

### Phase 4 — the loop core
`plan` (delivery confirmation, trust dialog, round loop, `VERDICT` convergence)

### Phase 5 — build
`build` (worktree, two-workspace diff, code ↔ review loop)

The base ref stops being hard-coded here. Bash meant `origin/main` in four places; Python
resolves it once from the repository and records it on line five of `build.env`, which
Bash's four-line reader ignores. See docs/parity.md.

`build` exits **2** at its round cap — a router contract (`go.md:61`), and distinct from
the `1` of a real failure.

### Phase 6 — revise
`revise`

Exactly one code turn and one review turn — `build` had the bounded loop, and from here
the user is the round counter. Exits **0** whether the reviewer approves or not: findings
from a revise are the thing you asked for.

Two diffs with two ranges: `diff.patch` three-dot from the recorded base, `revise.patch`
two-dot from the pre-turn `HEAD`.

### Phase 7 — ship
`brainstorm`, `ship`, `go` (including the agent-pane guard and merge/cleanup ordering)

**A phase is done when it has driven a real workspace end-to-end — not when its unit
tests pass.**

### Cutover

**Keep the Bash script installed alongside Python through v0.1.** Every behavior in the
catalogue was discovered live; assume the list is incomplete. `devcage-macos` removes the
Bash script only once every command has been driven end-to-end in Python.

---

## Public Project

Must work outside the original environment. Avoid hard-coded paths, machine-specific
config, and personal assumptions.

Specifically, these are personal and must be configurable or optional:

- **`brainstorm`'s Obsidian vault** is currently a hard-coded iCloud path
  (`wq:23`). It becomes a configurable note sink with no default.
- **The `inbox` / `chat` / `ask` tab habit** — keep the commands, make the labels and
  layout configurable.
- **Specific router and agent model choices** — already config, keep them there.

---

## Documentation

README:

1. What is `wq`?
2. Installation
3. Quick Start
4. Commands
5. Configuration
6. Troubleshooting
7. Development

**Lead with the two ideas people will actually come for:**

1. **Cross-model adversarial review with bounded rounds.** Writer and reviewer are
   deliberately different models; the reviewer is told to be adversarial; findings are
   BLOCKING / NON-BLOCKING; convergence is a machine-readable verdict; the round count is
   capped so it cannot loop forever. Content is passed **by path, never pasted between
   panes** — the single largest token sink in a loop like this.
2. **What actually breaks when you drive coding-agent TUIs unattended** — i.e.
   `docs/behaviors.md`. This travels well beyond herdr users. The code is the proof; the
   knowledge is the draw.

Quick Start:

```bash
uv tool install herdr-workflow
wq doctor
cd my-project
wq plan my-feature "Add authentication"
```

---

## Installation

```bash
uv tool install herdr-workflow      # recommended
```

Development:

```bash
git clone https://github.com/henrywang/herdr-workflow
cd herdr-workflow
uv sync
uv run wq --help
```

Contributors must be able to run `uv sync && uv run pytest` **without a real herdr
installation.**

---

## Existing Configuration

`devcage-macos` remains responsible for installing herdr, configuring herdr, and
installing `wq`. It swaps the Bash script for the Python package — **after** cutover, not
before. No major herdr configuration changes required.

---

## Release Roadmap

### v0.1
Socket client, all twelve commands, `doctor`, behavior catalogue with tests, CI,
README. Bash retired.

### Later, if there is demand
Resume interrupted workflows, richer `list`/status output, PyPI, Homebrew, standalone
binaries, herdr plugin.

Deliberately unscheduled. They should not complicate the core.

---

## Success Criteria

- Every Bash command works identically in Python, verified end-to-end against a real
  herdr daemon.
- Every behavior in `docs/behaviors.md` has a named test against the fake server.
- Unit tests run with no herdr installed.
- `wq doctor` catches a protocol mismatch before a user hits it as a mystery failure.
- The `wq go` agent-pane guard is present and tested.
- Workflow logic has no socket dependency.
- No hard-coded personal paths remain.
- `devcage-macos` only installs and configures `wq`.
- A stranger can read `docs/behaviors.md` and learn something they did not know.
