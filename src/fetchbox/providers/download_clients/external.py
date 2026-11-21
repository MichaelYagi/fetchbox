"""External download clients over their web APIs: qBittorrent, Transmission, Deluge.

Each maps its client's states onto Fetchbox's (``metadata``, ``checking``,
``downloading``, ``seeding``, ``paused``, ``error``). A finished torrent counts as
``seeding`` even when the client has stopped it, so completion is never missed. The
torrent's ref is its v1 info hash, worked out locally (libtorrent) before adding, so
it doesn't depend on what each client echoes back.
"""

from __future__ import annotations

import base64
import itertools
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx
import libtorrent as lt
from pydantic import BaseModel, Field, SecretStr, field_validator

from fetchbox.core.providers import TestResult, registry
from fetchbox.providers.download_clients import (
    AddOptions,
    ClientFile,
    ClientRef,
    ClientTorrent,
    DownloadClientError,
    DownloadClientProvider,
    TorrentState,
    TransferLimits,
)
from fetchbox.providers.download_clients.builtin import ref_of
from fetchbox.providers.indexers import TorrentSource

TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class ExternalSettings(BaseModel):
    url: str = Field(description="Web UI / RPC address, e.g. http://192.168.1.10:8080")
    username: str | None = None
    password: SecretStr | None = None
    verify_tls: bool = Field(default=True, description="Check the HTTPS certificate")

    @field_validator("url")
    @classmethod
    def _http(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        p = urlsplit(v)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("must be an http(s) URL")
        return v


def torrent_ref(src: TorrentSource) -> ClientRef:
    try:
        if src.torrent is not None:
            return ref_of(lt.torrent_info(lt.bdecode(src.torrent)).info_hashes())  # type: ignore[arg-type]
        if src.magnet:
            return ref_of(lt.parse_magnet_uri(src.magnet).info_hashes)
    except RuntimeError as exc:
        raise DownloadClientError("invalid_torrent", f"not a valid torrent: {exc}") from None
    raise DownloadClientError("invalid_torrent", "no magnet link or .torrent given")


def _kib(limit: int | None) -> int:
    return 0 if not limit else max(1, limit // 1024)


class _External(DownloadClientProvider):
    """Shared HTTP plumbing; subclasses implement the client's API."""

    product: ClassVar[str] = ""

    def __init__(
        self, settings: BaseModel, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(settings)
        assert isinstance(settings, ExternalSettings)
        self.cfg = settings
        self._http = httpx.AsyncClient(
            base_url=settings.url,
            timeout=TIMEOUT,
            transport=transport,
            verify=settings.verify_tls,
            headers={"User-Agent": "Fetchbox"},
        )

    async def stop(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return await self._http.request(method, path, **kw)
        except httpx.TimeoutException:
            raise DownloadClientError("unavailable", f"{self.product} timed out") from None
        except httpx.HTTPError as exc:
            raise DownloadClientError(
                "unavailable", f"{self.product} unreachable: {type(exc).__name__}"
            ) from None

    def _password(self) -> str:
        return self.cfg.password.get_secret_value() if self.cfg.password else ""

    async def version(self) -> str:
        raise NotImplementedError

    async def test(self) -> TestResult:
        try:
            v = await self.version()
        except DownloadClientError as exc:
            return TestResult(ok=False, message=exc.message, details={"kind": exc.kind})
        return TestResult(ok=True, message=f"{self.product} {v}".strip(), details={"version": v})


# ---- qBittorrent (Web API v2) -------------------------------------------------------------------

_QBIT: dict[str, TorrentState] = {
    "error": "error", "missingFiles": "error", "unknown": "error",
    "metaDL": "metadata", "forcedMetaDL": "metadata",
    "checkingDL": "checking", "checkingUP": "checking", "checkingResumeData": "checking",
    "pausedDL": "paused", "stoppedDL": "paused",
    "uploading": "seeding", "stalledUP": "seeding", "queuedUP": "seeding",
    "forcedUP": "seeding", "pausedUP": "seeding", "stoppedUP": "seeding",
}  # fmt: skip  # everything else (downloading, stalledDL, queuedDL, moving...) downloads


class QbittorrentClient(_External):
    kind: ClassVar[str] = "qbittorrent"
    product: ClassVar[str] = "qBittorrent"
    Settings: ClassVar[type[BaseModel]] = ExternalSettings

    def __init__(self, settings: BaseModel, **kw: Any) -> None:
        super().__init__(settings, **kw)
        self._logged_in = False
        self._v5: bool | None = None  # 5.x renamed pause/resume to stop/start

    async def _login(self) -> None:
        r = await self._request(
            "POST",
            "/api/v2/auth/login",
            data={"username": self.cfg.username or "", "password": self._password()},
            headers={"Referer": self.cfg.url},
        )
        if r.status_code == 403:
            raise DownloadClientError("rejected", "qBittorrent banned this IP after failed logins")
        if r.status_code != 200 or r.text.strip() != "Ok.":
            raise DownloadClientError("rejected", "qBittorrent refused the username or password")
        self._logged_in = True

    async def _api(self, method: str, path: str, **kw: Any) -> httpx.Response:
        if not self._logged_in:
            await self._login()
        r = await self._request(method, "/api/v2/" + path, **kw)
        if r.status_code == 403:  # session expired
            await self._login()
            r = await self._request(method, "/api/v2/" + path, **kw)
        if r.status_code == 404:
            raise DownloadClientError("not_found", f"qBittorrent has no {path}")
        if r.status_code >= 400:
            raise DownloadClientError("rejected", f"qBittorrent: {r.text.strip() or r.status_code}")
        return r

    async def version(self) -> str:
        return (await self._api("GET", "app/version")).text.strip()

    async def _is_v5(self) -> bool:
        if self._v5 is None:
            api = (await self._api("GET", "app/webapiVersion")).text.strip()
            parts = tuple(int(p) for p in api.split(".")[:2] if p.isdigit())
            self._v5 = parts >= (2, 11)
        return self._v5

    async def add(self, src: TorrentSource, opts: AddOptions) -> ClientRef:
        ref = torrent_ref(src)
        if opts.category:
            try:
                await self._api(
                    "POST",
                    "torrents/createCategory",
                    data={"category": opts.category, "savePath": opts.save_path},
                )
            except DownloadClientError as exc:
                if exc.kind != "rejected":  # 409: the category exists
                    raise
        data = {
            "savepath": opts.save_path,
            "paused": str(opts.paused).lower(),
            "stopped": str(opts.paused).lower(),
            "autoTMM": "false",
        }
        if opts.category:
            data["category"] = opts.category
        if opts.download_limit:
            data["dlLimit"] = str(opts.download_limit)
        if opts.upload_limit:
            data["upLimit"] = str(opts.upload_limit)
        files = None
        if src.torrent is not None:
            files = {"torrents": ("release.torrent", src.torrent, "application/x-bittorrent")}
        else:
            data["urls"] = src.magnet or ""
        r = await self._api("POST", "torrents/add", data=data, files=files)
        # "Fails." for a torrent already there counts as added; anything else is a refusal.
        if r.text.strip() == "Fails." and not await self.status([ref]):
            raise DownloadClientError("invalid_torrent", "qBittorrent refused the torrent")
        return ref

    def _torrent(self, t: dict[str, Any]) -> ClientTorrent:
        state = _QBIT.get(t.get("state", ""), "downloading")
        size = t.get("size")
        eta = t.get("eta")
        return ClientTorrent(
            ref=t["hash"].lower(),
            name=t.get("name", ""),
            state=state,
            progress=float(t.get("progress", 0.0)),
            size=size if size and size > 0 else None,
            downloaded=int(t.get("downloaded", 0)),
            uploaded=int(t.get("uploaded", 0)),
            download_rate=int(t.get("dlspeed", 0)),
            upload_rate=int(t.get("upspeed", 0)),
            seeds=int(t.get("num_seeds", 0)),
            peers=int(t.get("num_leechs", 0)),
            ratio=float(t.get("ratio", 0.0)),
            seed_seconds=int(t.get("seeding_time", 0)),
            save_path=t.get("save_path", ""),
            error=t.get("state") if state == "error" else None,
            eta_seconds=eta if isinstance(eta, int) and 0 <= eta < 8640000 else None,
        )

    async def status(self, refs: list[ClientRef] | None = None) -> list[ClientTorrent]:
        params = {"hashes": "|".join(refs)} if refs else {}
        if refs is not None and not refs:
            return []
        rows = (await self._api("GET", "torrents/info", params=params)).json()
        return [self._torrent(t) for t in rows]

    async def files(self, ref: ClientRef) -> list[ClientFile]:
        rows = (await self._api("GET", "torrents/files", params={"hash": ref})).json()
        return [
            ClientFile(path=f["name"], size=int(f["size"]), progress=float(f["progress"]))
            for f in rows
        ]

    async def set_limits(self, ref: ClientRef | None, limits: TransferLimits) -> None:
        if ref is None:
            await self._api(
                "POST", "transfer/setDownloadLimit", data={"limit": limits.download or 0}
            )
            await self._api("POST", "transfer/setUploadLimit", data={"limit": limits.upload or 0})
            return
        h = {"hashes": ref}
        await self._api(
            "POST", "torrents/setDownloadLimit", data={**h, "limit": limits.download or 0}
        )
        await self._api("POST", "torrents/setUploadLimit", data={**h, "limit": limits.upload or 0})

    async def pause(self, ref: ClientRef) -> None:
        await self._api("POST", "torrents/stop" if await self._is_v5() else "torrents/pause",
                        data={"hashes": ref})  # fmt: skip

    async def resume(self, ref: ClientRef) -> None:
        await self._api("POST", "torrents/start" if await self._is_v5() else "torrents/resume",
                        data={"hashes": ref})  # fmt: skip

    async def remove(self, ref: ClientRef, delete_data: bool) -> None:
        await self._api(
            "POST",
            "torrents/delete",
            data={"hashes": ref, "deleteFiles": str(delete_data).lower()},
        )

    async def recheck(self, ref: ClientRef) -> None:
        await self._api("POST", "torrents/recheck", data={"hashes": ref})


# ---- Transmission (RPC) -------------------------------------------------------------------------

_FIELDS = [
    "hashString", "name", "status", "percentDone", "metadataPercentComplete", "sizeWhenDone",
    "downloadedEver", "uploadedEver", "rateDownload", "rateUpload", "peersSendingToUs",
    "peersGettingFromUs", "uploadRatio", "secondsSeeding", "downloadDir", "error",
    "errorString", "eta",
]  # fmt: skip


class TransmissionSettings(ExternalSettings):
    rpc_path: str = Field(default="/transmission/rpc", description="Appended to the URL")


class TransmissionClient(_External):
    kind: ClassVar[str] = "transmission"
    product: ClassVar[str] = "Transmission"
    Settings: ClassVar[type[BaseModel]] = TransmissionSettings

    def __init__(self, settings: BaseModel, **kw: Any) -> None:
        super().__init__(settings, **kw)
        assert isinstance(settings, TransmissionSettings)
        self._path = "/" + settings.rpc_path.strip("/")
        self._session = ""

    async def _rpc(self, method: str, **arguments: Any) -> dict[str, Any]:
        auth = (self.cfg.username or "", self._password()) if self.cfg.username else None
        body = {"method": method, "arguments": arguments}
        for _ in range(2):
            r = await self._request(
                "POST",
                self._path,
                json=body,
                auth=auth,
                headers={"X-Transmission-Session-Id": self._session},
            )
            if r.status_code == 409:  # first call, or the session id rotated
                self._session = r.headers.get("X-Transmission-Session-Id", "")
                continue
            break
        if r.status_code == 401:
            raise DownloadClientError("rejected", "Transmission refused the username or password")
        if r.status_code >= 400:
            raise DownloadClientError("unavailable", f"Transmission returned HTTP {r.status_code}")
        out: dict[str, Any] = r.json()
        if out.get("result") != "success":
            raise DownloadClientError("rejected", f"Transmission: {out.get('result')}")
        args: dict[str, Any] = out.get("arguments") or {}
        return args

    async def version(self) -> str:
        return str((await self._rpc("session-get", fields=["version"])).get("version", ""))

    async def add(self, src: TorrentSource, opts: AddOptions) -> ClientRef:
        ref = torrent_ref(src)
        args: dict[str, Any] = {"download-dir": opts.save_path, "paused": opts.paused}
        if src.torrent is not None:
            args["metainfo"] = base64.b64encode(src.torrent).decode()
        else:
            args["filename"] = src.magnet
        if opts.category:
            args["labels"] = [opts.category]
        out = await self._rpc("torrent-add", **args)
        if not ({"torrent-added", "torrent-duplicate"} & set(out)):
            raise DownloadClientError("invalid_torrent", "Transmission refused the torrent")
        return ref

    def _torrent(self, t: dict[str, Any]) -> ClientTorrent:
        done = float(t.get("percentDone", 0.0))
        status = int(t.get("status", 0))
        state: TorrentState
        if t.get("error"):
            state = "error"
        elif status in (1, 2):
            state = "checking"
        elif done >= 1.0:
            state = "seeding"
        elif status == 0:
            state = "paused"
        elif float(t.get("metadataPercentComplete", 1.0)) < 1.0:
            state = "metadata"
        else:
            state = "downloading"
        size = t.get("sizeWhenDone")
        eta = t.get("eta")
        return ClientTorrent(
            ref=t["hashString"].lower(),
            name=t.get("name", ""),
            state=state,
            progress=done,
            size=size if size else None,
            downloaded=int(t.get("downloadedEver", 0)),
            uploaded=int(t.get("uploadedEver", 0)),
            download_rate=int(t.get("rateDownload", 0)),
            upload_rate=int(t.get("rateUpload", 0)),
            seeds=int(t.get("peersSendingToUs", 0)),
            peers=int(t.get("peersGettingFromUs", 0)),
            ratio=max(float(t.get("uploadRatio", 0.0)), 0.0),
            seed_seconds=int(t.get("secondsSeeding", 0)),
            save_path=t.get("downloadDir", ""),
            error=t.get("errorString") or None if state == "error" else None,
            eta_seconds=eta if isinstance(eta, int) and eta >= 0 else None,
        )

    async def status(self, refs: list[ClientRef] | None = None) -> list[ClientTorrent]:
        if refs is not None and not refs:
            return []
        args: dict[str, Any] = {"fields": _FIELDS}
        if refs:
            args["ids"] = refs
        out = await self._rpc("torrent-get", **args)
        return [self._torrent(t) for t in out.get("torrents", [])]

    async def files(self, ref: ClientRef) -> list[ClientFile]:
        out = await self._rpc("torrent-get", ids=[ref], fields=["name", "files"])
        [t] = out.get("torrents") or [{}]
        files = t.get("files", [])
        if not files:
            raise DownloadClientError("not_found", f"Transmission has no torrent {ref}")
        return [
            ClientFile(
                path=f["name"],
                size=int(f["length"]),
                progress=(f["bytesCompleted"] / f["length"]) if f["length"] else 1.0,
            )
            for f in files
        ]

    async def set_limits(self, ref: ClientRef | None, limits: TransferLimits) -> None:
        down, up = _kib(limits.download), _kib(limits.upload)
        if ref is None:
            await self._rpc(
                "session-set",
                **{
                    "speed-limit-down": down,
                    "speed-limit-down-enabled": bool(down),
                    "speed-limit-up": up,
                    "speed-limit-up-enabled": bool(up),
                },
            )
            return
        await self._rpc(
            "torrent-set",
            ids=[ref],
            downloadLimit=down,
            downloadLimited=bool(down),
            uploadLimit=up,
            uploadLimited=bool(up),
        )

    async def pause(self, ref: ClientRef) -> None:
        await self._rpc("torrent-stop", ids=[ref])

    async def resume(self, ref: ClientRef) -> None:
        await self._rpc("torrent-start", ids=[ref])

    async def remove(self, ref: ClientRef, delete_data: bool) -> None:
        await self._rpc("torrent-remove", ids=[ref], **{"delete-local-data": delete_data})

    async def recheck(self, ref: ClientRef) -> None:
        await self._rpc("torrent-verify", ids=[ref])


# ---- Deluge (Web UI JSON-RPC) -------------------------------------------------------------------

_DELUGE_FIELDS = [
    "name", "state", "progress", "total_wanted", "all_time_download", "total_uploaded",
    "download_payload_rate", "upload_payload_rate", "num_seeds", "num_peers", "ratio",
    "seeding_time", "save_path", "eta", "message", "is_finished",
]  # fmt: skip


class DelugeClient(_External):
    """Talks to deluge-web, which must be connected to a daemon (it connects to the first
    configured host if not). Deluge has no username here: only the web password."""

    kind: ClassVar[str] = "deluge"
    product: ClassVar[str] = "Deluge"
    Settings: ClassVar[type[BaseModel]] = ExternalSettings

    def __init__(self, settings: BaseModel, **kw: Any) -> None:
        super().__init__(settings, **kw)
        self._ids = itertools.count(1)
        self._ready = False

    async def _call(self, method: str, *params: Any) -> Any:
        r = await self._request(
            "POST", "/json", json={"method": method, "params": list(params), "id": next(self._ids)}
        )
        if r.status_code >= 400:
            raise DownloadClientError("unavailable", f"Deluge returned HTTP {r.status_code}")
        out = r.json()
        if err := out.get("error"):
            if err.get("code") == 1:  # not authenticated
                self._ready = False
            raise DownloadClientError("rejected", f"Deluge: {err.get('message')}")
        return out.get("result")

    async def _rpc(self, method: str, *params: Any) -> Any:
        if not self._ready:
            if not await self._call("auth.login", self._password()):
                raise DownloadClientError("rejected", "Deluge refused the password")
            if not await self._call("web.connected"):
                hosts = await self._call("web.get_hosts") or []
                if not hosts:
                    raise DownloadClientError("unavailable", "deluge-web has no daemon configured")
                await self._call("web.connect", hosts[0][0])
            self._ready = True
        try:
            return await self._call(method, *params)
        except DownloadClientError:
            if self._ready:
                raise
            self._ready = False  # session expired: log in again once
            return await self._rpc(method, *params)

    async def version(self) -> str:
        return str(await self._rpc("daemon.info") or "")

    async def add(self, src: TorrentSource, opts: AddOptions) -> ClientRef:
        ref = torrent_ref(src)
        options: dict[str, Any] = {"download_location": opts.save_path, "add_paused": opts.paused}
        if opts.download_limit:
            options["max_download_speed"] = _kib(opts.download_limit)
        if opts.upload_limit:
            options["max_upload_speed"] = _kib(opts.upload_limit)
        try:
            if src.torrent is not None:
                b64 = base64.b64encode(src.torrent).decode()
                await self._rpc("core.add_torrent_file", "release.torrent", b64, options)
            else:
                await self._rpc("core.add_torrent_magnet", src.magnet, options)
        except DownloadClientError as exc:
            if "already" not in exc.message.lower():  # already in session: fine
                raise
        return ref

    def _torrent(self, ref: str, t: dict[str, Any]) -> ClientTorrent:
        raw = t.get("state", "")
        done = float(t.get("progress", 0.0)) / 100
        state: TorrentState
        if raw == "Error":
            state = "error"
        elif raw in ("Checking", "Allocating"):
            state = "checking"
        elif t.get("is_finished") or done >= 1.0:
            state = "seeding"
        elif raw == "Paused":
            state = "paused"
        else:
            state = "downloading"
        size = t.get("total_wanted")
        eta = t.get("eta")
        return ClientTorrent(
            ref=ref.lower(),
            name=t.get("name", ""),
            state=state,
            progress=done,
            size=size if size else None,
            downloaded=int(t.get("all_time_download", 0)),
            uploaded=int(t.get("total_uploaded", 0)),
            download_rate=int(t.get("download_payload_rate", 0)),
            upload_rate=int(t.get("upload_payload_rate", 0)),
            seeds=int(t.get("num_seeds", 0)),
            peers=int(t.get("num_peers", 0)),
            ratio=max(float(t.get("ratio", 0.0)), 0.0),
            seed_seconds=int(t.get("seeding_time", 0)),
            save_path=t.get("save_path", ""),
            error=t.get("message") if state == "error" else None,
            eta_seconds=int(eta) if isinstance(eta, int | float) and eta > 0 else None,
        )

    async def status(self, refs: list[ClientRef] | None = None) -> list[ClientTorrent]:
        if refs is not None and not refs:
            return []
        flt = {"id": refs} if refs else {}
        out = await self._rpc("core.get_torrents_status", flt, _DELUGE_FIELDS) or {}
        return [self._torrent(ref, t) for ref, t in out.items()]

    async def files(self, ref: ClientRef) -> list[ClientFile]:
        t = await self._rpc("core.get_torrent_status", ref, ["files", "file_progress"]) or {}
        files = t.get("files") or []
        if not files:
            raise DownloadClientError("not_found", f"Deluge has no torrent {ref}")
        progress = t.get("file_progress") or [0.0] * len(files)
        return [
            ClientFile(path=f["path"], size=int(f["size"]), progress=float(p))
            for f, p in zip(files, progress, strict=False)
        ]

    async def set_limits(self, ref: ClientRef | None, limits: TransferLimits) -> None:
        down = _kib(limits.download) or -1  # -1 = unlimited in Deluge
        up = _kib(limits.upload) or -1
        if ref is None:
            await self._rpc("core.set_config", {"max_download_speed": down, "max_upload_speed": up})
        else:
            await self._rpc(
                "core.set_torrent_options",
                [ref],
                {"max_download_speed": down, "max_upload_speed": up},
            )

    async def pause(self, ref: ClientRef) -> None:
        await self._rpc("core.pause_torrent", [ref])

    async def resume(self, ref: ClientRef) -> None:
        await self._rpc("core.resume_torrent", [ref])

    async def remove(self, ref: ClientRef, delete_data: bool) -> None:
        await self._rpc("core.remove_torrent", ref, delete_data)

    async def recheck(self, ref: ClientRef) -> None:
        await self._rpc("core.force_recheck", [ref])


registry.register(QbittorrentClient)
registry.register(TransmissionClient)
registry.register(DelugeClient)
