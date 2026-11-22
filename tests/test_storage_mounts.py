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


def test_native_folders_only() -> None:
    """In WSL only WSL folders; on Windows only Windows folders."""
    from fetchbox.core.paths import native_folder_problem

    wsl = {"wsl": True, "windows": False}
    for path in (r"C:\Users\me\Media", "D:/TV", "/mnt/c/Users/me/Media", "/mnt/d", r"\\nas\media"):
        assert native_folder_problem(path, **wsl), path
    for path in ("/home/me/Media", "/srv/media", "/mnt/nas/media", "/mnt/media2"):
        assert native_folder_problem(path, **wsl) is None, path
    win = {"wsl": False, "windows": True}
    for path in (r"\\wsl.localhost\Debian\home\me", r"\\wsl$\Ubuntu\srv", "//wsl.localhost/x"):
        assert native_folder_problem(path, **win), path
    assert native_folder_problem(r"C:\Media", **win) is None
    assert native_folder_problem(r"\\nas\media", **win) is None
    # neither: anything goes (Linux, macOS)
    assert native_folder_problem("/mnt/c/x", wsl=False, windows=False) is None


async def test_crossing_folders_are_refused_in_wsl(
    c: httpx.AsyncClient, server: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fetchbox.core import paths

    monkeypatch.setattr(paths, "under_wsl", lambda: True)
    movies = next(d for d in (await c.get("/destinations")).json() if d["media_kind"] == "movie")
    r = await c.patch(f"/destinations/{movies['id']}", json={"path": "/mnt/c/Users/me/Movies"},
                      headers={"If-Match": movies["etag"]})  # fmt: skip
    assert r.status_code == 422 and "WSL folder" in r.text
    clients = (await c.get("/download-clients")).json()
    builtin = next(x for x in clients if x["kind"] == "builtin")
    r = await c.patch(f"/download-clients/{builtin['id']}",
                      json={"policy": {"save_path": "/home/me/torrents"}})  # fmt: skip
    assert r.status_code == 422 and "library folders" in r.text  # no separate folder at all
    # one already stored (from before): flagged, nothing goes there
    from sqlalchemy import update

    from fetchbox.modules.storage.models import Destination

    async with server.app.state.fb.db.sessionmaker() as s:
        await s.execute(update(Destination).where(Destination.id == movies["id"])
                        .values(path="/mnt/c/Users/me/Movies"))  # fmt: skip
        await s.commit()
    d = (await c.post(f"/destinations/{movies['id']}/check")).json()
    assert d["status"] == "offline" and "WSL folder" in d["status_message"]


async def test_fetchboxs_own_default_folders_on_the_other_system_move_while_empty(
    c: httpx.AsyncClient,
    server: Running,
    settings: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An earlier version under WSL made its default folders in the Windows Downloads
    folder. At startup an empty one moves to this system's default; one with files in it
    stays (flagged), since changing its path wouldn't bring the files along."""
    from sqlalchemy import update

    from fetchbox.modules.storage import service as storage_service
    from fetchbox.modules.storage.models import Destination

    other = tmp_path / "windows-side"  # stands in for /mnt/c/... under WSL
    monkeypatch.setattr(
        storage_service,
        "native_folder_problem",
        lambda p, **_: "the other system" if p.startswith(str(other)) else None,
    )
    dests = {d["name"]: d for d in (await c.get("/destinations")).json()}
    (other / "TV" / "Show").mkdir(parents=True)
    (other / "TV" / "Show" / "ep.mkv").write_bytes(b"x")  # someone's file: leave it
    async with server.app.state.fb.db.sessionmaker() as s:
        for name in ("Movies", "TV", "Music"):
            await s.execute(update(Destination).where(Destination.id == dests[name]["id"])
                            .values(path=str(other / name)))  # fmt: skip
        await s.commit()

    await server.app.state.storage.seed(None)  # what startup does
    now = {d["name"]: d["path"] for d in (await c.get("/destinations")).json()}
    assert now["Movies"] == str(settings.downloads_dir / "Movies")
    assert now["Music"] == str(settings.downloads_dir / "Music")
    assert now["TV"] == str(other / "TV")  # has a file in it: stays, for you to decide
