"""Fake SABnzbd and NZBGet, shaped on responses recorded from the real ones (SABnzbd 5.1.3,
NZBGet 26.3; see tests/fixtures/usenet). A finished job is written to disk as each writes
it: SABnzbd into complete/<name>/, NZBGet into completed/<Category>/<name>/."""

from __future__ import annotations

import base64
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

SAB_KEY = "0123456789abcdef0123456789abcdef"
NZBGET_AUTH = ("nzbget", "tegbzn6789")


@dataclass
class Job:
    ref: str
    name: str
    category: str
    nzb: bytes
    paused: bool
    state: str = "queued"  # queued, completed, failed
    folder: Path | None = None  # where it finished (as Fetchbox sees it)
    reason: str = ""


@dataclass
class UsenetStore:
    """What a client holds; ``local`` is its download folder as Fetchbox sees it, which
    the client sees as ``remote`` (a path mapping, as on a NAS)."""

    local: Path
    remote: str
    jobs: dict[str, Job] = field(default_factory=dict)
    added: int = 0

    def client_path(self, p: Path) -> str:
        return self.remote + "/" + p.relative_to(self.local).as_posix()

    def finish(self, ref: str, files: dict[str, bytes], under: str = "complete") -> Path:
        job = self.jobs[ref]
        folder = self.local / under / job.name
        for rel, data in files.items():
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_bytes(data)
        job.state, job.folder = "completed", folder
        return folder

    def fail(self, ref: str, reason: str) -> None:
        self.jobs[ref].state, self.jobs[ref].reason = "failed", reason


class FakeSab:
    def __init__(self, store: UsenetStore, key: str = SAB_KEY) -> None:
        self.store, self.key = store, key
        self.speedlimit = "0"

    def _slot(self, j: Job) -> dict[str, Any]:
        mb = f"{len(j.nzb) / 2**20:.2f}"
        return {"nzo_id": j.ref, "filename": j.name, "cat": j.category, "mb": mb,
                "mbleft": mb, "percentage": "0", "status": "Paused" if j.paused else
                "Downloading", "timeleft": "0:01:40"}  # fmt: skip

    def _history(self, j: Job) -> dict[str, Any]:
        done = j.state == "completed"
        storage = self.store.client_path(j.folder) if done and j.folder else ""
        return {"nzo_id": j.ref, "name": j.name, "category": j.category, "bytes": 300_000,
                "status": "Completed" if done else "Failed", "storage": storage,
                "fail_message": "" if done
                else f"{j.reason} - https://sabnzbd.org/not-complete"}  # fmt: skip

    def handle(self, req: httpx.Request) -> httpx.Response:
        q = req.url.params
        if req.url.path != "/api":
            return httpx.Response(404, text="<html>not found</html>")
        mode = q.get("mode")
        if mode == "version":
            return httpx.Response(200, json={"version": "5.1.3"})
        if q.get("apikey") != self.key:
            return httpx.Response(200, json={"status": False, "error": "API Key Incorrect"})
        jobs = self.store.jobs
        wanted = set(q["nzo_ids"].split(",")) if q.get("nzo_ids") else None
        if mode == "addfile":
            body = req.read()
            nzb = body[body.index(b"<?xml") : body.rindex(b"</nzb>") + 6]
            self.store.added += 1
            ref = f"SABnzbd_nzo_{self.store.added:04d}"
            jobs[ref] = Job(ref, q["nzbname"], q.get("cat", "*"), nzb, q.get("priority") == "-2")
            return httpx.Response(200, json={"status": True, "nzo_ids": [ref]})
        if mode == "queue" and q.get("name") in ("pause", "resume"):
            jobs[q["value"]].paused = q["name"] == "pause"
            return httpx.Response(200, json={"status": True, "nzo_ids": [q["value"]]})
        if mode in ("queue", "history") and q.get("name") == "delete":
            j = jobs.get(q["value"])
            if j is not None and (j.state == "queued") == (mode == "queue"):
                if q.get("del_files") == "1" and j.folder is not None:
                    shutil.rmtree(j.folder, ignore_errors=True)
                del jobs[q["value"]]
            return httpx.Response(200, json={"status": True, "nzo_ids": []})
        if mode == "queue":
            slots = [self._slot(j) for j in jobs.values() if j.state == "queued"]
            slots = [s for s in slots if wanted is None or s["nzo_id"] in wanted]
            return httpx.Response(200, json={"queue": {"kbpersec": "2048.00", "slots": slots}})
        if mode == "history":
            slots = [self._history(j) for j in jobs.values() if j.state != "queued"]
            slots = [s for s in slots if wanted is None or s["nzo_id"] in wanted]
            return httpx.Response(200, json={"history": {"slots": slots}})
        if mode == "config" and q.get("name") == "speedlimit":
            self.speedlimit = q["value"]
            return httpx.Response(200, json={"status": True})
        return httpx.Response(200, json={"status": False, "error": "Not implemented"})


class FakeNzbget:
    def __init__(self, store: UsenetStore, auth: tuple[str, str] = NZBGET_AUTH) -> None:
        self.store, self.auth = store, auth
        self.rate = 0

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.url.path != "/jsonrpc":
            return httpx.Response(404)
        user, pw = self.auth
        expected = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        if req.headers.get("authorization") != expected:
            return httpx.Response(401)
        call = json.loads(req.content)
        result = self._call(call["method"], call["params"])
        return httpx.Response(200, json={"version": "1.1", "result": result})

    def _group(self, j: Job) -> dict[str, Any]:
        return {"NZBID": int(j.ref), "NZBName": j.name, "Category": j.category,
                "Status": "PAUSED" if j.paused else "DOWNLOADING", "FileSizeLo": 300_000,
                "FileSizeHi": 0, "RemainingSizeLo": 300_000, "RemainingSizeHi": 0}  # fmt: skip

    def _history(self, j: Job) -> dict[str, Any]:
        done = j.state == "completed"
        return {"NZBID": int(j.ref), "Name": j.name, "Category": j.category,
                "Status": "SUCCESS/HEALTH" if done else "FAILURE/HEALTH", "FileSizeLo": 300_000,
                "FileSizeHi": 0, "FinalDir": "",
                "DestDir": self.store.client_path(j.folder)
                if done and j.folder else ""}  # fmt: skip

    def _call(self, method: str, params: list[Any]) -> Any:
        jobs = self.store.jobs
        if method == "version":
            return "26.3"
        if method == "status":
            return {"DownloadRate": 2_000_000}
        if method == "append":
            name, content, category, _prio, _top, paused = params[:6]
            self.store.added += 1
            ref = str(self.store.added)
            nzb = base64.b64decode(content)
            jobs[ref] = Job(ref, name.removesuffix(".nzb"), category, nzb, bool(paused))
            return self.store.added
        if method == "listgroups":
            return [self._group(j) for j in jobs.values() if j.state == "queued"]
        if method == "history":
            return [self._history(j) for j in jobs.values() if j.state != "queued"]
        if method == "rate":
            self.rate = params[0]
            return True
        if method == "editqueue":
            command, _, ids = params
            refs = [str(i) for i in ids]
            if not all(r in jobs for r in refs):
                return False
            for r in refs:
                if command in ("GroupPause", "GroupResume"):
                    jobs[r].paused = command == "GroupPause"
                elif command in ("GroupDelete", "GroupFinalDelete", "HistoryFinalDelete"):
                    del jobs[r]  # (NZBGet never deletes a finished job's files)
            return True
        raise AssertionError(f"unexpected NZBGet call {method}")
