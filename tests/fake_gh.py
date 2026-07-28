"""A fake `gh` on PATH.

A real executable rather than a patched wrapper function, for the same reason the fake
herdr speaks a real socket: mocking `git.gh.merge` would assert that wq calls its own
wrapper the way wq expects to call its own wrapper. A fake `gh` sees the **argv wq actually
builds**, which is what lets a test assert that `--delete-branch` is absent (behavior #7)
rather than trusting a comment saying it is.

It records every invocation to a log file, so tests can assert on the arguments after the
fact.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT = r"""#!/usr/bin/env python3
import json, os, sys, time

log = os.environ["FAKE_GH_LOG"]
script = json.load(open(os.environ["FAKE_GH_SCRIPT"]))

argv = sys.argv[1:]
with open(log, "a") as fh:
    fh.write(json.dumps(argv) + "\n")

# How many times this subcommand has been called so far, so a rule can answer
# differently on later calls -- which is how "no checks reported" then real checks
# is expressed.
seen = 0
for line in open(log):
    if json.loads(line)[:2] == argv[:2]:
        seen += 1

for rule in script:
    if argv[: len(rule["match"])] != rule["match"]:
        continue
    if "after" in rule and seen <= rule["after"]:
        continue
    sys.stdout.write(rule.get("out", ""))
    sys.exit(rule.get("code", 0))

sys.stdout.write("unscripted: " + " ".join(argv))
sys.exit(1)
"""


def _rules() -> list[dict[str, object]]:
    return []


@dataclass
class FakeGh:
    """Scriptable `gh`. Rules are matched in order against the leading argv."""

    directory: Path
    rules: list[dict[str, object]] = field(default_factory=_rules)

    @property
    def log(self) -> Path:
        return self.directory / "gh.log"

    @property
    def script(self) -> Path:
        return self.directory / "gh.script.json"

    def on(
        self,
        match: list[str],
        *,
        out: str = "",
        code: int = 0,
        after: int | None = None,
    ) -> FakeGh:
        """Answer `gh <match...>` with `out` and `code`.

        `after` makes the rule apply only from the Nth call of that subcommand onward,
        which is how "no checks reported, then real checks" is expressed.
        """
        rule: dict[str, object] = {"match": match, "out": out, "code": code}
        if after is not None:
            rule["after"] = after
        self.rules.append(rule)
        return self

    def install(self, monkeypatch: object) -> FakeGh:
        """Write the script and put it first on PATH."""
        self.directory.mkdir(parents=True, exist_ok=True)
        binary = self.directory / "gh"
        binary.write_text(SCRIPT)
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.script.write_text(json.dumps(self.rules))
        self.log.write_text("")

        # monkeypatch is typed loosely so this module stays importable without pytest in
        # the signature; every caller passes a real MonkeyPatch.
        setenv = monkeypatch.setenv  # type: ignore[attr-defined]
        setenv("PATH", f"{self.directory}{os.pathsep}{os.environ['PATH']}")
        setenv("FAKE_GH_LOG", str(self.log))
        setenv("FAKE_GH_SCRIPT", str(self.script))
        return self

    def calls(self) -> list[list[str]]:
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def calls_matching(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls() if c[: len(prefix)] == list(prefix)]


def working_gh(directory: Path, monkeypatch: object, *, pr: int = 7) -> FakeGh:
    """The happy path: PR opens, checks appear, CI passes, merge succeeds."""
    return (
        FakeGh(directory)
        .on(["pr", "create"], out=f"https://github.com/o/r/pull/{pr}\n")
        .on(["pr", "view"], out=f"{pr}\n")
        .on(["pr", "checks", str(pr), "--watch", "--fail-fast"], out="all checks passed\n")
        .on(["pr", "checks"], out="build\tpass\t1s\n")
        .on(["pr", "merge"], out=f"Merged pull request #{pr}\n")
        .install(monkeypatch)
    )
