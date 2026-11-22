"""NAS shares and other mounts: an unmounted share is detected, never written into."""

from __future__ import annotations

import asyncio
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
    # and suggests a folder beside the downloads, on their drive
    assert d["suggested_path"] == str(other_drive / "Fetchbox" / "Movies")
    # clients on different drives: no single move fixes it, so no suggestion
    d_drive = tmp_path / "D-drive"
    d_drive.mkdir()
    real_sources = storage.link_sources

    async def two_drives() -> list[tuple[str, str]]:
        return [*await real_sources(), ("qBittorrent", str(d_drive))]

    monkeypatch.setattr(storage, "link_sources", two_drives)
    monkeypatch.setattr(storage_service, "_device", lambda folder: hash(folder))
    d = (await c.post(f"/destinations/{movies['id']}/check")).json()
    assert d["suggested_path"] is None and "different drives" in d["status_message"]
    monkeypatch.undo()
    monkeypatch.setattr(storage.local, "check_access", probe)
    health = (await c.get("/system/health")).json()
    assert any("are copied in full" in (x.get("message") or "") for x in health["checks"]), health

    # hardlink only: such downloads wait instead of being copied, and it says that
    r = await c.patch(f"/destinations/{movies['id']}", json={"link_mode": "hardlink_only"},
                      headers={"If-Match": d["etag"]})  # fmt: skip
    assert r.status_code == 200, r.text
    d = (await c.post(f"/destinations/{movies['id']}/check")).json()
    assert d["status"] == "degraded" and "can't be imported" in d["status_message"]


def test_windows_paths_under_wsl() -> None:
    from fetchbox.core.paths import host_path

    wsl = {"wsl": True}
    assert host_path(r"C:\Users\me\Media\Movies", **wsl) == "/mnt/c/Users/me/Media/Movies"
    assert host_path("D:/TV/", **wsl) == "/mnt/d/TV"
    assert host_path("C:\\", **wsl) == "/mnt/c"
    assert host_path(r"\\wsl.localhost\Debian\home\me\TV", **wsl) == "/home/me/TV"
    assert host_path(r"\\wsl$\Ubuntu\srv", **wsl) == "/srv"
    assert host_path("/home/me/TV", **wsl) == "/home/me/TV"
    assert host_path(r"C:\Media", wsl=False) == r"C:\Media"  # elsewhere: untouched


async def test_move_a_library_folder_with_its_files(
    c: httpx.AsyncClient, server: Running, tmp_path: Path
) -> None:
    from sqlalchemy import select

    from fetchbox.modules.library.models import MediaFile
    from fetchbox.modules.storage.models import StorageNode
    from tests.test_existing_library import _job

    movies = next(d for d in (await c.get("/destinations")).json() if d["media_kind"] == "movie")
    old = Path(movies["path"])
    rel = "Dune (2021)/Dune (2021).mkv"
    (old / rel).parent.mkdir(parents=True, exist_ok=True)
    (old / rel).write_bytes(b"x" * 5000)
    (old / "notes.txt").write_text("mine")  # not Fetchbox's: stays
    db = server.app.state.fb.db
    async with db.sessionmaker() as s:
        node = (await s.execute(select(StorageNode))).scalars().first()
        s.add(MediaFile(id="01TESTMOVEFILE000000000000", storage_node_id=node.id,
                        destination_id=movies["id"], relative_path=rel, size=5000,
                        quality={"name": "Bluray-1080p"}))  # fmt: skip
        await s.commit()

    r = await c.post(f"/destinations/{movies['id']}/move", json={"path": str(old / "inside")})
    assert r.status_code == 422  # not into itself

    new = tmp_path / "C-drive" / "Fetchbox" / "Movies"
    job = await _job(c, (await c.post(f"/destinations/{movies['id']}/move",
                                       json={"path": str(new)})).json()["id"])  # fmt: skip
    assert job["status"] == "succeeded", job
    assert job["result"]["moved"] == 1 and job["result"]["old_copies_removed"] == 1
    assert (new / rel).read_bytes() == b"x" * 5000
    assert not (old / rel).exists() and not (old / "Dune (2021)").exists()
    assert (old / "notes.txt").exists()
    d = next(x for x in (await c.get("/destinations")).json() if x["id"] == movies["id"])
    assert d["path"] == str(new)
    assert not list(new.rglob("*.fetchbox-partial"))


async def test_a_failed_move_changes_nothing(
    c: httpx.AsyncClient, server: Running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select

    from fetchbox.modules.library import relocate
    from fetchbox.modules.library.models import MediaFile
    from fetchbox.modules.storage.models import StorageNode

    movies = next(d for d in (await c.get("/destinations")).json() if d["media_kind"] == "movie")
    old = Path(movies["path"])
    rels = ["A (2001)/A (2001).mkv", "B (2002)/B (2002).mkv"]
    async with server.app.state.fb.db.sessionmaker() as s:
        node = (await s.execute(select(StorageNode))).scalars().first()
        for i, rel in enumerate(rels):
            (old / rel).parent.mkdir(parents=True, exist_ok=True)
            (old / rel).write_bytes(b"y" * 3000)
            s.add(MediaFile(id=f"01TESTFAILMOVE{i:012d}", storage_node_id=node.id,
                            destination_id=movies["id"], relative_path=rel, size=3000,
                            quality={"name": "Bluray-1080p"}))  # fmt: skip
        await s.commit()
    real = relocate._copy
    calls: list[Path] = []

    def flaky(src: Path, dst: Path) -> None:
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("disk full")  # the second file fails
        real(src, dst)

    monkeypatch.setattr(relocate, "_copy", flaky)
    new = tmp_path / "Elsewhere" / "Movies"
    jid = (await c.post(f"/destinations/{movies['id']}/move", json={"path": str(new)})).json()["id"]
    for _ in range(100):
        job = (await c.get(f"/jobs/{jid}")).json()
        if job["status"] in ("succeeded", "failed"):
            break
        await asyncio.sleep(0.1)
    assert job["status"] == "failed" and "nothing was changed" in (job["error"] or "")
    assert all((old / rel).is_file() for rel in rels)  # originals untouched
    assert not list(new.rglob("*.mkv"))  # the copy that worked was removed again
    d = next(x for x in (await c.get("/destinations")).json() if x["id"] == movies["id"])
    assert d["path"] == str(old)
