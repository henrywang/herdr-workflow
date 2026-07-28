"""Where the herdr API socket lives.

Resolution order is herdr's own, documented at https://herdr.dev/docs/socket-api/ --
matching it means `wq` follows the user into a named session without being told.
"""

from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    return Path(os.environ.get("HERDR_CONFIG_DIR", Path.home() / ".config" / "herdr"))


def resolve(session: str | None = None, override: str | None = None) -> Path:
    """Resolve the API socket path.

    Order: explicit override (config or --socket) -> session argument ->
    HERDR_SOCKET_PATH -> HERDR_SESSION -> the default socket.

    Note this is `herdr.sock`, the API socket -- never `herdr-client.sock`, which speaks
    the binary TUI protocol and will not answer JSON. See docs/protocol-framing.md.
    """
    if override and override != "auto":
        return Path(override).expanduser()

    if session:
        return config_dir() / "sessions" / session / "herdr.sock"

    env_path = os.environ.get("HERDR_SOCKET_PATH")
    if env_path:
        return Path(env_path).expanduser()

    env_session = os.environ.get("HERDR_SESSION")
    if env_session:
        return config_dir() / "sessions" / env_session / "herdr.sock"

    return config_dir() / "herdr.sock"
