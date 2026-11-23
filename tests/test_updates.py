"""Update check: a newer release is offered; off means no contact at all."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from fetchbox import updater
from fetchbox.modules.system.updates import UpdateChecker, newer
from tests.conftest import Running


def test_no_releases_yet() -> None:
    from fetchbox.modules.system.updates import newest

    assert newest([]) is None and newest([{"tag_name": "v1.0.0", "draft": True}]) is None


def test_version_comparison() -> None:
    assert newer("v0.2.0", "0.1.0") and newer("v0.1.1", "0.1.0.dev0")
    assert not newer("v0.1.0", "0.1.0.dev0")  # a dev build of 0.1.0 isn't offered 0.1.0
    assert not newer("v0.1.0", "0.2.0") and not newer("nightly", "0.1.0")


async def test_updates_endpoint(server: Running) -> None:
    calls: list[str] = []

    def github(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        page = "https://github.com/MichaelYagi/fetchbox/releases/tag/v9.0.0"
        dl = "https://github.com/MichaelYagi/fetchbox/releases/download/v9.0.0/"
        assets = [
            {"name": n, "browser_download_url": dl + n, "size": 1}
            for n in ("Fetchbox-Windows-10-11-v9.0.0-Setup.exe", "SHA256SUMS-desktop.txt")
        ]
        body = {"tag_name": "v9.0.0", "published_at": "2027-01-01", "html_url": page,
                "assets": assets, "prerelease": True}  # fmt: skip
        # newest first as GitHub lists them, but the highest version wins, pre-release or not;
        # a draft is never offered
        return httpx.Response(200, json=[
            {"tag_name": "v10.0.0", "draft": True, "assets": []},
            {"tag_name": "v8.9.9", "html_url": "x", "assets": []},
            body,
            {"tag_name": "nightly", "assets": []},
        ])  # fmt: skip

    server.app.state.updates = UpdateChecker(httpx.MockTransport(github))
    async with server.client() as c:
        r = (await c.get("/system/updates")).json()
        assert r["update_available"] and r["latest"] == "v9.0.0" and r["checked"]
        assert [d["name"] for d in r["downloads"]][1] == "SHA256SUMS-desktop.txt"
        await c.get("/system/updates")
        assert len(calls) == 1  # cached
        assert (
            await c.patch("/system/config", json={"updates": {"check": False}})
        ).status_code == 200
        r = (await c.get("/system/updates", params={"fresh": True})).json()
        assert not r["checked"] and not r["update_available"] and len(calls) == 1  # no contact


def test_pick_the_installer_for_this_computer() -> None:
    names = ["Fetchbox-Windows-10-11-v9.0.0-Setup.exe", "Fetchbox-Windows-10-11-v9.0.0.zip",
             "Fetchbox-macOS-Silicon-v9.0.0.dmg", "Fetchbox-macOS-Intel-v9.0.0.dmg",
             "Fetchbox-Linux-x86_64-v9.0.0.deb", "SHA256SUMS-desktop.txt"]  # fmt: skip
    files = [updater.Download(n, f"https://x/{n}") for n in names]

    def chosen(system: str, machine: str = "x86_64") -> str | None:
        got = updater.pick(files, system=system, machine=machine)
        return got[0].name if got else None

    assert chosen("win32", "AMD64") == names[0]
    assert chosen("darwin", "arm64") == names[2]
    assert chosen("darwin", "x86_64") == names[3]
    assert chosen("linux") is None  # apt, the AppImage or Docker
    assert updater.pick(files[:-1], system="win32") is None  # no checksums: no install


def test_download_is_checked_against_the_release(tmp_path: Path) -> None:
    good = b"installer bytes" * 1000
    name = "Fetchbox-Windows-10-11-v9.0.0-Setup.exe"
    served = {"installer": good}

    def gh(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("SHA256SUMS-desktop.txt"):
            line = f"{hashlib.sha256(good).hexdigest()}  {name}\n"
            return httpx.Response(200, text="0" * 64 + "  other.dmg\n" + line)
        return httpx.Response(200, content=served["installer"])

    inst = updater.Download(name, "https://x/" + name)
    sums = updater.Download("SHA256SUMS-desktop.txt", "https://x/SHA256SUMS-desktop.txt")
    t = httpx.MockTransport(gh)
    path = updater.download(inst, sums, tmp_path, transport=t)
    assert path.read_bytes() == good

    served["installer"] = good + b"tampered"
    with pytest.raises(updater.UpdateError, match="doesn't match"):
        updater.download(inst, sums, tmp_path / "2", transport=t)
    assert list((tmp_path / "2").iterdir()) == []  # nothing left to run
    other = updater.Download("Fetchbox-macOS-Intel-v9.0.0.dmg", "https://x/m.dmg")
    with pytest.raises(updater.UpdateError, match="isn't in the release's checksum list"):
        updater.download(other, sums, tmp_path / "3", transport=t)


def test_macos_swap_script(tmp_path: Path) -> None:
    app = tmp_path / "Apps with space" / "Fetchbox.app"
    script = updater.macos_script(tmp_path / "u.dmg", app, wait_pid=4242, relaunch=True)
    text = script.read_text()
    script.unlink()
    assert "kill -0 4242" in text and "hdiutil attach" in text
    # the new copy goes in beside the old one first; quoted paths (spaces)
    assert f"'{app}.new'" in text and text.rstrip().endswith(f"open '{app}'")


async def test_install_from_the_web_ui_goes_through_the_tray(
    server: Running, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Settings' Install: only for the desktop app its tray started; the tray takes the
    request once (polling) and runs the same install as its menu item."""
    from fetchbox.modules.auth.deps import TRAY_ENV, TRAY_HEADER

    dl = "https://github.com/MichaelYagi/fetchbox/releases/download/v9.0.0/"
    names = ("Fetchbox-macOS-Silicon-v9.0.0.dmg", "Fetchbox-macOS-Intel-v9.0.0.dmg",
             "Fetchbox-Windows-10-11-v9.0.0-Setup.exe", "SHA256SUMS-desktop.txt")  # fmt: skip
    assets = [{"name": n, "browser_download_url": dl + n, "size": 1} for n in names]
    release = {"tag_name": "v9.0.0", "html_url": "x", "assets": assets}
    server.app.state.updates = UpdateChecker(
        httpx.MockTransport(lambda _req: httpx.Response(200, json=[release]))
    )
    async with server.client() as c:
        u = (await c.get("/system/updates")).json()
        assert u["update_available"] and not u["can_install"]  # not the desktop app
        r = await c.post("/system/updates/install")
        assert r.status_code == 409, r.text

        # the desktop app, started by its tray
        monkeypatch.setenv(TRAY_ENV, "tray-token")
        monkeypatch.setattr(updater, "installed_app", lambda: tmp_path / "Fetchbox.app")
        monkeypatch.setattr(updater.sys, "platform", "darwin")
        u = (await c.get("/system/updates")).json()
        assert u["can_install"] and u["installing"] is None
        r = await c.post("/system/updates/install")
        assert r.status_code == 202 and r.json() == {"version": "v9.0.0"}
        assert (await c.get("/system/updates")).json()["installing"] == "v9.0.0"
        # only the tray takes it
        assert (await c.post("/system/tray/requests")).status_code == 403
        tray = {TRAY_HEADER: "tray-token"}
        assert (await c.post("/system/tray/requests", headers=tray)).json() == {"install": "v9.0.0"}
        assert (await c.post("/system/tray/requests", headers=tray)).json() == {"install": None}


def test_the_tray_installs_what_the_web_ui_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    from fetchbox.tray import Updates

    monkeypatch.setattr(updater.sys, "platform", "win32")  # the Windows installer is picked
    names = ("Fetchbox-Windows-10-11-v9.0.0-Setup.exe", "SHA256SUMS-desktop.txt")
    dl = [{"name": n, "url": f"https://x/{n}", "size": 1} for n in names]
    asked: list[str | None] = ["v9.0.0", None]

    class Sup:
        healthy = True

        def call(self, method: str, path: str) -> httpx.Response:
            if path == "/system/tray/requests":
                return httpx.Response(200, json={"install": asked.pop(0)})
            assert path == "/system/updates?fresh=true"  # checked again, now
            body = {"update_available": True, "latest": "v9.0.0", "url": "x", "downloads": dl}
            return httpx.Response(200, json=body)

    u = Updates(Sup(), Path("."))  # type: ignore[arg-type]
    assert u.asked() and u.latest == "v9.0.0" and u.files is not None
    assert not u.asked()  # taken once
