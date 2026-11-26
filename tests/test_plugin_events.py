"""The plugin API's public events: translated from Fetchbox's own, adding nothing to the bus
(so the Activity log, notifications and webhooks see exactly what they did before)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
import pytest

from fetchbox.core.events import Actor, Event, Subject
from fetchbox.plugin import PUBLIC_EVENTS, PublicEvent, public_events, to_public
from fetchbox.plugin.events import _MAP
from fetchbox.providers.download_clients.builtin import BuiltinClient
from tests.conftest import Running
from tests.pipeline import Fakes
from tests.test_library_api import EXPECTED, _acquire, fast, made, seed  # noqa: F401
from tests.torrents import Made, until


def _ev(type_: str, subject: Subject | None = None, **data: Any) -> Event:
    return Event(type=type_, subject=subject, actor=Actor(), data=data)


def test_every_public_event_comes_from_an_internal_one() -> None:
    assert {public for public, _ in _MAP.values()} == set(PUBLIC_EVENTS)


def test_translation() -> None:
    title = Subject(type="title", id="T1")
    added = to_public(_ev("title.added", title, name="Dune", year=2021, kind="movie"))
    assert added is not None and added.type == "title.added"
    assert (added.title_id, added.kind, added.data) == (
        "T1",
        "movie",
        {"name": "Dune", "year": 2021},
    )

    f = Subject(type="media_file", id="F1")
    imported = to_public(_ev("import.completed", f, title_id="T1", kind="series",
                             file_ids=["F1"], path="Show/S01E01.mkv", folders=["/x"]))  # fmt: skip
    assert imported is not None and imported.type == "file.imported"
    assert imported.file_ids == ["F1"] and imported.title_id == "T1"
    assert "folders" not in imported.data  # internal detail stays internal

    album = to_public(_ev("import.completed", f, title_id="A1", file_ids=["F1", "F2", "F3"]))
    assert album is not None and album.file_ids == ["F1", "F2", "F3"]

    renamed = to_public(_ev("file.renamed", f, title_id="T1", old="a.mkv", new="b.mkv"))
    assert renamed is not None and renamed.file_ids == ["F1"]
    assert renamed.data == {"old": "a.mkv", "new": "b.mkv"}

    assert to_public(_ev("download.progress", None, progress=0.5)) is None
    assert to_public(_ev("auth.login", None)) is None


async def test_unknown_types_are_refused(server: Running) -> None:
    with pytest.raises(ValueError, match="not public"):
        async for _ in public_events(server.app.state.fb.bus, "import.completed"):
            pass


async def test_an_import_rename_and_delete_as_plugins_see_them(
    c: httpx.AsyncClient,
    fakes: Fakes,
    server: Running,
    made: Made,  # noqa: F811
    seed: BuiltinClient,  # noqa: F811
) -> None:
    seen: list[PublicEvent] = []

    async def collect() -> None:
        async for ev in public_events(server.app.state.fb.bus):
            seen.append(ev)

    task = asyncio.create_task(collect())
    try:
        await asyncio.sleep(0)
        movie = await _acquire(c, fakes, server, made, seed)
        await until(lambda: _has(seen, "file.imported"))
        [imp] = [e for e in seen if e.type == "file.imported"]
        assert imp.title_id == movie["id"] and imp.kind == "movie"
        [f] = (await c.get(f"/titles/{movie['id']}/files")).json()
        assert imp.file_ids == [f["id"]] and imp.data["path"] == EXPECTED
        assert {"title.added", "release.grabbed", "download.completed"} <= {e.type for e in seen}

        # a rename: one public event per file, and still one Activity line for it
        simple = {"movie_folder": "{Title} ({Year})", "movie_file": "{Title} ({Year})"}
        assert (await c.put("/library/naming/movie", json={"patterns": simple})).status_code == 200
        allow = {"storage": {"rename_existing": True}}
        assert (await c.patch("/system/config", json=allow)).status_code == 200
        r = await c.post("/library/rename/movie", json={})
        assert r.status_code == 200 and r.json()["failed"] == [], r.text
        assert [x["file_id"] for x in r.json()["renamed"]] == [f["id"]], r.text
        await until(lambda: _has(seen, "file.renamed"))
        [ren] = [e for e in seen if e.type == "file.renamed"]
        assert ren.file_ids == [f["id"]] and ren.title_id == movie["id"]
        assert ren.data == {"old": EXPECTED, "new": "Dune (2021)/Dune (2021).mkv",
                            "destination_id": ren.data["destination_id"]}  # fmt: skip

        assert (await c.delete(f"/files/{f['id']}")).status_code == 204
        await until(lambda: _has(seen, "file.deleted"))
        [gone] = [e for e in seen if e.type == "file.deleted"]
        assert gone.file_ids == [f["id"]] and gone.title_id == movie["id"]

        await server.app.state.activity.settle()
        actions = [a["category"] + "." + a["action"] for a in
                   (await c.get("/activity", params={"limit": 200})).json()["items"]]  # fmt: skip
        assert "file.renamed" not in actions and actions.count("library.renamed") == 1
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _has(seen: list[PublicEvent], type_: str) -> bool:
    return any(e.type == type_ for e in seen)
