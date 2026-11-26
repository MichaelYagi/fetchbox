"""macOS ._ files ("AppleDouble", written beside files on a NAS): never taken for media,
and removed from library folders."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from fetchbox.core import macfiles
from fetchbox.modules.library.music_files import is_audio
from fetchbox.modules.library.service import is_video, pick_video
from fetchbox.providers.download_clients import ClientFile
from tests.conftest import Running

EP = "Show - S03E07 - Episode 7 [WEBDL-1080p].mkv"


def test_they_arent_media() -> None:
    assert is_video(EP) and not is_video(f"._{EP}")
    assert not is_video(f"Season 3/._{EP}")
    assert is_audio("01 - Song.flac") and not is_audio("Album/._01 - Song.flac")
    files = [ClientFile(path=f"Show/._{EP}", size=10**10, progress=1.0),
             ClientFile(path=f"Show/{EP}", size=10**9, progress=1.0)]  # fmt: skip
    picked = pick_video(files)
    assert picked is not None and picked.path == f"Show/{EP}"


def _junk(tmp_path: Path) -> Path:
    show = tmp_path / "TV" / "Show" / "Season 3"
    show.mkdir(parents=True)
    (show / EP).write_bytes(b"video")
    (show / f"._{EP}").write_bytes(b"meta")  # beside its file
    (show / "._Gone.mkv").write_bytes(b"meta")  # its file is gone
    (show.parent / "._Season 3").write_bytes(b"meta")  # beside a folder: kept
    return show


def test_sweep(tmp_path: Path) -> None:
    show = _junk(tmp_path)
    removed = macfiles.sweep(tmp_path / "TV")
    assert sorted(Path(p).name for p in removed) == sorted([f"._{EP}", "._Gone.mkv"])
    assert (show / EP).read_bytes() == b"video"
    assert (show.parent / "._Season 3").exists()


def test_tidy_after_placing(tmp_path: Path) -> None:
    f = tmp_path / EP
    f.write_bytes(b"video")
    (tmp_path / f"._{EP}").write_bytes(b"meta")
    macfiles.tidy(f)
    assert f.exists() and not (tmp_path / f"._{EP}").exists()


async def test_library_folders_are_swept(
    client: httpx.AsyncClient, server: Running, tmp_path: Path
) -> None:
    storage = server.app.state.storage
    from fetchbox.modules.storage.service import MAC_FILES_JOB

    # the sweep at startup first (it runs on its own): it would otherwise take these files
    for _ in range(200):
        jobs = (await client.get("/jobs", params={"limit": 100})).json()
        rows = jobs.get("items", jobs) if isinstance(jobs, dict) else jobs
        if any(j["type"] == MAC_FILES_JOB and j["status"] in ("succeeded", "failed") for j in rows):
            break
        await asyncio.sleep(0.05)
    tv = next(d for d in await storage.destinations() if d.media_kind == "series")
    show = _junk(Path(tv.path).parent)
    assert Path(tv.path) == show.parent.parent  # TV/Show/Season 3
    assert sorted(Path(p).name for p in await storage.sweep_mac_files()) == [
        *sorted([f"._{EP}", "._Gone.mkv"])]  # fmt: skip

    # and at once when an import changes a folder
    (show / f"._{EP}").write_bytes(b"meta")
    await storage.bus.emit("import.completed", folders=[str(show)])
    for _ in range(50):
        if not (show / f"._{EP}").exists():
            break
        await asyncio.sleep(0.05)
    assert not (show / f"._{EP}").exists() and (show / EP).exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="extended attributes as a Mac keeps them")
def test_tidy_clears_what_macos_would_keep_in_a_dot_underscore_file(tmp_path: Path) -> None:
    f = tmp_path / EP
    f.write_bytes(b"video")
    subprocess.run(["xattr", "-w", "com.apple.quarantine", "0081;0;Fetchbox;", str(f)], check=True)
    subprocess.run(["xattr", "-w", "org.example.note", "x", str(f)], check=True)
    macfiles.tidy(f)
    left = subprocess.run(["xattr", str(f)], capture_output=True, text=True, check=True).stdout
    assert left.strip() == "" and f.read_bytes() == b"video"
