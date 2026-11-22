"""NAS shares and other mounts: an unmounted share is detected, never written into."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from fetchbox.modules.storage import service as storage_service
from fetchbox.modules.storage.service import mount_of
from tests.conftest import Running


async def test_unmounted_share_is_detected_and_left_alone(
    c: httpx.AsyncClient, server: Running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nas = tmp_path / "nas"
    folder = nas / "Movies"
    folder.mkdir(parents=True)
    mounted = {str(nas): True}
    real = os.path.ismount

    def fake_ismount(p: Any) -> bool:
        return mounted.get(str(p), real(p))

    monkeypatch.setattr(storage_service.os.path, "ismount", fake_ismount)
    r = await c.post("/destinations", json={"name": "NAS movies", "path": str(folder),
                                            "media_kind": "movie"})  # fmt: skip
    assert r.status_code == 201, r.text
    d = (await c.post(f"/destinations/{r.json()['id']}/check")).json()
    assert d["status"] == "online" and d["mount_point"] == str(nas)  # learned while mounted

    mounted[str(nas)] = False  # the NAS goes away; the empty mount folder is still there
    d = (await c.post(f"/destinations/{d['id']}/check")).json()
    assert d["status"] == "unmounted" and "isn't mounted" in d["status_message"]
    assert not list(folder.iterdir())  # no probe files written underneath
    health = (await c.get("/system/health")).json()
    assert health["status"] == "error"

    mounted[str(nas)] = True
    d = (await c.post(f"/destinations/{d['id']}/check")).json()
    assert d["status"] == "online"


def test_mount_of(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_service.os.path, "ismount",
                        lambda p: str(p) in (str(tmp_path), os.path.abspath(os.sep)))  # fmt: skip
    assert mount_of(tmp_path / "a" / "not-yet" / "deeper") == str(tmp_path)


async def test_recycle_retention(server: Running, settings: Any) -> None:
    storage = server.app.state.storage
    dest = await storage.default_destination("movie")
    bin_ = Path(dest.path) / ".fetchbox-recycle"
    for day in ("2020-01-01", "2999-01-01", "not-a-day"):
        (bin_ / day).mkdir(parents=True)
        (bin_ / day / "x.mkv").write_bytes(b"x")
    assert await storage.purge_recycle(7) == 1
    assert sorted(p.name for p in bin_.iterdir()) == ["2999-01-01", "not-a-day"]


async def test_mounts_listed(c: httpx.AsyncClient) -> None:
    r = await c.get("/storage/mounts")
    assert r.status_code == 200 and isinstance(r.json(), list)


async def test_copies_from_a_client_on_another_drive_are_flagged(
    c: httpx.AsyncClient, server: Running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client saving to another drive than the library means a full second copy of
    every download: the library folder says so, naming the client and its folder."""
    other_drive = tmp_path / "C-drive" / "Downloads"
    other_drive.mkdir(parents=True)
    storage = server.app.state.storage
    real = storage.local.check_access

    async def probe(path: str, *, link_source: str | None = None) -> Any:
        r = await real(path, link_source=link_source)
        if link_source and Path(link_source) == other_drive:
            r.hardlink_with = False  # what a second drive answers
        return r

    monkeypatch.setattr(storage.local, "check_access", probe)
    movies = next(d for d in (await c.get("/destinations")).json() if d["media_kind"] == "movie")
    assert (await c.post(f"/destinations/{movies['id']}/check")).json()["status"] == "online"

    builtin = next(x for x in (await c.get("/download-clients")).json() if x["kind"] == "builtin")
    r = await c.patch(f"/download-clients/{builtin['id']}",
                      json={"policy": {"save_path": str(other_drive)}})  # fmt: skip
    assert r.status_code == 200, r.text
    d = (await c.post(f"/destinations/{movies['id']}/check")).json()
    assert d["status"] == "degraded"
    assert "are copied in full" in d["status_message"] and str(other_drive) in d["status_message"]
    assert builtin["name"] in d["status_message"]
    health = (await c.get("/system/health")).json()
    assert any("are copied in full" in (x.get("message") or "") for x in health["checks"]), health

    # hardlink only: such downloads wait instead of being copied, and it says that
    r = await c.patch(f"/destinations/{movies['id']}", json={"link_mode": "hardlink_only"},
                      headers={"If-Match": d["etag"]})  # fmt: skip
    assert r.status_code == 200, r.text
    d = (await c.post(f"/destinations/{movies['id']}/check")).json()
    assert d["status"] == "degraded" and "can't be imported" in d["status_message"]
