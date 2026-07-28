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

**Bash:** `wq:173-197` · **Test:** `(pending — Phase 4)`

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

---

## 2. `agent.prompt` returning OK does not mean the agent took the text

**Bash:** `wq:199-244` · **Test:** `(pending — Phase 4)`

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

---

## 3. A freshly created pane is not at a shell prompt yet

**Bash:** `wq:103-129` · **Test:** `(pending — Phase 2)`

`pane.split` returns as soon as the pane exists. Starting an agent in it immediately fails
with `agent_pane_busy`. The gap is short but real.

**Handling:** retry on `agent_pane_busy` specifically, and only that code. Every other
error is a real failure and must surface immediately — a blanket retry turns a typo in a
model name into a fifteen-second hang followed by a confusing message.

---

## 4. Agent names are global, so concurrent workspaces collide

**Bash:** `wq:105-111` · **Test:** `(pending — Phase 2)`

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

**Bash:** `wq:250-281` · **Test:** `(pending — Phase 4)`

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

**Bash:** `wq:531-553` · **Test:** `(pending — Phase 5)`

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

---

## 7. `gh pr merge --delete-branch` fails *after* the merge lands

**Bash:** `wq:775-782` · **Test:** `(pending — Phase 7)`

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

**Bash:** `wq:76-94` · **Test:** `(pending — Phase 2)`

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

**Bash:** `wq:299-304` · **Test:** `(pending — Phase 4)`

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
