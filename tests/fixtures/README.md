# Captured screens

Real `herdr agent read --source visible` output, captured from live agents. These are
what the unit tests replay, so a test asserting that `wq` detects a dialog is asserting it
against the thing an agent actually draws — not against a string the test wrote itself.

That distinction is behavior #11: a fake built from your own reading of a system cannot
disprove your reading. A synthetic screen containing `TRUST_DIALOG` proves the *loop*
works and nothing about whether the constant matches reality.

| File | Captured | From |
| --- | --- | --- |
| `claude-trust-dialog.txt` | 2026-07-28 | Claude Code (Sonnet 5) starting in a fresh `/tmp` directory, no `--permission-mode` |

**Recapture when an agent CLI changes its wording.** The way to do it is the way these
were made: start the agent in a directory it has never seen, and read the pane before
touching anything.
