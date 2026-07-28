"""Wire types for the herdr socket API.

These types intentionally cover only the methods and fields wq reads. See
`docs/protocol-framing.md` for the transport contract.

Field names match the wire exactly, and required/optional follows the shipped schema
rather than guesswork. msgspec ignores unknown fields, so a herdr release that adds a
field will not break decoding; one that adds a *required* field we model as required
will, which is what `wq doctor`'s protocol check exists to catch first.
"""

from __future__ import annotations

from typing import Any

import msgspec

# The protocol version this package is written against. `wq doctor` compares the running
# server's `ping.protocol` against this and says so plainly when they diverge -- a shared
# tool against a young API will break for strangers in ways it never breaks for us.
PINNED_PROTOCOL = 17

# herdr's agent status vocabulary. `done` does not persist: a pane that has been read
# settles back to `idle`. See behavior #5 -- this enum is the reason that bug exists.
AGENT_STATUSES = ("idle", "working", "blocked", "done", "unknown")


class ErrorBody(msgspec.Struct):
    code: str
    message: str


class Envelope(msgspec.Struct):
    """One line off the socket, before we know what it is.

    Three shapes share the stream:
      - success:  {"id": ..., "result": {...}}
      - error:    {"id": ..., "error": {"code": ..., "message": ...}}
      - event:    {"event": ..., "data": {...}}   -- no id

    `result` and `data` stay as Raw so the read loop can hand them to the waiting caller
    without knowing which of the 89 result types it is holding.

    They default to an empty Raw rather than being `Raw | None`: msgspec will not decode
    an optional Raw -- `Raw | None` fails with "Expected `null`, got `object`" on every
    real response. Absence is therefore an empty Raw, which `has_result` reads.
    """

    id: str | None = None
    result: msgspec.Raw = msgspec.Raw()
    error: ErrorBody | None = None
    event: str | None = None
    data: msgspec.Raw = msgspec.Raw()

    @property
    def has_result(self) -> bool:
        return len(bytes(self.result)) > 0

    @property
    def has_data(self) -> bool:
        return len(bytes(self.data)) > 0


class Pong(msgspec.Struct):
    """Result of `ping`: {"type": "pong", version, protocol, capabilities?}."""

    type: str
    version: str
    protocol: int
    capabilities: dict[str, Any] | None = None


class WorkspaceWorktreeInfo(msgspec.Struct):
    """A workspace's git worktree, when it has one.

    `is_linked_worktree` distinguishes the worktree wq created from the parent checkout
    that `worktree create` opens alongside it -- see behavior #6, where telling them
    apart is the difference between cleaning up and leaking a workspace.

    **Named for the schema, not for the concept.** herdr's schema has two different
    structs both called `WorktreeInfo`: this one, hanging off a workspace, and another --
    `path`, `label`, `is_bare`, `is_prunable` -- returned by `worktree.create` and
    `worktree.list`. They share a name and share almost no fields. Modelling one as the
    other decodes cleanly right up until a live server sends the other.
    """

    repo_key: str
    repo_name: str
    repo_root: str
    checkout_path: str
    is_linked_worktree: bool


class Workspace(msgspec.Struct):
    workspace_id: str
    number: int
    label: str
    focused: bool
    pane_count: int
    tab_count: int
    active_tab_id: str
    agent_status: str
    worktree: WorkspaceWorktreeInfo | None = None
    tokens: dict[str, str] = {}


class Tab(msgspec.Struct):
    tab_id: str
    workspace_id: str
    number: int
    label: str
    focused: bool
    pane_count: int
    agent_status: str


class Pane(msgspec.Struct):
    pane_id: str
    terminal_id: str
    workspace_id: str
    tab_id: str
    focused: bool
    agent_status: str
    revision: int
    label: str | None = None
    title: str | None = None
    cwd: str | None = None
    agent: str | None = None


class Agent(msgspec.Struct):
    """An agent registered against a pane.

    Two fields here carry the whole reliability story:

    `interactive_ready` -- herdr reads this off the prompt box. A trust dialog draws one,
    so this reports true while the only input the pane accepts is a dialog answer
    (behavior #1). Necessary but not sufficient.

    **It is `None` more often than you would expect.** The schema marks it optional, and
    herdr 0.7.5 omits it entirely from both `agent.list` and `agent.get` for a freshly
    started pi agent. So it is modelled as `bool | None`: absent and false are different
    claims, and treating a missing field as "not ready" would wait forever. Readiness
    cannot be built on this field alone -- see behavior #2.

    `state_change_seq` -- the receipt that a prompt was actually taken. `agent.prompt`
    returning ok means herdr accepted the request, not that the TUI took the text
    (behavior #2). This number moving is the only proof. Verified present and non-zero on
    a live agent.
    """

    pane_id: str
    terminal_id: str
    workspace_id: str
    tab_id: str
    focused: bool
    agent_status: str
    revision: int
    interactive_ready: bool | None = None
    launch_pending: bool = False
    state_change_seq: int = 0
    name: str | None = None
    agent: str | None = None
    label: str | None = None
    cwd: str | None = None


class Snapshot(msgspec.Struct):
    """`session.snapshot`'s payload.

    Carries `protocol` and `version` too, so a snapshot doubles as a version check when
    we already need one for another reason.
    """

    workspaces: list[Workspace] = []
    tabs: list[Tab] = []
    panes: list[Pane] = []
    agents: list[Agent] = []
    protocol: int = 0
    version: str = ""
    focused_workspace_id: str | None = None
    focused_tab_id: str | None = None
    focused_pane_id: str | None = None


class SnapshotResult(msgspec.Struct):
    """Result of `session.snapshot`: {"type": "session_snapshot", "snapshot": {...}}."""

    snapshot: Snapshot
    type: str = "session_snapshot"


class WorkspaceCreated(msgspec.Struct):
    """Result of `workspace.create`.

    **Not `worktree.create`** -- that has its own result type and its own `worktree` field
    of a shape this struct knows nothing about. See `WorktreeCreated` and behavior #12.
    """

    workspace: Workspace
    root_pane: Pane
    tab: Tab | None = None
    type: str = "workspace_created"


class WorktreeCreated(msgspec.Struct):
    """Result of `worktree.create`.

    Its own result type, not `workspace_created`: the wire `type` differs and it carries an
    extra `worktree`. The rest of the shape matches, so `root_pane` is available directly.

    **`worktree` is deliberately not modelled.** wq never reads it, msgspec ignores
    unknown fields, and modelling a field you do not use can only ever fail -- which is
    exactly what it did: the first live run decoded `worktree` against the *workspace's*
    worktree struct, failed on a missing `repo_key`, and left behind the worktree and both
    workspaces it had just created. A decode error after the irreversible work is the
    worst place to have one. See behavior #12.

    What this does **not** report is the second workspace. When the repository had no
    workspace open, herdr opens one for the parent checkout as well and mentions it
    nowhere -- behavior #6. Only diffing the workspace list around the call reveals it.
    """

    workspace: Workspace
    root_pane: Pane
    tab: Tab | None = None
    type: str = "worktree_created"


class TabCreated(msgspec.Struct):
    """Result of `tab.create`."""

    root_pane: Pane
    tab: Tab | None = None
    type: str = "tab_created"


class PaneResult(msgspec.Struct):
    """Result of `pane.get` and `pane.split`."""

    pane: Pane
    type: str = "pane_info"


class AgentListResult(msgspec.Struct):
    """Result of `agent.list`."""

    agents: list[Agent] = []
    type: str = "agent_list"


class AgentResult(msgspec.Struct):
    """Result of `agent.get`, `agent.prompt` and `agent.wait`.

    **The `agent` returned by `agent.prompt` carries the state from *before* the prompt.**
    Verified live: seq was 4 before, `agent.prompt` returned 4, and only a subsequent poll
    saw 5. So this response cannot be used as the delivery receipt -- see behavior #2.
    """

    agent: Agent
    type: str = "agent_info"


class ReadPayload(msgspec.Struct):
    pane_id: str
    text: str
    source: str = "visible"
    format: str = "text"


class AgentReadResult(msgspec.Struct):
    """Result of `agent.read`: {"type": "agent_read", "read": {..., "text": ...}}."""

    read: ReadPayload
    type: str = "agent_read"
