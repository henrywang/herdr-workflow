# Changelog

## Unreleased

First working version. All 13 commands have been driven end to end against a real herdr
daemon and real agents.

### Commands

`up`, `chat`, `ask`, `tidy`, `brainstorm`, `plan`, `build`, `revise`, `ship`, `go`,
`list`, `clean`, and `doctor`.

### Behaviour worth knowing about

- **The base branch is resolved, never assumed.** `wq build` asks the repository what to
  branch from — `origin/HEAD`, then `origin/main`, then `origin/master` — and records the
  answer, so the branch point, every diff, and the pull request all agree on one commit.
  Repositories that never renamed their default branch work without configuration.
- **`wq brainstorm` has no default note directory**, on purpose: set `WQ_VAULT` or
  `[paths] notes`. Nothing here guesses where you keep your notes.
- **Exit codes are part of the interface.** `build` exits **2** when it hits its round cap
  with findings outstanding — unreviewed code is on a branch — where `revise` exits **0**,
  because findings are what you asked it for. Both are distinct from **1**, a real failure.
- **`wq go` refuses to run in a pane with an agent in it.** It pushes, merges and deletes
  branches, and its cleanup closes the workspace it would be running in. Use `wq ship`,
  which puts it in a plain shell tab.
- **`gh pr merge` is run without `--delete-branch`**, deliberately: in a worktree checkout
  that flag makes `gh` fail *after* the merge has landed. Both branches are deleted
  afterwards instead.
- Colour only on a TTY, and `NO_COLOR` is honoured.

### The reliability layer

Driving agent TUIs unattended fails in specific, non-obvious ways: a pane reports "ready"
while it will silently swallow your prompt, a successful `agent.prompt` does not mean the
agent took the text, `gh pr checks` says "no checks reported" before CI starts.

Thirteen of these are catalogued in [docs/behaviors.md](docs/behaviors.md), each with the
test that pins it — 267 tests, ~30 seconds, no herdr installation required. That catalogue
is the most portable thing in this project: if you script Claude Code, Codex, or Amp, most
of it applies to you whether or not you use herdr.
