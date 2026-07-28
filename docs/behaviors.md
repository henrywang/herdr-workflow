# What breaks when you drive coding-agent TUIs unattended

Every entry below was discovered by running the thing and watching it fail — usually
somewhere unhelpful, like a loop that hung for thirty minutes on work that finished in
two. None of it is guesswork, and none of it is style.

This file is the specification for the Python port. `wq` was Bash first, and the Bash
source is cited throughout as `wq:<line>`, referring to the implementation preserved at
[`devcage-macos/config/herdr/wq`](https://github.com/henrywang/devcage-macos). The point
of the rewrite is that each of these becomes a named test against a fake daemon instead of
a live experiment costing minutes and tokens.

**If you script Claude Code, Codex, or Amp, most of this applies to you whether or not you
use herdr.** The specifics are herdr's; the failure *classes* are not.

Each entry names the test that covers it. `(pending)` means the phase that needs it has
not landed yet.

---

## 1. A pane can report "ready" while it will silently eat your prompt

**Bash:** `wq:173-197` · **Test:** `tests/unit/test_delivery.py`

Claude Code asks *"Is this a project you created or one you trust?"* the first time it
runs in a directory — which is every pane `wq` starts, since the scratch dir and the build
worktree are both new.

herdr infers readiness from the prompt box. **The trust dialog's option list draws one.**
So the pane reports `interactive_ready: true` while the only input it will accept is an
answer to the dialog.

Send a prompt into that state and two things happen, both bad:

1. The prompt text is swallowed whole — no error, no trace.
2. **Its trailing Enter confirms the highlighted option.** You did not just lose a prompt;
   you answered a security question on the agent's behalf, whichever way it happened to be
   pointing.

**Handling:** read the visible screen before every prompt, look for the dialog, answer it
explicitly. Every directory `wq` starts an agent in is one it just created or one the
caller named on the command line, so the answer is yes.

**Do not try to replace this with events.** `events.subscribe` reports *agent* state. This
is a *screen* state. No event fires.

**Confirmed live, 2026-07-28**, against Claude Code (Sonnet 5) started in a fresh `/tmp`
directory with no `--permission-mode`. With the dialog on screen and nothing but a dialog
answer accepted:

```
AgentState(status='idle', seq=51, interactive_ready=True)
```

`idle` and `ready`. After `settle` answered it and the pane reached a real composer, the
state was **byte-for-byte the same** — same status, same `seq`, same `interactive_ready`.
Nothing herdr reports distinguishes "ready for a prompt" from "about to eat your prompt and
answer a security question with its Enter".

The screen is captured at `tests/fixtures/claude-trust-dialog.txt` and replayed by
`test_the_dialog_is_detected_in_a_real_captured_screen`. That matters more than it looks:
the other tests build a screen *containing* `TRUST_DIALOG` and then search for it, which
proves the loop works and nothing about whether the constant matches reality. A `settle`
that silently matches nothing is indistinguishable from a pane with no dialog — and sends
the prompt anyway.

---

## 2. `agent.prompt` returning OK does not mean the agent took the text

**Bash:** `wq:199-244` · **Test:** `tests/unit/test_delivery.py`

`agent.prompt` reports whether **herdr** accepted the request — not whether the **TUI**
accepted the text. A TUI mid-startup, or sitting on a dialog, drops it without a trace.

Worse: Claude Code keeps drawing a composer for **tens of seconds** after it answers the
trust dialog, while still discarding everything typed into it. Nothing in its status, its
title, or its screen distinguishes that pane from a working one.

**So delivery has to be confirmed, not predicted.** `state_change_seq` on `AgentInfo` is
the receipt: an agent that took a prompt leaves idle and the sequence moves. Nothing moved
after ten seconds means nothing was delivered — clear the composer with Esc and retry.

**The subtlety that costs you a second bug:** a turn that starts *late* looks exactly like
a dropped prompt until the last poll, which may land after you gave up. Compare the status
against the one **this attempt started from**, not against a fixed list. A warm pane sits
in `done` from its previous turn, so seeing `done` proves nothing.

`agent.prompt` accepts an inline `wait` option, and it is genuinely useful — but it does
not solve this. It waits on agent state, and the failure here is that the agent never
enters a state at all.

### `interactive_ready` cannot gate delivery

**[verified, herdr 0.7.5]** The field is **optional in the schema**, and it is not
reliably set — including across two runs of *the same command on the same kind of pane*:

| Observation | `interactive_ready` |
|-------------|--------------------|
| `pi` agent in a workspace root pane | absent ~2s, then `True` |
| `pi` agent in a fresh `wq chat` tab, run A | **never set** (60s of polling) |
| `pi` agent in a fresh `wq chat` tab, run B | `True` within 10s |

Run A still delivered its prompt and got its answer back, with `state_change_seq` moving
exactly as expected. So a pane can be entirely ready while herdr never says so — and
whether it says so is not a property of the pane you can predict.

**This makes it a warm-up hint, not a gate.** Two consequences:

1. **Never block on it for long.** The ceiling is 10s and paid in full whenever the hint
   does not arrive. The Bash implementation's `agent_ready()` instead treats it as a hard
   requirement and *dies* after 60 seconds with *"agent in pane X never became ready for
   input"* — on run A above, that path would have failed a `wq chat` that was working
   perfectly.
2. **Never build correctness on it.** `wait_ready` returns a boolean rather than raising,
   logs when the hint never came, and prompts anyway. The delivery receipt is the guard.

Our model types it `bool | None`, because *absent* and *false* are different claims.

### `agent.start` returning ok does not mean `agent.get` knows about the agent

**[verified]** Registration is asynchronous. For roughly the first second, `agent.get` on
a pane whose agent has just started answers as though the pane has no agent at all.

Treating the first `unknown` as fatal made `wq chat` fail instantly on a tab it had just
created — *"no agent in pane w28:p2"*. Found by running against a real daemon; the fake
never reproduced it because the fake answers immediately.

`unknown` means either "registration has not caught up" or "the agent is gone", and a
single sample cannot tell them apart. So they are distinguished **by time**: only fail once
a grace window has passed with no agent ever seen.

---

## 3. A freshly created pane is not at a shell prompt yet

**Bash:** `wq:103-129` · **Test:** `tests/unit/test_agents.py`

`pane.split` returns as soon as the pane exists. Starting an agent in it immediately fails
with `agent_pane_busy`. The gap is short but real.

**Handling:** retry on `agent_pane_busy` specifically, and only that code. Every other
error is a real failure and must surface immediately — a blanket retry turns a typo in a
model name into a fifteen-second hang followed by a confusing message.

---

## 4. Agent names are global, so concurrent workspaces collide

**Bash:** `wq:105-111` · **Test:** `tests/unit/test_agents.py`

herdr's agent names are global, not scoped to a workspace. A second concurrent `wq`
command starting its own `idea` or `review` pane fails with `agent_name_taken`.

**Handling:** register under a name qualified by the pane id, which is unique by
construction. herdr rejects anything outside `[a-z0-9_-]`, so lowercase it and replace the
separators. Keep the short name as the *pane label*.

**This is only safe because nothing looks agents up by name.** Panes are found by label
from the snapshot; agents are targeted by pane id. Reintroduce name-based lookup and
concurrency breaks again, in a way tests that run one workflow at a time will not catch.

---

## 5. `done` is not a state that persists

**Bash:** `wq:250-281` · **Test:** `tests/unit/test_loops.py`

`done` is where a finished turn lands, but it does not stay there: **a pane that has been
read settles back to `idle`.** Wait on `done` alone and you hang for the full turn timeout
— thirty minutes, by default — on work that finished minutes ago.

So you must also accept `idle`. But `idle` is ambiguous: it is *also* the state of a pane
that has not picked the prompt up yet.

**What resolves it:** behavior #2. Delivery has already been proven, so `idle` here means
finished rather than not-started. Confirm it holds across a short re-check, because a pane
can flash idle between steps of a single turn.

---

## 6. Creating one worktree can open two workspaces

**Bash:** `wq:531-553` · **Test:** `test_building.py::test_the_parent_workspace_is_found_and_recorded`

`worktree.create` opens **two** workspaces when the repo has no workspace open yet:

1. the linked worktree, labelled with your slug
2. the **parent checkout**, labelled with the repo name

**Only the first comes back in the response.** The second is invisible, and every build
leaks one that nothing knows how to close.

**Handling:** diff the workspace-id list around the call. The diff proves `wq` opened it;
a predicate says which of the new ones it is — `worktree.repo_root == repo` and
`is_linked_worktree == false`. Picking by timing alone would eventually record, and later
close, a workspace belonging to a concurrent command.

When the repo already had a workspace open, the diff is empty — and that workspace is the
user's, not `wq`'s to touch.

**Confirmed live, 2026-07-28.** A build in `/tmp/wq-p5/demo` opened `w2B` (label `demo`,
`is_linked_worktree: false`) alongside the `hello` worktree workspace it reported. `wq
clean hello` later closed it from `build.env`, which is the whole point of recording it.

**Resolve both sides of the path comparison.** herdr reported `repo_root` as
`/private/tmp/wq-p5/demo` where `git rev-parse --show-toplevel` said `/tmp/wq-p5/demo` —
the same directory, spelled two ways, because `/tmp` is a symlink on macOS. An unresolved
comparison matches nothing, silently, and only in the `/tmp` repositories you reach for
when validating.

---

## 7. `gh pr merge --delete-branch` fails *after* the merge lands

**Bash:** `wq:775-782` · **Test:** `test_shipping.py::test_merge_never_passes_delete_branch`

In a worktree checkout, `--delete-branch` makes `gh` clean up the local branch by first
switching the current checkout to the default branch. But `main` is already checked out in
the parent repo, so git refuses the second checkout.

`gh` then exits non-zero — **after the merge has already landed on GitHub.** A script with
`set -e`, or any equivalent, dies at that point, skipping every cleanup step that follows,
having already merged.

**Handling:** merge only. Delete both branches separately, afterwards, once the worktree
holding the local one is gone.

The general lesson: **everything after an irreversible step must be incapable of aborting
the function.** Cleanup after a landed merge is best-effort by definition.

---

## 8. Only close a workspace that is still what you left behind

**Bash:** `wq:76-94` · **Test:** `tests/unit/test_inbox_and_clean.py`

The parent workspace from behavior #6 must be cleaned up — but by the time cleanup runs, a
human may have adopted it.

**Two guards, both load-bearing:**

- **Never close the workspace the command is running in.** `wq go` started by hand from
  the repo's own tab lives in that pane, and closing it mid-cleanup is a self-destruct.
- **Only close it if it is still one tab, one pane, no agent.** Split, given a second tab,
  or given an agent, it belongs to someone now.

`close_parent_ws` has two callers — `go` and `clean` — and `clean` reads the id from
`build.env` line 4, because a workspace labelled with the repo is invisible to a
label-based search for the slug.

---

## 9. Terminal state is a heuristic. A file on disk is not.

**Bash:** `wq:299-304` · **Test:** `tests/unit/test_loops.py`

Every behavior above is an inference about what a TUI is doing from the outside. Each one
is right most of the time.

So when a turn is supposed to produce output, **confirm the output**: the file must exist,
be non-empty, and have an mtime newer than before the turn. The mtime matters — a plan
file left over from the previous round passes an existence check while proving nothing.

This is the backstop that makes the rest safe to get occasionally wrong.

---

## 10. Read the whole response; do not stream into a short-circuit

**Bash:** `wq:51-56` · **Test:** `tests/unit/test_client.py`

In Bash: `herdr status | grep -q` exits as soon as `grep` matches, herdr takes SIGPIPE, and
`pipefail` turns a healthy server into a failed check. Capture the output, then test it.

The Python port cannot reproduce that failure literally, but the underlying rule survives
and gets sharper: **the socket is a stream shared by responses and unsolicited events.** A
client that writes a request and reads exactly one line will eventually read an event
where it expected its answer.

That is why the client has a background reader, a pending-futures map keyed on request id,
and a separate event path — see
[protocol-framing.md](protocol-framing.md#events-arrive-on-the-same-connection-without-ids).

---

## 11. Your fake server is only as right as your reading of the protocol

**Test:** `tests/integration/test_live_daemon.py`

Not a herdr behavior — a *method* failure, and the most transferable thing here.

The client was built with a background reader, a pending-futures map, and id correlation,
because the documentation describes events arriving on the same connection. 27 unit tests
passed against a fake daemon built from the same reading.

Then it met a real daemon and failed on its second request. **herdr answers one request
per connection and then closes it.** `wq list` worked because it makes exactly one call;
`wq doctor` makes two, and the second died.

The fake could never have caught it, because the fake and the client were wrong the same
way. A test suite built entirely on your own model of a system tests your consistency,
not your correctness.

**What this changes:** every phase needs at least one test against the real daemon before
it counts as done, and the fake gets corrected the moment reality disagrees — the fake
now closes after each response, so a client that ever regresses to a long-lived
connection fails in unit tests too.

The related discovery, from the same session: `events.subscribe` names events **dotted**
(`workspace.created`) while `events.wait` names them **underscored**
(`workspace_created`). Both are in the schema, as separate definitions. Reading one and
assuming the other is a five-minute bug that looks like a twenty-minute one.

---

## 12. Two different structs in the schema share the name `WorktreeInfo`

**Test:** `test_building.py::_created_worktree` (the fake sends the real shape)

herdr's schema defines `WorktreeInfo` twice, in the same document:

| Where | Fields |
| --- | --- |
| `WorkspaceWorktreeInfo`, hanging off a workspace | `repo_key`, `repo_name`, `repo_root`, `checkout_path`, `is_linked_worktree` |
| `WorktreeInfo`, returned by `worktree.create` and `worktree.list` | `path`, `label`, `branch`, `is_bare`, `is_detached`, `is_prunable`, `is_linked_worktree`, `open_workspace_id` |

They share one field. `wq build` modelled the second as the first, and the first live run
died with `Object missing required field 'repo_key'`.

**Where it hurt:** the decode failed *after* `worktree.create` had already succeeded. The
git worktree existed, both workspaces were open, and `build.env` — the only record of the
invisible one — had not been written yet. The failure leaked exactly what behavior #6
exists to prevent.

**Handling:** wq never reads that field, so it is no longer modelled at all. msgspec
ignores unknown fields; a field you do not read can only ever cost you.

**The general rule: model what you read, and nothing else.** Every extra required field in
a wire struct is a decode failure waiting for a server that fills it in differently — and
decode happens after the request, which is to say after the side effects.

---

## 13. "No checks reported" reads like a CI failure and means the opposite

**Bash:** `wq:757-770` · **Test:** `test_shipping.py::test_no_checks_reported_is_waited_out_not_treated_as_failure`

GitHub registers a pull request's check runs a few seconds *after* the PR opens. In that
window `gh pr checks --watch` does not wait — it **exits immediately** with `no checks
reported`.

Two ways to get this wrong, and they fail in opposite directions:

- treat the non-zero exit as a CI failure → every fast `wq go` reports a failure that never
  happened
- treat `--watch` returning as "CI passed" → **merge a PR whose tests have not started**

**Handling:** poll plain `gh pr checks` until the output stops saying `no checks reported`,
*then* hand over to `--watch`. A repository with no CI at all never leaves that loop, so it
is bounded by `WQ_CI_APPEAR_TIMEOUT` and the timeout message says how to merge by hand.

This one generalises past GitHub: **"not started" and "finished with nothing" are the same
string in most status APIs.** Anything that waits on external CI needs to distinguish them
by time, not by a single reading — the same shape as rule of thumb #9.

---

## Rules of thumb

1. **Confirm effects, do not infer them.** Prompt delivery via `state_change_seq`; turn
   output via file mtime.
2. **A state you read is a state that may already have changed.** Re-confirm anything you
   are about to act on irreversibly.
3. **Retry on specific error codes, never on failure in general.**
4. **After an irreversible step, nothing may abort.**
5. **Screen state and agent state are different things.** Events cover the second only.
6. **Assume a concurrent copy of yourself is running.** Several of these bugs exist only
   because two commands overlapped.
7. **Verify against the real thing before believing your own test suite.** A fake built
   from your reading of the docs cannot disprove your reading of the docs.
8. **A signal that is sometimes absent cannot be a gate.** `interactive_ready` is a hint;
   `state_change_seq` is a receipt. Build on receipts.
9. **"Not there yet" and "never coming" look identical in one sample.** Distinguish them by
   time, not by a single read.
10. **Model only the fields you read.** Decoding happens after the request, so a struct
    that is stricter than it needs to be fails you after the side effects have landed.
