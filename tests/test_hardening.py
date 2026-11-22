"""Hardening: filesystem boundary, security headers."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from fetchbox.providers.storage import StorageError
from fetchbox.providers.storage.local import inside
from tests.pipeline import Fakes, standard_hd
from tests.test_existing_library import _dest, _job


def test_inside(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    (root / "Movies").mkdir(parents=True)
    assert inside(root, "Movies/x.mkv") == (root / "Movies/x.mkv").resolve()
    for bad in ("../x.mkv", "Movies/../../x.mkv", str(tmp_path / "elsewhere.mkv")):
        with pytest.raises(StorageError, match="outside"):
            inside(root, bad)
    if hasattr(os, "symlink"):
        try:
            (root / "link").symlink_to(tmp_path, target_is_directory=True)
        except OSError:
            return  # no symlink permission (Windows without developer mode)
        with pytest.raises(StorageError, match="outside"):
            inside(root, "link/escaped.mkv")  # a symlink pointing out of the folder


async def test_import_api_refuses_paths_outside_the_folder(
    c: httpx.AsyncClient, fakes: Fakes, tmp_path: Path
) -> None:
    dest = await _dest(c, "movie")
    outside = Path(dest["path"]).parent / "secret.mkv"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"\0" * 1000)
    body = {"destination_id": dest["id"], "items": [{
        "folder": "x", "tmdb_id": 438631, "profile_id": await standard_hd(c),
        "files": ["../secret.mkv", str(outside)]}]}  # fmt: skip
    res = (await _job(c, (await c.post("/library/import", json=body)).json()["id"]))["result"]
    assert res["files"] == 0  # the title is added, but nothing outside is recorded


async def test_security_headers(c: httpx.AsyncClient) -> None:
    r = await c.get("/system/ping")
    assert r.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert (
        r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    )
    assert r.headers["referrer-policy"] == "no-referrer"
