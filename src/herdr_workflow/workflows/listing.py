"""`wq list` -- what is running.

`bs-` and `plan-` workspaces announce themselves by label. A build workspace is labelled
with the bare slug, which is indistinguishable from a workspace opened by hand -- so it
counts as one of wq's only when a scratch dir with a build.env backs it. The filesystem is
the index, as everywhere else here.

The two sections mean different things and the router relies on the difference: a slug in
`live` can be revised, a slug that appears only under `scratch` has no panes left and has
to be rebuilt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from herdr_workflow.herdr.client import HerdrClient

_MANAGED_LABEL = re.compile(r"^(bs|plan)-")


@dataclass(frozen=True)
class Row:
    workspace_id: str
    label: str
    agent_status: str
    is_build: bool
    current: bool


@dataclass(frozen=True)
class Listing:
    rows: list[Row]
    scratch: list[str]
    root: Path

    @property
    def current(self) -> str | None:
        return next((r.label for r in self.rows if r.current), None)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def scan_scratch(root: Path) -> tuple[set[str], list[str]]:
    """One pass over the scratch root, returning (build slugs, every entry).

    Read once rather than twice: `builds` and `scratch` come from the same directory, and
    scanning it twice lets a concurrent `wq clean` land between them and produce a listing
    that contradicts itself.
    """
    if not root.is_dir():
        return set(), []
    entries = sorted(root.iterdir(), key=lambda p: p.name)
    builds = {d.name for d in entries if d.is_dir() and (d / "build.env").is_file()}
    return builds, [p.name for p in entries]


async def collect(client: HerdrClient, root: Path) -> Listing:
    snapshot = await client.snapshot()
    builds, scratch = scan_scratch(root)

    candidates = [
        ws for ws in snapshot.workspaces if _MANAGED_LABEL.match(ws.label) or ws.label in builds
    ]

    # `build` and every `revise` rewrite diff.patch, so its mtime already records which
    # build was worked on last. Marking it lets the router revise the obvious one without
    # asking, and there is no index to keep in sync -- the answer is read back out of
    # state that had to exist anyway.
    #
    # Picked from the rows rather than from the scratch dirs: a build that has been
    # shipped has a newer diff.patch than anything still open, and no panes left to
    # revise.
    current: str | None = None
    newest = 0.0
    for ws in candidates:
        if ws.label not in builds:
            continue
        stamp = _mtime(root / ws.label / "diff.patch")
        if stamp > newest:
            newest = stamp
            current = ws.label

    rows = [
        Row(
            workspace_id=ws.workspace_id,
            label=ws.label,
            agent_status=ws.agent_status,
            is_build=ws.label in builds,
            current=ws.label == current,
        )
        for ws in candidates
    ]

    return Listing(rows=rows, scratch=scratch, root=root)


def render(listing: Listing) -> str:
    """Render the plain-text listing.

    The `*` marker is a contract, not decoration: the router prompt says "`revise`
    defaults to the slug marked `*` in `wq list`". Changing this format breaks the router
    silently.
    """
    lines = ["live:"]
    for row in listing.rows:
        marker = "  * " if row.current else "    "
        lines.append(f"{marker}{row.workspace_id}\t{row.label}\t{row.agent_status}")
    if listing.current:
        lines.append("")
        lines.append("  * = most recently worked on")

    lines.append("")
    lines.append(f"scratch ({listing.root}):")
    lines.extend(f"  {name}" for name in listing.scratch)
    return "\n".join(lines)
