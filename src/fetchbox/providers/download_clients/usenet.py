"""Usenet download clients: SABnzbd and NZBGet, over their web APIs.

Fetchbox fetches the NZB from the indexer itself (so the client never sees an indexer's
API key) and hands the client the file, paused; Fetchbox's queue rules resume it like a
torrent. A job's ref is the client's own id (SABnzbd's nzo_id, NZBGet's NZBID), which
stays the same from the queue into the history.

Their states map onto Fetchbox's like a torrent's: a finished job reports as ``seeding``
(complete; nothing is shared) and a failed one as ``error``, with the client's reason. Its
``save_path`` is the finished job's own folder, which holds only that job: the import
takes the files from there, and once it's in the library the job is removed from the
client with its files (Usenet has nothing to seed).

Neither client can list a finished job's files or move them, so ``files`` and
``relocate`` aren't offered: the import looks in the folder itself.
"""

from __future__ import annotations

import base64
import re
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from fetchbox.core.providers import registry
from fetchbox.core.secrets import strip_key
from fetchbox.providers.download_clients import (
    AddOptions,
    ClientFile,
    ClientRef,
    ClientTorrent,
    DownloadClientError,
    TorrentState,
    TransferLimits,
)
from fetchbox.providers.download_clients.external import ExternalSettings, _External, _kib
from fetchbox.providers.indexers import TorrentSource

MIB = 1024 * 1024


def _nzb(src: TorrentSource) -> bytes:
    if src.nzb is None:
        raise DownloadClientError("invalid_torrent", "a Usenet client takes NZB files only")
    return src.nzb


def _job_name(src: TorrentSource) -> str:
    """The job's name: the release's (the client names its folder after it)."""
    return (src.name or "Fetchbox download").strip()


def _job_folder(storage: str, name: str) -> str:
    """A finished job's folder from where the client says it put it. SABnzbd names the
    job's folder, or for a single file sometimes that file; either way, its folder. The
    path is the client's, in its own style (a Linux NAS's, whatever Fetchbox runs on)."""
    p: PurePath = PurePosixPath(storage) if "/" in storage else PureWindowsPath(storage)
    if p.name == name or not p.suffix or len(p.suffix) > 6:
        return storage
    return str(p.parent)


def _duration(text: str) -> int | None:
    """SABnzbd's time left, "h:mm:ss" or "d:hh:mm:ss"."""
    try:
        parts = [int(x) for x in text.split(":")]
    except ValueError:
        return None
    seconds = 0
    for n, unit in zip(reversed(parts), (1, 60, 3600, 86400), strict=False):
        seconds += n * unit
    return seconds or None


def _usenet_job(
    ref: str,
    name: str,
    state: TorrentState,
    progress: float,
    size: int | None,
    *,
    save_path: str = "",
    rate: int = 0,
    eta: int | None = None,
    error: str | None = None,
) -> ClientTorrent:
    done = int((size or 0) * progress)
    return ClientTorrent(
        ref=ref, name=name, state=state, progress=progress, size=size, downloaded=done,
        uploaded=0, download_rate=rate, upload_rate=0, seeds=0, peers=0, ratio=0.0,
        seed_seconds=0, save_path=save_path, error=error, eta_seconds=eta,
    )  # fmt: skip


class _Usenet(_External):
    protocol: ClassVar[Literal["torrent", "usenet", "direct"]] = "usenet"

    async def files(self, ref: ClientRef) -> list[ClientFile]:
        raise DownloadClientError("not_found", f"{self.product} doesn't list a job's files")

    async def recheck(self, ref: ClientRef) -> None:
        raise DownloadClientError("rejected", "a Usenet download can't be rechecked")


# ---- SABnzbd ------------------------------------------------------------------------------------


class SabnzbdSettings(ExternalSettings):
    url: str = Field(description="SABnzbd's address, e.g. http://192.168.1.10:8080")
    api_key: SecretStr = Field(description="SABnzbd: Config > General > Security > API Key")

    @field_validator("api_key", mode="before")
    @classmethod
    def _strip_key(cls, v: object) -> object:
        return strip_key(v)


# Queue states; post-processing ones are in the history ("Verifying", "Extracting", ...)
_SAB_QUEUE: dict[str, TorrentState] = {
    "Paused": "paused", "Grabbing": "metadata", "Fetching": "metadata",
    "Propagating": "metadata", "Checking": "checking",
}  # fmt: skip  # Queued, Downloading: downloading


class SabnzbdClient(_Usenet):
    kind: ClassVar[str] = "sabnzbd"
    product: ClassVar[str] = "SABnzbd"
    Settings: ClassVar[type[BaseModel]] = SabnzbdSettings

    def __init__(self, settings: BaseModel, **kw: Any) -> None:
        super().__init__(settings, **kw)
        assert isinstance(settings, SabnzbdSettings)
        self.sab = settings

    async def _api(self, mode: str, *, files: Any = None, **params: Any) -> Any:
        query = {"mode": mode, "output": "json", "apikey": self.sab.api_key.get_secret_value()}
        query.update({k: v for k, v in params.items() if v is not None})
        if files is not None:
            r = await self._request("POST", "/api", params=query, files=files)
        else:
            r = await self._request("GET", "/api", params=query)
        if r.status_code == 404:
            raise DownloadClientError("unavailable", f"no SABnzbd at {self.cfg.url} (HTTP 404)")
        if r.status_code >= 400:
            raise DownloadClientError("unavailable", f"SABnzbd returned HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise DownloadClientError(
                "unavailable", f"no SABnzbd at {self.cfg.url} (not its API)"
            ) from None
        if isinstance(data, dict) and data.get("error"):
            error = str(data["error"])
            if "api key" in error.lower():
                raise DownloadClientError(
                    "rejected",
                    "SABnzbd refused the API key: copy it from SABnzbd's Config > General > "
                    "Security > API Key",
                )
            raise DownloadClientError("rejected", f"SABnzbd: {error}")
        return data

    async def version(self) -> str:
        await self._api("queue", limit=1)  # (checks the API key; "version" needs none)
        return str((await self._api("version")).get("version", ""))

    async def add(self, src: TorrentSource, opts: AddOptions) -> ClientRef:
        name = _job_name(src)
        data = await self._api(
            "addfile",
            files={"name": (f"{name}.nzb", _nzb(src), "application/x-nzb")},
            nzbname=name,
            cat=opts.category,
            priority=-2 if opts.paused else None,  # -2: paused
        )
        ids = data.get("nzo_ids") if isinstance(data, dict) else None
        if not data.get("status") or not ids:
            raise DownloadClientError("rejected", "SABnzbd didn't take the NZB")
        return str(ids[0])

    def _queued(self, s: dict[str, Any], rate: int) -> ClientTorrent:
        size = int(float(s.get("mb") or 0) * MIB) or None
        left = float(s.get("mbleft") or 0) * MIB
        progress = 1 - left / size if size else 0.0
        state = _SAB_QUEUE.get(str(s.get("status")), "downloading")
        return _usenet_job(
            str(s["nzo_id"]), str(s.get("filename") or ""), state, max(0.0, min(progress, 1.0)),
            size, rate=rate if state == "downloading" else 0,
            eta=_duration(str(s.get("timeleft") or "")) if state == "downloading" else None,
        )  # fmt: skip

    def _finished(self, h: dict[str, Any]) -> ClientTorrent:
        name = str(h.get("name") or "")
        status = str(h.get("status") or "")
        size = int(h.get("bytes") or 0) or None
        storage = str(h.get("storage") or "")
        if status == "Completed":
            folder = _job_folder(storage, name) if storage else ""
            return _usenet_job(str(h["nzo_id"]), name, "seeding", 1.0, size, save_path=folder)
        if status == "Failed":
            reason = str(h.get("fail_message") or "failed")
            reason = re.sub(r"\s*-\s*https?://\S+$", "", reason)  # (drop its help link)
            return _usenet_job(str(h["nzo_id"]), name, "error", 0.0, size, error=reason)
        # Queued for, or in, post-processing: verifying, repairing, extracting, moving
        return _usenet_job(str(h["nzo_id"]), name, "checking", 1.0, size)

    async def status(self, refs: list[ClientRef] | None = None) -> list[ClientTorrent]:
        ids = ",".join(refs) if refs else None
        queue = (await self._api("queue", nzo_ids=ids, limit=1000)).get("queue") or {}
        history = (await self._api("history", nzo_ids=ids, limit=1000)).get("history") or {}
        rate = int(float(queue.get("kbpersec") or 0) * 1024)
        out = [self._queued(s, rate) for s in queue.get("slots") or []]
        out += [self._finished(h) for h in history.get("slots") or []]
        rate_taken = False
        for t in out:  # the queue's speed is one number: give it to the job downloading
            if t.download_rate and rate_taken:
                t.download_rate = 0
            rate_taken = rate_taken or bool(t.download_rate)
        return [t for t in out if not refs or t.ref in refs]

    async def set_limits(self, ref: ClientRef | None, limits: TransferLimits) -> None:
        if ref is not None:
            return  # SABnzbd limits its whole queue only
        await self._api("config", name="speedlimit", value=f"{_kib(limits.download)}K")

    async def pause(self, ref: ClientRef) -> None:
        await self._api("queue", name="pause", value=ref)

    async def resume(self, ref: ClientRef) -> None:
        await self._api("queue", name="resume", value=ref)

    async def remove(self, ref: ClientRef, delete_data: bool) -> None:
        flag = 1 if delete_data else 0
        await self._api("queue", name="delete", value=ref, del_files=flag)
        await self._api("history", name="delete", value=ref, del_files=flag)


# ---- NZBGet (JSON-RPC) --------------------------------------------------------------------------


class NzbgetSettings(ExternalSettings):
    url: str = Field(description="NZBGet's address, e.g. http://192.168.1.10:6789")
    username: str | None = Field(
        default="nzbget", description="Settings > Security > ControlUsername"
    )
    password: SecretStr | None = Field(default=None, description="ControlPassword")


_NZBGET_QUEUE: dict[str, TorrentState] = {
    "PAUSED": "paused", "FETCHING": "metadata",
    "QUEUED": "downloading", "DOWNLOADING": "downloading",
}  # fmt: skip  # every other state is post-processing (verifying, repairing, unpacking...)

# History: SUCCESS/*, or WARNING/* that still left the files (a script's complaint)
_NZBGET_OK_WARNINGS = {"SCRIPT", "SKIPPED"}


def _size(g: dict[str, Any], key: str) -> int:
    return (int(g.get(f"{key}Hi") or 0) << 32) + int(g.get(f"{key}Lo") or 0)


class NzbgetClient(_Usenet):
    kind: ClassVar[str] = "nzbget"
    product: ClassVar[str] = "NZBGet"
    Settings: ClassVar[type[BaseModel]] = NzbgetSettings

    async def _rpc(self, method: str, *params: Any) -> Any:
        auth = (self.cfg.username or "", self._password())
        r = await self._request(
            "POST", "/jsonrpc", json={"method": method, "params": list(params)}, auth=auth
        )
        if r.status_code == 401:
            raise DownloadClientError(
                "rejected",
                "NZBGet refused the username or password: they're NZBGet's ControlUsername "
                "and ControlPassword (Settings > Security)",
            )
        if r.status_code == 404:
            raise DownloadClientError("unavailable", f"no NZBGet at {self.cfg.url} (HTTP 404)")
        if r.status_code >= 400:
            raise DownloadClientError("unavailable", f"NZBGet returned HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise DownloadClientError(
                "unavailable", f"no NZBGet at {self.cfg.url} (not its API)"
            ) from None
        if data.get("error"):
            raise DownloadClientError("rejected", f"NZBGet: {data['error'].get('message', '')}")
        return data.get("result")

    async def version(self) -> str:
        return str(await self._rpc("version"))

    async def add(self, src: TorrentSource, opts: AddOptions) -> ClientRef:
        name = _job_name(src)
        content = base64.b64encode(_nzb(src)).decode()
        # append(NZBFilename, Content, Category, Priority, AddToTop, AddPaused, DupeKey,
        #        DupeScore, DupeMode, PPParameters)
        nzb_id = await self._rpc(
            "append", f"{name}.nzb", content, opts.category or "", 0, False, opts.paused,
            "", 0, "FORCE", [],
        )  # fmt: skip
        if not isinstance(nzb_id, int) or nzb_id <= 0:
            raise DownloadClientError("rejected", "NZBGet didn't take the NZB")
        return str(nzb_id)

    def _queued(self, g: dict[str, Any], rate: int) -> ClientTorrent:
        size = _size(g, "FileSize") or None
        left = _size(g, "RemainingSize")
        progress = 1 - left / size if size else 0.0
        state = _NZBGET_QUEUE.get(str(g.get("Status")), "checking")
        downloading = state == "downloading" and str(g.get("Status")) == "DOWNLOADING"
        return _usenet_job(
            str(g["NZBID"]), str(g.get("NZBName") or ""), state,
            1.0 if state == "checking" else max(0.0, min(progress, 1.0)), size,
            rate=rate if downloading else 0,
            eta=int(left / rate) if downloading and rate else None,
        )  # fmt: skip

    def _finished(self, h: dict[str, Any]) -> ClientTorrent:
        ref, name = str(h["NZBID"]), str(h.get("Name") or "")
        kind, _, detail = str(h.get("Status") or "").partition("/")
        size = _size(h, "FileSize") or None
        if kind == "SUCCESS" or (kind == "WARNING" and detail in _NZBGET_OK_WARNINGS):
            folder = str(h.get("FinalDir") or h.get("DestDir") or "")
            return _usenet_job(ref, name, "seeding", 1.0, size, save_path=folder)
        why = {
            "HEALTH": "articles missing from your news server(s)",
            "PAR": "repair failed", "UNPACK": "unpacking failed", "MOVE": "moving failed",
            "PASSWORD": "the archive needs a password", "SPACE": "out of disk space",
            "DAMAGED": "damaged and couldn't be repaired", "MANUAL": "deleted in NZBGet",
            "DUPE": "deleted in NZBGet as a duplicate", "COPY": "a duplicate of a file there",
        }.get(detail, detail.lower() or "failed")  # fmt: skip
        return _usenet_job(ref, name, "error", 0.0, size, error=f"NZBGet: {why}")

    async def status(self, refs: list[ClientRef] | None = None) -> list[ClientTorrent]:
        rate = int((await self._rpc("status") or {}).get("DownloadRate") or 0)
        out = [self._queued(g, rate) for g in await self._rpc("listgroups", 0) or []]
        out += [self._finished(h) for h in await self._rpc("history", False) or []]
        return [t for t in out if not refs or t.ref in refs]

    async def set_limits(self, ref: ClientRef | None, limits: TransferLimits) -> None:
        if ref is not None:
            return  # NZBGet limits its whole queue only
        await self._rpc("rate", _kib(limits.download))

    async def _edit(self, command: str, ref: ClientRef) -> None:
        if not await self._rpc("editqueue", command, "", [int(ref)]):
            raise DownloadClientError("not_found", f"NZBGet has no job {ref}")

    async def pause(self, ref: ClientRef) -> None:
        await self._edit("GroupPause", ref)

    async def resume(self, ref: ClientRef) -> None:
        await self._edit("GroupResume", ref)

    async def remove(self, ref: ClientRef, delete_data: bool) -> None:
        """From the queue (its partial files too when asked) or the history. NZBGet keeps
        a finished job's files: Fetchbox deletes that folder itself (see the service)."""
        queued = {str(g["NZBID"]) for g in await self._rpc("listgroups", 0) or []}
        if ref in queued:
            await self._edit("GroupFinalDelete" if delete_data else "GroupDelete", ref)
        else:
            await self._rpc("editqueue", "HistoryFinalDelete", "", [int(ref)])


registry.register(SabnzbdClient)
registry.register(NzbgetClient)
