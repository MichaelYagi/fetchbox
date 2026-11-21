"""In-memory qBittorrent, Transmission and Deluge web APIs, for httpx.MockTransport.

One ``Store`` holds the torrents; each fake speaks one client's protocol over it, with
its login / session rules. ``finish(ref)`` completes a torrent, writing its file under
``local_root`` (the path the client's save path maps to), like a real client would.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from fetchbox.providers.download_clients.external import torrent_ref
from fetchbox.providers.indexers import TorrentSource


@dataclass
class T:
    ref: str
    name: str
    save_path: str
    paused: bool
    progress: float = 0.0
    size: int = 1000
    uploaded: int = 0
    seed_seconds: int = 0
    category: str | None = None
    error: str | None = None


@dataclass
class Store:
    torrents: dict[str, T] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)
    local_root: Path | None = None  # client path prefix -> this local folder
    client_root: str = ""
    default_dir: str = "/downloads"  # where the client saves when not told

    def add(self, src: TorrentSource, save_path: str, paused: bool, category: str | None) -> str:
        ref = torrent_ref(src)
        name = "Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-FLUX"
        self.torrents.setdefault(ref, T(ref, name, save_path, paused, category=category))
        return ref

    def finish(self, ref: str, data: bytes = b"x" * 1000) -> None:
        t = self.torrents[ref]
        t.progress, t.size = 1.0, len(data)
        if self.local_root is not None:
            rel = t.save_path.removeprefix(self.client_root).lstrip("/")
            f = self.local_root / rel / t.name / f"{t.name}.mkv"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)


def _form(req: httpx.Request) -> dict[str, str]:
    ctype = req.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        out: dict[str, str] = {}
        boundary = ctype.split("boundary=")[1].encode()
        for part in req.content.split(b"--" + boundary):
            if b'name="' not in part:
                continue
            head, _, body = part.partition(b"\r\n\r\n")
            name = head.split(b'name="')[1].split(b'"')[0].decode()
            out[name] = (
                base64.b64encode(body[:-2]).decode() if name == "torrents" else (body[:-2].decode())
            )
        return out
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


class FakeQbit:
    def __init__(self, store: Store, *, version: str = "v4.6.7", api: str = "2.9.3") -> None:
        self.s, self.version, self.api = store, version, api
        self.sid: str | None = None
        self.categories: set[str] = set()
        self.v5 = tuple(int(x) for x in api.split(".")[:2]) >= (2, 11)

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v2/")
        self.s.calls.append(path)
        if path == "auth/login":
            f = _form(req)
            # As the real ones answer: 4.x 200 "Ok."/"Fails.", 5.x 204 (empty)/401.
            if (f.get("username"), f.get("password")) != ("admin", "pw"):
                return httpx.Response(401) if self.v5 else httpx.Response(200, text="Fails.")
            self.sid = "s1"
            cookie = {"set-cookie": "SID=s1; path=/"}
            return (
                httpx.Response(204, headers=cookie)
                if self.v5
                else httpx.Response(200, text="Ok.", headers=cookie)
            )
        if req.headers.get("cookie") != f"SID={self.sid}" or self.sid is None:
            return httpx.Response(403, text="Forbidden")
        q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
        f = _form(req) if req.method == "POST" else {}
        if path == "app/version":
            return httpx.Response(200, text=self.version)
        if path == "app/webapiVersion":
            return httpx.Response(200, text=self.api)
        if path == "torrents/createCategory":
            if f["category"] in self.categories:
                return httpx.Response(409, text="Category name already exists")
            self.categories.add(f["category"])
            return httpx.Response(200, text="")
        if path == "torrents/add":
            src = (
                TorrentSource(torrent=base64.b64decode(f["torrents"]))
                if "torrents" in f
                else TorrentSource(magnet=f["urls"])
            )
            self.s.add(
                src,
                f.get("savepath", self.s.default_dir),
                f.get("paused") == "true",
                f.get("category"),
            )
            return httpx.Response(200, text="Ok.")
        if path == "torrents/info":
            hashes = q.get("hashes", "").split("|") if q.get("hashes") else None
            ts = [t for t in self.s.torrents.values() if not hashes or self._qid(t.ref) in hashes]
            rows = [self._info(t) for t in ts]
            return httpx.Response(200, json=rows)
        if path == "torrents/files":
            t = self.s.torrents[self._ref(q["hash"])]
            files = [{"name": f"{t.name}/{t.name}.mkv", "size": t.size, "progress": t.progress}]
            return httpx.Response(200, json=files)
        ref = self._ref(f.get("hashes", ""))
        if path in ("torrents/pause", "torrents/stop"):
            if (path == "torrents/stop") != (self.v5):
                return httpx.Response(404)
            self.s.torrents[ref].paused = True
        elif path in ("torrents/resume", "torrents/start"):
            if (path == "torrents/start") != (self.v5):
                return httpx.Response(404)
            self.s.torrents[ref].paused = False
        elif path == "torrents/delete":
            self.s.torrents.pop(ref, None)
            self.s.limits["deleted_files"] = f["deleteFiles"]
        elif path == "torrents/recheck":
            pass
        elif path.endswith("Limit"):
            self.s.limits[path] = f["limit"]
        else:
            return httpx.Response(404)
        return httpx.Response(200, text="")

    def _qid(self, ref: str) -> str:
        """qBittorrent 5 names hybrid torrents by their (truncated) v2 hash."""
        return hashlib.sha256(ref.encode()).hexdigest()[:40] if self.v5 else ref

    def _ref(self, qid: str) -> str:
        """Like the real one, only qBittorrent's own id is accepted."""
        return next((r for r in self.s.torrents if self._qid(r) == qid), "unknown:" + qid)

    def _info(self, t: T) -> dict[str, Any]:
        done = t.progress >= 1
        state = (
            ("pausedUP" if t.paused else "uploading")
            if done
            else ("pausedDL" if t.paused else "downloading")
        )
        if t.error:
            state = "error"
        return {
            "hash": self._qid(t.ref), "infohash_v1": t.ref,
            "name": t.name, "state": state, "progress": t.progress,
            "size": t.size, "downloaded": int(t.size * t.progress), "uploaded": t.uploaded,
            "dlspeed": 0, "upspeed": 0, "num_seeds": 0, "num_leechs": 0,
            "ratio": t.uploaded / t.size, "seeding_time": t.seed_seconds,
            "save_path": t.save_path, "eta": 8640000,
        }  # fmt: skip


class FakeTransmission:
    def __init__(self, store: Store) -> None:
        self.s = store
        self.session = "sess-1"

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.headers.get("authorization") != "Basic " + base64.b64encode(b"admin:pw").decode():
            return httpx.Response(401)
        if req.headers.get("x-transmission-session-id") != self.session:
            return httpx.Response(409, headers={"X-Transmission-Session-Id": self.session})
        body = json.loads(req.content)
        m, a = body["method"], body.get("arguments", {})
        self.s.calls.append(m)
        out: dict[str, Any] = {}
        if m == "session-get":
            out = {"version": "4.0.6"}
        elif m == "torrent-add":
            src = (
                TorrentSource(torrent=base64.b64decode(a["metainfo"]))
                if "metainfo" in a
                else TorrentSource(magnet=a["filename"])
            )
            dup = torrent_ref(src) in self.s.torrents
            ref = self.s.add(
                src,
                a.get("download-dir", self.s.default_dir),
                a["paused"],
                (a.get("labels") or [None])[0],
            )
            out = {"torrent-duplicate" if dup else "torrent-added": {"hashString": ref}}
        elif m == "torrent-get":
            ids = a.get("ids")
            ts = [t for t in self.s.torrents.values() if not ids or t.ref in ids]
            if "files" in a["fields"]:
                out = {"torrents": [{"name": t.name, "files": [
                    {"name": f"{t.name}/{t.name}.mkv", "length": t.size,
                     "bytesCompleted": int(t.size * t.progress)}]} for t in ts]}  # fmt: skip
            else:
                out = {"torrents": [self._t(t) for t in ts]}
        elif m in ("torrent-stop", "torrent-start"):
            for r in a["ids"]:
                self.s.torrents[r].paused = m == "torrent-stop"
        elif m == "torrent-remove":
            for r in a["ids"]:
                self.s.torrents.pop(r, None)
            self.s.limits["deleted_files"] = a["delete-local-data"]
        elif m in ("torrent-set", "session-set", "torrent-verify"):
            self.s.limits[m] = a
        else:
            return httpx.Response(200, json={"result": f"method name not recognized: {m}"})
        return httpx.Response(200, json={"result": "success", "arguments": out})

    def _t(self, t: T) -> dict[str, Any]:
        return {
            "hashString": t.ref, "name": t.name,
            "status": 0 if t.paused else (6 if t.progress >= 1 else 4),
            "percentDone": t.progress, "metadataPercentComplete": 1.0, "sizeWhenDone": t.size,
            "downloadedEver": int(t.size * t.progress), "uploadedEver": t.uploaded,
            "rateDownload": 0, "rateUpload": 0, "peersSendingToUs": 0, "peersGettingFromUs": 0,
            "uploadRatio": t.uploaded / t.size if t.uploaded else -1,
            "secondsSeeding": t.seed_seconds, "downloadDir": t.save_path,
            "error": 3 if t.error else 0, "errorString": t.error or "", "eta": -1,
        }  # fmt: skip


class FakeDeluge:
    def __init__(self, store: Store) -> None:
        self.s = store
        self.logged_in = False
        self.connected = False

    def handle(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        m, p, rid = body["method"], body["params"], body["id"]
        self.s.calls.append(m)

        def ok(result: Any) -> httpx.Response:
            return httpx.Response(200, json={"result": result, "error": None, "id": rid})

        if m == "auth.login":
            self.logged_in = p == ["pw"]
            return ok(self.logged_in)
        if not self.logged_in:
            return httpx.Response(
                200, json={"result": None, "error": {"message": "Not authenticated", "code": 1},
                           "id": rid})  # fmt: skip
        if m == "web.connected":
            return ok(self.connected)
        if m == "web.get_hosts":
            return ok([["h1", "127.0.0.1", 58846, "localclient"]])
        if m == "web.connect":
            self.connected = True
            return ok(None)
        if not self.connected:
            return httpx.Response(
                200, json={"result": None, "error": {"message": "not connected", "code": 2},
                           "id": rid})  # fmt: skip
        if m == "daemon.info":
            return ok("2.1.1")
        if m in ("core.add_torrent_magnet", "core.add_torrent_file"):
            src = (
                TorrentSource(magnet=p[0])
                if m == "core.add_torrent_magnet"
                else TorrentSource(torrent=base64.b64decode(p[1]))
            )
            opts = p[-1]
            return ok(
                self.s.add(
                    src, opts.get("download_location", self.s.default_dir), opts["add_paused"], None
                )
            )
        if m == "core.get_torrents_status":
            ids = p[0].get("id")
            return ok({t.ref: self._t(t) for t in self.s.torrents.values()
                       if not ids or t.ref in ids})  # fmt: skip
        if m == "core.get_torrent_status":
            t = self.s.torrents[p[0]]
            return ok({"files": [{"path": f"{t.name}/{t.name}.mkv", "size": t.size}],
                       "file_progress": [t.progress]})  # fmt: skip
        if m in ("core.pause_torrent", "core.resume_torrent"):
            for r in p[0]:
                self.s.torrents[r].paused = m == "core.pause_torrent"
            return ok(None)
        if m == "core.remove_torrent":
            self.s.torrents.pop(p[0], None)
            self.s.limits["deleted_files"] = p[1]
            return ok(True)
        if m in ("core.set_config", "core.set_torrent_options", "core.force_recheck"):
            self.s.limits[m] = p
            return ok(None)
        return httpx.Response(
            200, json={"result": None, "error": {"message": f"Unknown method {m}", "code": 2},
                       "id": rid})  # fmt: skip

    def _t(self, t: T) -> dict[str, Any]:
        done = t.progress >= 1
        state = "Paused" if t.paused else ("Seeding" if done else "Downloading")
        return {
            "name": t.name, "state": state,
            "progress": t.progress * 100, "total_wanted": t.size,
            "all_time_download": int(t.size * t.progress), "total_uploaded": t.uploaded,
            "download_payload_rate": 0, "upload_payload_rate": 0, "num_seeds": 0, "num_peers": 0,
            "ratio": t.uploaded / t.size, "seeding_time": t.seed_seconds,
            "save_path": t.save_path, "eta": 0, "message": "OK", "is_finished": done,
        }  # fmt: skip
