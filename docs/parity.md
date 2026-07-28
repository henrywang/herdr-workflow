# Bash/Python parity

The Bash implementation is the specification, and the CLI surface is a compatibility
contract — the router prompt in `devcage-macos` calls these commands by name and reads
their output. This file records how parity is checked and what has been checked so far.

Parity cannot be a unit test: it needs the Bash script, a real herdr daemon, and real
state. So it is a procedure, run at the end of each phase.

**Last run:** Phase 6, against herdr 0.7.5.

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

### `wq plan` — Phase 4

One live run, `claude:opus:high` planning against `pi:openai-codex/gpt-5.6-sol:high`
reviewing, cap of 2 rounds, in a throwaway scratch root:

```
==> round 1: drafting plan
==> round 1: review
==> round 2: revising plan
==> round 2: review
==> round limit (2) reached with findings outstanding
```

517s, exit 0 — matching Bash, which reserves a non-zero exit for `build`'s round limit,
not `plan`'s.

Artifacts confirmed:

- `request.md` written for the agents to read, never pasted into a prompt
- `plan.md` (325 lines) with all seven required sections present
- `review.md` with 3 BLOCKING and 4 NON-BLOCKING findings, ending in `VERDICT: CHANGES`,
  correctly read as *not* approved

The review was genuinely adversarial — among other things it flagged the planner for
appending a rebuttal to the previous review instead of implementation content. That is the
cross-model pairing doing what it exists to do, and it is not something a same-model
reviewer reliably produces.

Note the reviewer pane again never reported `interactive_ready` across two rounds, and both
prompts landed regardless. Behavior #2's decision to treat readiness as advisory has now
paid off on every phase that prompts.

### `wq build` — Phase 5

One live run against a throwaway repository in `/tmp`, `claude:sonnet:high` coding against
`pi:openai-codex/gpt-5.6-sol:high` reviewing, cap of 1 round:

```
==> creating worktree wq/hello
branching from origin/main
herdr also opened w2D for the parent checkout
==> implementing
==> round 1: code review
pane w2E:p2 never reported interactive_ready; prompting anyway
==> code approved in round 1
```

exit 0. Artifacts confirmed:

- `build.env`, five lines: repo, `wq/hello`, worktree path, `w2D`, `origin/main`
- `diff.patch` — the real committed change, both files, generated from
  `git diff origin/main...HEAD`
- `code-review.md` ending `VERDICT: APPROVED`, and honest about it: the reviewer noted it
  could not run `pytest` because pytest was not installed rather than claiming the tests
  passed
- one commit on `wq/hello`

**Behavior #6 confirmed end to end.** `worktree.create` reported one workspace and opened
two; the diff-and-predicate found the second; `build.env` recorded it; and `wq clean hello`
closed it afterwards — `closed workspace w2D (parent repo)`. The session went back to
exactly the workspaces it started with.

**The first attempt failed**, and it is worth recording why: a decode error on a field wq
never reads, *after* `worktree.create` had already created the worktree and both
workspaces. See behavior #12. The fake had been sending a shape the real server does not
send, so the unit tests agreed with the mistake — behavior #11, again, in a new costume.
The fake now sends the real payload.

`interactive_ready` was absent again, on the reviewer pane, and the prompt landed anyway.
That is four phases running.

### `wq revise` — Phase 6

Built `calc` first (80s, approved in round 1), then revised it — "also add a divide
function that raises ValueError on division by zero, with a test for that case":

```
==> revising
==> reviewing the change
pane w2G:p2 never reported interactive_ready; prompting anyway
==> approved
```

64s, exit 0. **The two ranges came out cleanly distinct**, which is the whole point of the
command:

| | added lines |
| --- | --- |
| `diff.patch` — three-dot, whole branch | `multiply`, `divide`, and all three tests |
| `revise.patch` — two-dot, this turn | `divide` and its two tests only |

Two commits on `wq/calc`, and `wq list` marked `calc` with `*` afterwards — the router
contract that makes `wq revise` default to the right slug.

Two guards checked live, both giving the remedy Bash gives:

```
wq: no build for nope
    fix: run: wq build nope <repo>

wq: worktree /private/tmp/wq-p6/demo-worktrees/calc is gone
    why: calc has already been shipped or cleaned
```

`wq clean calc` then closed both the build workspace and the recorded parent, leaving the
session exactly as it started. `interactive_ready` absent again, on the reviewer pane.

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

### The base ref is resolved, not hard-coded — intentional

Bash means `origin/main` in two places: the `--base` it cuts the branch from
(`wq:540`) and the diff range it reviews (`wq:567`, `wq:657`, and `ship`). That is wrong
for every repository that never renamed its default branch, and it is exactly the kind of
personal assumption a shared tool must not ship with.

Python asks the repository — `refs/remotes/origin/HEAD`, then `origin/main`, then
`origin/master` — and records the answer on **line five of `build.env`**, so the branch
point and every later diff agree on one commit.

**Parity holds where Bash worked.** `origin/main` is tried before `origin/master`, so any
repository Bash could build in resolves to the same ref.

**The cutover is safe in both directions.** Bash's reader takes the first four lines and
ignores the rest, so it reads a Python-written file without noticing line five. A
Bash-written file has no line five, and Python reads its absence as `origin/main` —
which is what Bash meant.

**Checked end to end, not assumed.** `build` resolves the base in the *parent checkout*;
`revise` reads it back and diffs in the *linked worktree*, a different directory that never
re-fetches. A linked worktree shares `.git` with its parent, so `origin/master` resolves
there too — `test_a_master_repo_can_be_built_and_then_revised` proves it, because a base
that failed to resolve would produce an empty delta and read as "the agent changed nothing"
rather than as an error.

The one divergence that remains: a build started by Python in a `master` repository and
finished by Bash would have its diff regenerated against `origin/main`. Bash never
supported those repositories at all, so this trades a command that could not work for one
that works unless you switch implementations mid-build.

---

## Not yet checked

Commands not yet ported: `brainstorm`, `ship`, `go`. Each gets a parity run as it lands.

`wq --help` will differ — Bash generates it by `sed`-ing its own header comment block,
Typer generates its own. See PLAN.md; the open question is whether the router depends on
the current format.
