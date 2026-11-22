"""The built-in torrent engine: one libtorrent session inside the Fetchbox process.

Torrents survive restarts through resume data in ``<state_dir>/<info hash>.fastresume``,
written when metadata arrives, when a torrent finishes, every few minutes for torrents
that changed, and on shutdown. libtorrent's own queueing is turned off (torrents are
added without ``auto_managed``): Fetchbox's queue manager decides what runs.

A torrent is reported ``seeding`` only after its data is on disk. libtorrent says
"finished" when the last piece is verified, but on Windows its writes can still be in
flight: reading the file then gave short or wrong bytes. A resume save with
``flush_disk_cache`` fixed it 7 times in 8 and ``disk_write_mode=always_pwrite`` not at
all; pausing, which closes the files once pending disk jobs finish, fixed it every
time. So on finish the engine pauses the torrent, and on ``torrent_paused_alert`` marks
it flushed and resumes seeding (unless Fetchbox asked for a pause meanwhile). Until
then it reports ``downloading`` at 99.9%.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from pathlib import Path
from typing import Any, ClassVar, Literal

import libtorrent as lt
import structlog
from pydantic import BaseModel, Field, field_validator

from fetchbox.core.providers import HealthStatus, TestResult, registry
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
from fetchbox.providers.indexers import TorrentSource

log = structlog.get_logger(__name__)

ALERT_INTERVAL = 0.25
RESUME_INTERVAL = 300.0
STOP_TIMEOUT = 10.0
_CHECKING = {
    lt.torrent_status.states.queued_for_checking,
    lt.torrent_status.states.checking_files,
    lt.torrent_status.states.checking_resume_data,
    lt.torrent_status.states.allocating,
}
_DONE = {lt.torrent_status.states.finished, lt.torrent_status.states.seeding}


INTERFACE_CHECK_SECONDS = 2.0


class BuiltinSettings(BaseModel):
    listen_interfaces: str = Field(
        default="0.0.0.0:6881,[::]:6881", description="Where to accept peer connections"
    )
    dht: bool = True
    lsd: bool = Field(default=True, description="Local service discovery")
    upnp: bool = Field(default=True, description="UPnP and NAT-PMP port mapping")
    encryption: Literal["enabled", "forced", "disabled"] = "enabled"
    download_limit: int | None = Field(default=None, ge=0, description="Bytes/s; empty = none")
    upload_limit: int | None = Field(default=None, ge=0)
    connections_limit: int = Field(default=200, ge=10, le=5000)
    interface: str | None = Field(
        default=None,
        description="Only use this network interface (e.g. your VPN's: wg0, tun0, ProtonVPN); "
        "empty: any",
    )
    kill_switch: bool = Field(
        default=True,
        description="With an interface set: pause every torrent while it is down or has no "
        "address, and resume when it's back",
    )

    @field_validator("interface", mode="before")
    @classmethod
    def _iface(cls, v: object) -> object:
        return (v.strip() or None) if isinstance(v, str) else v


def interface_address(name: str) -> str | None:
    """The interface's current address (IPv4 preferred), or None when it is missing,
    down or has none. By address, so the binding works the same on every OS."""
    import socket

    import psutil

    stats = psutil.net_if_stats().get(name)
    if stats is None or not stats.isup:
        return None
    addrs = psutil.net_if_addrs().get(name, [])
    v4 = [a.address for a in addrs if a.family == socket.AF_INET]
    v6 = [a.address.split("%")[0] for a in addrs
          if a.family == socket.AF_INET6 and not a.address.startswith("fe80")]  # fmt: skip
    found = v4 or v6
    return found[0] if found else None


def _listen_port(listen: str) -> int:
    m = re.search(r":(\d+)(?:,|$)", listen)
    return int(m.group(1)) if m else 6881


def ref_of(hashes: Any) -> ClientRef:
    v1 = str(hashes.v1)
    if v1 and set(v1) != {"0"}:
        return v1
    return str(hashes.v2)[:40]


def _bound(address: str | None, port: int) -> dict[str, Any]:
    """Listen and connect only through ``address`` (the chosen interface's)."""
    if address is None:  # interface down: nothing to bind to; the session is paused
        address = "127.0.0.1"
    host = f"[{address}]" if ":" in address else address
    return {"listen_interfaces": f"{host}:{port}", "outgoing_interfaces": address}


def _pack(cfg: BuiltinSettings, address: str | None = None) -> dict[str, Any]:
    enc = {"enabled": 1, "forced": 0, "disabled": 2}[cfg.encryption]  # lt enc_policy
    bound = cfg.interface is not None
    return {
        "listen_interfaces": cfg.listen_interfaces,
        **(_bound(address, _listen_port(cfg.listen_interfaces)) if bound else {}),
        "enable_dht": cfg.dht,
        # Bound to a VPN: don't announce or map ports on the local network.
        "enable_lsd": cfg.lsd and not bound,
        "enable_upnp": cfg.upnp and not bound,
        "enable_natpmp": cfg.upnp and not bound,
        "out_enc_policy": enc,
        "in_enc_policy": enc,
        "download_rate_limit": cfg.download_limit or 0,
        "upload_rate_limit": cfg.upload_limit or 0,
        "connections_limit": cfg.connections_limit,
        "alert_mask": lt.alert_category.status
        | lt.alert_category.error
        | lt.alert_category.storage,
        "user_agent": "Fetchbox",
    }


class BuiltinClient(DownloadClientProvider):
    kind: ClassVar[str] = "builtin"
    Settings: ClassVar[type[BaseModel]] = BuiltinSettings
    capabilities: ClassVar[frozenset[str]] = frozenset({"magnet", "torrent", "resume_data"})

    def __init__(self, settings: BaseModel, *, state_dir: Path | None = None) -> None:
        super().__init__(settings)
        assert isinstance(settings, BuiltinSettings)
        self.cfg = settings
        self.state_dir = state_dir
        self._session: Any = None
        self._handles: dict[ClientRef, Any] = {}
        self._errors: dict[ClientRef, str] = {}
        self._flushing: set[ClientRef] = set()  # finished, paused to close its files
        self._flushed: set[ClientRef] = set()  # finished and on disk
        self._resume_after_flush: set[ClientRef] = set()
        self._counters: dict[tuple[ClientRef, str], tuple[int, int]] = {}
        self._pending_saves = 0
        self._tasks: list[asyncio.Task[None]] = []
        self.blocked: str | None = None  # why the kill switch has paused everything
        self._address: str | None = None

    # ---- lifecycle ----------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._session is not None

    @property
    def listen_port(self) -> int | None:
        return int(self._session.listen_port()) if self._session is not None else None

    async def start(self) -> None:
        if self._session is not None:
            return
        if self.cfg.interface:
            self._address = interface_address(self.cfg.interface)
        self._session = lt.session(_pack(self.cfg, self._address))  # type: ignore[call-overload]
        if self.cfg.interface and self._address is None and self.cfg.kill_switch:
            self._block()
        if self.state_dir is not None:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            for f in sorted(self.state_dir.glob("*.fastresume")):
                try:
                    params = lt.read_resume_data(f.read_bytes())
                    params.flags &= ~lt.torrent_flags.auto_managed
                    h = self._session.add_torrent(params)
                    ref = ref_of(h.info_hashes())
                    self._handles[ref] = h
                    self._flushed.add(ref)  # whatever finished before a restart is on disk
                except Exception as exc:  # a corrupt file must not stop the engine
                    log.warning("builtin.resume_failed", file=f.name, error=str(exc))
        self._tasks = [
            asyncio.create_task(self._alert_loop(), name="builtin-alerts"),
            asyncio.create_task(self._resume_loop(), name="builtin-resume"),
        ]
        if self.cfg.interface:
            self._tasks.append(asyncio.create_task(self._watch_interface(), name="builtin-vpn"))
        log.info("builtin.started", port=self.listen_port, torrents=len(self._handles))

    async def stop(self) -> None:
        if self._session is None:
            return
        for h in self._handles.values():
            if h.is_valid() and h.status().has_metadata:
                h.save_resume_data(lt.save_resume_flags_t.save_info_dict)
                self._pending_saves += 1
        deadline = time.monotonic() + STOP_TIMEOUT
        while self._pending_saves > 0 and time.monotonic() < deadline:
            self._drain_alerts()
            await asyncio.sleep(0.05)
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._session.pause()
        self._session = None
        self._handles.clear()
        log.info("builtin.stopped")

    async def aclose(self) -> None:
        await self.stop()

    # ---- interface binding and kill switch ----------------------------------------------------

    def _block(self) -> None:
        self.blocked = (
            f"network interface {self.cfg.interface} is down or has no address: "
            "all torrents paused (kill switch)"
        )
        self._session.pause()
        log.warning("builtin.kill_switch", interface=self.cfg.interface)

    def check_interface(self) -> None:
        """Pause or resume for the interface's state; follow its address if it changed."""
        if self._session is None or not self.cfg.interface:
            return
        addr = interface_address(self.cfg.interface)
        if addr is not None and addr != self._address:
            self._session.apply_settings(_bound(addr, _listen_port(self.cfg.listen_interfaces)))
            log.info("builtin.interface_address", interface=self.cfg.interface, address=addr)
        self._address = addr
        if addr is None and self.cfg.kill_switch and self.blocked is None:
            self._block()
        elif addr is not None and self.blocked is not None:
            self.blocked = None
            self._session.resume()
            log.info("builtin.kill_switch_released", interface=self.cfg.interface)

    async def _watch_interface(self) -> None:
        while True:
            await asyncio.sleep(INTERFACE_CHECK_SECONDS)
            try:
                self.check_interface()
            except Exception:  # never let the watcher die
                log.exception("builtin.interface_check_failed")

    def _s(self) -> Any:
        if self._session is None:
            raise DownloadClientError("unavailable", "the built-in engine is not running")
        return self._session

    # ---- alerts and resume data ---------------------------------------------------------------

    async def _alert_loop(self) -> None:
        while True:
            try:
                self._drain_alerts()
            except Exception:  # never let one bad alert kill the loop
                log.exception("builtin.alert_error")
            await asyncio.sleep(ALERT_INTERVAL)

    def _drain_alerts(self) -> None:
        if self._session is None:
            return
        for a in self._session.pop_alerts():
            if isinstance(a, lt.save_resume_data_alert):
                self._pending_saves = max(self._pending_saves - 1, 0)
                self._write_resume(a)
            elif isinstance(a, lt.save_resume_data_failed_alert):
                self._pending_saves = max(self._pending_saves - 1, 0)
            elif isinstance(a, lt.torrent_finished_alert):
                h = a.handle
                if h.is_valid() and int(h.status().total_payload_download) > 0:
                    ref = ref_of(h.info_hashes())
                    self._flushing.add(ref)
                    if not h.status().flags & lt.torrent_flags.paused:
                        self._resume_after_flush.add(ref)
                    h.pause()  # closes the files once pending writes are done
            elif isinstance(a, lt.torrent_paused_alert):
                h = a.handle
                if h.is_valid() and (ref := ref_of(h.info_hashes())) in self._flushing:
                    self._flushing.discard(ref)
                    self._flushed.add(ref)
                    h.save_resume_data(lt.save_resume_flags_t.save_info_dict)
                    self._pending_saves += 1
                    if ref in self._resume_after_flush:
                        self._resume_after_flush.discard(ref)
                        h.resume()
            elif isinstance(a, lt.metadata_received_alert):
                if a.handle.is_valid():
                    a.handle.save_resume_data(lt.save_resume_flags_t.save_info_dict)
                    self._pending_saves += 1
            elif (
                isinstance(a, lt.torrent_error_alert | lt.file_error_alert) and a.handle.is_valid()
            ):
                self._errors[ref_of(a.handle.info_hashes())] = a.message()

    def _write_resume(self, a: Any) -> None:
        if self.state_dir is None:
            return
        ref = ref_of(a.handle.info_hashes())
        if ref not in self._handles:
            return  # removed meanwhile
        path = self.state_dir / f"{ref}.fastresume"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(lt.write_resume_data_buf(a.params))
        tmp.replace(path)

    async def _resume_loop(self) -> None:
        while True:
            await asyncio.sleep(RESUME_INTERVAL)
            for h in list(self._handles.values()):
                if h.is_valid() and h.need_save_resume_data():
                    h.save_resume_data(lt.save_resume_flags_t.save_info_dict)
                    self._pending_saves += 1

    # ---- Provider -----------------------------------------------------------------------------

    async def test(self) -> TestResult:
        if self._session is None:
            return TestResult(ok=False, message="the built-in engine is not running")
        if self.blocked:
            return TestResult(ok=False, message=self.blocked)
        return TestResult(
            ok=True,
            message=f"libtorrent {lt.__version__} listening on port {self.listen_port}",
            details={"port": self.listen_port, "torrents": len(self._handles)},
        )

    async def health(self) -> HealthStatus:
        res = await self.test()
        return HealthStatus(status="ok" if res.ok else "error", message=res.message)

    # ---- DownloadClientProvider ---------------------------------------------------------------

    async def add(self, src: TorrentSource, opts: AddOptions) -> ClientRef:
        s = self._s()
        params: Any
        try:
            if src.torrent is not None:
                ti = lt.torrent_info(lt.bdecode(src.torrent))  # type: ignore[arg-type]
                params = lt.add_torrent_params()
                params.ti = ti
                ref = ref_of(ti.info_hashes())
            elif src.magnet:
                params = lt.parse_magnet_uri(src.magnet)
                ref = ref_of(params.info_hashes)
            else:
                raise DownloadClientError("invalid_torrent", "no magnet link or .torrent given")
        except DownloadClientError:
            raise
        except Exception as exc:
            raise DownloadClientError("invalid_torrent", f"unreadable torrent: {exc}") from None
        if (existing := self._handles.get(ref)) is not None and existing.is_valid():
            return ref  # already here: adding again is a no-op
        if not opts.save_path:
            raise DownloadClientError("rejected", "the built-in engine needs a save path")
        await asyncio.to_thread(Path(opts.save_path).mkdir, parents=True, exist_ok=True)
        params.save_path = opts.save_path
        flags = params.flags & ~lt.torrent_flags.auto_managed
        flags = (
            (flags | lt.torrent_flags.paused) if opts.paused else (flags & ~lt.torrent_flags.paused)
        )
        params.flags = flags
        if opts.download_limit:
            params.download_limit = opts.download_limit
        if opts.upload_limit:
            params.upload_limit = opts.upload_limit
        h = s.add_torrent(params)
        self._handles[ref] = h
        if src.torrent is not None:
            h.save_resume_data(lt.save_resume_flags_t.save_info_dict)
            self._pending_saves += 1
        return ref

    def _handle(self, ref: ClientRef) -> Any:
        h = self._handles.get(ref)
        if h is None or not h.is_valid():
            raise DownloadClientError("not_found", f"no torrent {ref} in the built-in engine")
        return h

    def _status(self, ref: ClientRef, st: Any) -> ClientTorrent:
        paused = bool(st.flags & lt.torrent_flags.paused)
        # Finished but not yet flushed to disk (see module docstring). Files that were
        # already complete when added (nothing downloaded this session) need no flush.
        flushing = ref in self._flushing or (
            st.state in _DONE and ref not in self._flushed and int(st.total_payload_download) > 0
        )
        error = self._errors.get(ref) or (st.errc.message() if st.errc.value() else None)
        state: TorrentState
        if error:
            state = "error"
        elif flushing:
            state = "downloading"
        elif paused:
            state = "paused"
        elif st.state == lt.torrent_status.states.downloading_metadata:
            state = "metadata"
        elif st.state in _CHECKING:
            state = "checking"
        elif st.state in _DONE and not flushing:
            state = "seeding"
        else:
            state = "downloading"
        wanted = int(st.total_wanted) if st.has_metadata else None
        remaining = (wanted - int(st.total_wanted_done)) if wanted is not None else None
        rate = int(st.download_payload_rate)
        # all_time_* lag (updated with resume data); total_payload_* reset on every pause.
        downloaded = max(
            int(st.all_time_download), self._count(ref, "down", st.total_payload_download)
        )
        uploaded = max(int(st.all_time_upload), self._count(ref, "up", st.total_payload_upload))
        return ClientTorrent(
            ref=ref,
            name=st.name or ref,
            state=state,
            progress=1.0 if state == "seeding" else 0.999 if flushing else float(st.progress),
            size=wanted,
            downloaded=downloaded,
            uploaded=uploaded,
            download_rate=rate,
            upload_rate=int(st.upload_payload_rate),
            seeds=int(st.num_seeds),
            peers=int(st.num_peers),
            ratio=round(uploaded / wanted, 3) if wanted else 0.0,
            seed_seconds=int(st.seeding_duration.total_seconds())
            if hasattr(st.seeding_duration, "total_seconds")
            else int(st.seeding_duration),
            save_path=st.save_path,
            error=error,
            eta_seconds=(remaining // rate) if remaining and rate > 0 else None,
        )

    def _count(self, ref: ClientRef, kind: str, value: Any) -> int:
        """Running total of a counter that libtorrent resets when a torrent pauses."""
        base, last = self._counters.get((ref, kind), (0, 0))
        v = int(value)
        if v < last:  # reset since the last look: bank what it had reached
            base += last
        self._counters[(ref, kind)] = (base, v)
        return base + v

    async def status(self, refs: list[ClientRef] | None = None) -> list[ClientTorrent]:
        self._s()
        wanted = refs if refs is not None else list(self._handles)
        out = []
        for ref in wanted:
            h = self._handles.get(ref)
            if h is not None and h.is_valid():
                out.append(self._status(ref, h.status()))
        return out

    async def files(self, ref: ClientRef) -> list[ClientFile]:
        h = self._handle(ref)
        ti = h.torrent_file()
        if ti is None:
            return []
        fs = ti.layout() if hasattr(ti, "layout") else ti.files()  # files() is deprecated in 2.1
        done = h.file_progress()
        out = []
        for i in range(fs.num_files()):
            if fs.file_flags(i) & lt.file_storage.flag_pad_file:
                continue  # BEP 47 alignment padding, never written to disk
            size = int(fs.file_size(i))
            out.append(
                ClientFile(
                    path=fs.file_path(i), size=size, progress=(done[i] / size) if size else 1.0
                )
            )
        return out

    async def set_limits(self, ref: ClientRef | None, limits: TransferLimits) -> None:
        if ref is None:
            self._s().apply_settings(
                {
                    "download_rate_limit": limits.download or 0,
                    "upload_rate_limit": limits.upload or 0,
                }
            )
            return
        h = self._handle(ref)
        h.set_download_limit(limits.download or -1)
        h.set_upload_limit(limits.upload or -1)

    async def pause(self, ref: ClientRef) -> None:
        self._resume_after_flush.discard(ref)  # stay paused after a finish-flush
        self._handle(ref).pause()

    async def resume(self, ref: ClientRef) -> None:
        h = self._handle(ref)
        self._errors.pop(ref, None)
        h.clear_error()
        h.resume()

    async def remove(self, ref: ClientRef, delete_data: bool) -> None:
        h = self._handles.pop(ref, None)
        self._errors.pop(ref, None)
        self._flushing.discard(ref)
        self._flushed.discard(ref)
        self._resume_after_flush.discard(ref)
        self._counters.pop((ref, "down"), None)
        self._counters.pop((ref, "up"), None)
        if h is not None and h.is_valid():
            self._s().remove_torrent(h, lt.options_t.delete_files if delete_data else 0)
        if self.state_dir is not None:
            (self.state_dir / f"{ref}.fastresume").unlink(missing_ok=True)

    async def recheck(self, ref: ClientRef) -> None:
        self._handle(ref).force_recheck()

    # ---- test helpers -------------------------------------------------------------------------

    def connect_peer(self, ref: ClientRef, host: str, port: int) -> None:
        """Point a torrent at a known peer (tests use this; DHT is off there). A single
        attempt can fail (seen on Windows over loopback), so it is retried every second
        until the torrent has finished, for up to a minute."""
        self._handle(ref).connect_peer((host, port))

        async def retry() -> None:
            for _ in range(60):
                await asyncio.sleep(1.0)
                h = self._handles.get(ref)
                if self._session is None or h is None or not h.is_valid():
                    return
                if h.status().is_seeding:
                    return
                h.connect_peer((host, port))

        with contextlib.suppress(RuntimeError):  # no running loop: the one attempt only
            self._tasks.append(asyncio.get_running_loop().create_task(retry()))


registry.register(BuiltinClient)
