# Bash/Python parity

The Bash implementation is the specification, and the CLI surface is a compatibility
contract — the router prompt in `devcage-macos` calls these commands by name and reads
their output. This file records how parity is checked and what has been checked so far.

Parity cannot be a unit test: it needs the Bash script, a real herdr daemon, and real
state. So it is a procedure, run at the end of each phase.

**Last run:** Phase 3, against herdr 0.7.5.

---

## Procedure

Run both implementations against the same state and diff. Use `WQ_ROOT` and
`WQ_INBOX_LABEL` to work in a throwaway scratch root and a throwaway inbox, so a parity
run never touches the real one.

```bash
export WQ_ROOT=/tmp/wq-difftest
diff <(~/bin/wq list) <(uv run wq list)
```

Build interesting state first — an empty listing agrees trivially and proves little. The
cases that matter:

- a `plan-` workspace, a `bs-` workspace, and a bare-slug workspace backed by `build.env`
- **a decoy workspace with no `build.env`**, which must *not* appear
- two builds with different `diff.patch` mtimes, to place the `*` marker
- a scratch dir with no workspace, which must appear under `scratch` but not `live`

---

## Checked in Phase 1–2

### `wq list` — byte-identical

Empty state and populated state both diff clean, including:

```
live:
    w10	plan-alpha	unknown
    w21	bs-alpha	unknown
    w22	alpha	unknown
  * w23	beta	unknown

  * = most recently worked on

scratch (/tmp/wq-difftest):
  alpha
  beta
  orphan
```

- the `*` marker on the newest `diff.patch` — **the router contract**: its prompt says
  *"`revise` defaults to the slug marked `*` in `wq list`"*
- tab-separated columns and the two-space/four-space indent
- the legend line, present only when there is a marked build
- `decoy-repo` correctly absent — a bare-slug workspace is only wq's when a `build.env`
  backs it
- `orphan` under `scratch` but not `live`

### `wq clean` — equivalent effects

`uv run wq clean alpha` and `~/bin/wq clean beta` each closed their workspaces
(`plan-`, `bs-`, bare slug) and removed the scratch dir, leaving the decoy workspace and
the unrelated scratch dir untouched.

### `wq up` — both directions of cutover

This is the case that matters during the cutover, when both implementations are installed:

| Created by | Read by | Result |
|-----------|---------|--------|
| Bash | Python | `router already running` — no duplicate workspace, no second agent |
| Python | Bash | `router already running` — same |

Agent naming also matches. Bash produced `router-w25-p1`; Python produced
`router-w26-p1` from pane `w26:p1`. Same pane-id-qualified, lowercased convention, so
neither implementation collides with the other's agents.

---

### `wq chat`, `wq ask`, `wq tidy` — Phase 3

Run live against a throwaway inbox (`WQ_INBOX_LABEL=wq-p3`), with a real `pi` agent:

- `chat` created the tab, delivered the prompt, and the agent answered `CHAT-OK`
- a second `chat` reused the same tab (`created_tab: false`) — no second agent
- `ask` created a timestamped `ask-033837` tab scoped to the cwd
- `tidy` closed the finished ask tab and left `router` and `chat` untouched

Timing matters here, because `go.md` documents `chat` and `ask` as returning promptly:
**6s into a fresh tab, 4s into a warm one.** An earlier build waited the full 60s
readiness ceiling on a fresh tab — see behavior #2 on why that ceiling is now 10s.

Not directly diffable against Bash: both are fire-and-forget, so there is no output to
compare beyond the `tab <label> — close it with: wq tidy` line, which matches.

---

## Known deviations

### ANSI colour on non-TTY output — intentional

Bash's `log()` emits `\033[1;34m==>\033[0m` unconditionally. Python emits colour only when
stdout is a TTY, and honours `NO_COLOR`.

Kept deliberately. In a pane both are identical; when output is captured — by the router,
or a pipe — Python's is clean where Bash's carries escape codes. No consumer depends on
the escapes.

If byte-identical captured output is ever needed, `output/console.py` is the one place to
change.

---

## Not yet checked

Commands not yet ported: `brainstorm`, `plan`, `build`, `revise`, `ship`, `go`. Each gets
a parity run as it lands.

`wq --help` will differ — Bash generates it by `sed`-ing its own header comment block,
Typer generates its own. See PLAN.md; the open question is whether the router depends on
the current format.
