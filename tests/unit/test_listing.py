from __future__ import annotations

from pathlib import Path
from typing import Any

from herdr_workflow.herdr.client import HerdrClient
from herdr_workflow.workflows import listing
from tests.fake_herdr import FakeHerdr


def _scratch(root: Path, slug: str, *, build: bool, diff_mtime: float | None = None) -> None:
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    if build:
        (d / "build.env").write_text("/repo\nwq/x\n/wt\nw9\n")
    if diff_mtime is not None:
        patch = d / "diff.patch"
        patch.write_text("diff")
        import os

        os.utime(patch, (diff_mtime, diff_mtime))


async def test_only_wq_workspaces_are_listed(
    client: HerdrClient, fake: FakeHerdr, snapshot_result: dict[str, Any], tmp_path: Path
) -> None:
    """A bare-slug workspace counts only when a build.env backs it.

    `beta` is wq's because tmp_path/beta/build.env exists. `some-repo` is a workspace the
    user opened by hand and must not appear.
    """
    root = tmp_path / "wq"
    _scratch(root, "beta", build=True, diff_mtime=1000)
    fake.on("session.snapshot", snapshot_result)

    result = await listing.collect(client, root)
    labels = {r.label for r in result.rows}
    assert labels == {"plan-alpha", "beta", "bs-gamma"}
    assert "some-repo" not in labels
    assert "inbox" not in labels


async def test_current_marker_follows_newest_diff_patch(
    client: HerdrClient, fake: FakeHerdr, snapshot_result: dict[str, Any], tmp_path: Path
) -> None:
    """The `*` marker is a router contract: `revise` defaults to the slug it marks."""
    root = tmp_path / "wq"
    _scratch(root, "beta", build=True, diff_mtime=1000)
    _scratch(root, "delta", build=True, diff_mtime=9000)

    snap = dict(snapshot_result)
    inner: dict[str, Any] = dict(snap["snapshot"])
    workspaces: list[Any] = list(inner["workspaces"])
    workspaces.append(
        {
            "workspace_id": "w6",
            "number": 6,
            "label": "delta",
            "focused": False,
            "pane_count": 1,
            "tab_count": 1,
            "active_tab_id": "w6:t1",
            "agent_status": "idle",
        }
    )
    inner["workspaces"] = workspaces
    snap["snapshot"] = inner
    fake.on("session.snapshot", snap)

    result = await listing.collect(client, root)
    assert result.current == "delta"
    assert "  * w6\tdelta" in listing.render(result)


async def test_render_includes_the_marker_legend_and_scratch_section(
    client: HerdrClient, fake: FakeHerdr, snapshot_result: dict[str, Any], tmp_path: Path
) -> None:
    root = tmp_path / "wq"
    _scratch(root, "beta", build=True, diff_mtime=1000)
    fake.on("session.snapshot", snapshot_result)

    text = listing.render(await listing.collect(client, root))
    assert text.startswith("live:")
    assert "* = most recently worked on" in text
    assert f"scratch ({root}):" in text
    assert "  beta" in text


async def test_no_builds_means_no_current_marker(
    client: HerdrClient, fake: FakeHerdr, snapshot_result: dict[str, Any], tmp_path: Path
) -> None:
    root = tmp_path / "wq"
    root.mkdir(parents=True)
    fake.on("session.snapshot", snapshot_result)

    result = await listing.collect(client, root)
    assert result.current is None
    assert "* = most recently worked on" not in listing.render(result)


async def test_missing_scratch_root_is_not_an_error(
    client: HerdrClient, fake: FakeHerdr, snapshot_result: dict[str, Any], tmp_path: Path
) -> None:
    """A fresh install has no scratch dir yet; `wq list` must still work."""
    fake.on("session.snapshot", snapshot_result)
    result = await listing.collect(client, tmp_path / "absent")
    assert result.scratch == []
    assert {r.label for r in result.rows} == {"plan-alpha", "bs-gamma"}
