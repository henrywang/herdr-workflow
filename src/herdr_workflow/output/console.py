"""Terminal output.

Matches the Bash implementation's shape deliberately: `==>` progress lines on stdout,
`wq: ` errors on stderr. The router reads this output, and people have muscle memory for
it, so it is not a free choice.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

_BLUE = "\033[1;34m"
_DIM = "\033[2m"
_RED = "\033[1;31m"
_RESET = "\033[0m"


def _color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return stream.isatty()


_verbose = False


def set_verbose(value: bool) -> None:
    global _verbose
    _verbose = value


def log(message: str) -> None:
    """A progress line. `==> doing the thing`."""
    prefix = f"{_BLUE}==>{_RESET}" if _color(sys.stdout) else "==>"
    print(f"{prefix} {message}")


def detail(message: str) -> None:
    """Extra context, shown only under --verbose."""
    if not _verbose:
        return
    if _color(sys.stdout):
        print(f"{_DIM}{message}{_RESET}")
    else:
        print(message)


def error(message: str, *, why: str | None = None, fix: str | None = None) -> None:
    """An error, with its remedy. Always stderr, so `wq list > file` stays clean."""
    prefix = f"{_RED}wq:{_RESET}" if _color(sys.stderr) else "wq:"
    print(f"{prefix} {message}", file=sys.stderr)
    if why:
        print(f"    why: {why}", file=sys.stderr)
    if fix:
        print(f"    fix: {fix}", file=sys.stderr)
