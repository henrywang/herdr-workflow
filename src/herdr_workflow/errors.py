"""Error types.

Every error explains what failed, why, and how to fix it. The person reading the error is
usually mid-workflow and wants the next command, not only a diagnosis.
"""

from __future__ import annotations


class WqError(Exception):
    """Base for every error wq reports to the user.

    Carries a remedy alongside the message so the CLI can render both without the caller
    having to format them.
    """

    exit_code = 1

    def __init__(self, message: str, *, why: str | None = None, fix: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.why = why
        self.fix = fix


class ConfigError(WqError):
    """Configuration is missing, malformed, or contradictory."""


class HerdrError(WqError):
    """Something went wrong talking to herdr."""


class HerdrUnavailable(HerdrError):
    """The daemon is not running, or its socket is unreachable."""

    def __init__(self, message: str, *, why: str | None = None) -> None:
        super().__init__(message, why=why, fix="start it with: herdr")


class ProtocolError(HerdrError):
    """The daemon sent something we cannot parse, or violated the framing contract."""


class ApiError(HerdrError):
    """The daemon returned an error response.

    `code` is herdr's machine-readable error code. Several are load-bearing --
    `agent_pane_busy` and `agent_name_taken` drive retry logic rather than failure -- so
    it is kept as a field rather than folded into the message.
    """

    def __init__(self, code: str, message: str, *, method: str | None = None) -> None:
        self.code = code
        self.method = method
        detail = f"{method}: {message}" if method else message
        super().__init__(detail, why=f"herdr returned error code '{code}'")


class GitError(WqError):
    """A git command failed, or the repository is not in a state wq can work with.

    Carries git's own stderr as `why`. Nothing wq could synthesise beats it.
    """


class WorkflowError(WqError):
    """A workflow could not proceed."""
