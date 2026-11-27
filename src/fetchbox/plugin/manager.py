"""Installed plugins: found, approved, loaded at startup, configured, installed and removed.

Plugins are Python packages with a ``fetchbox.plugins`` entry point: installed into
Fetchbox's environment (pip), or from a wheel file into ``<data>/plugins/lib`` (Settings >
Plugins, which works in the desktop apps too). A plugin runs only once someone approved it:
one that turns up by itself (copied in by hand) stays off until approved.

Each plugin's settings are kept per contribution (an add-on's, a media kind's), with its
``Secret`` fields encrypted like every other secret of Fetchbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.metadata as md
import os
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from fetchbox.core.db import Database
from fetchbox.core.health import HealthRegistry
from fetchbox.core.providers import HealthStatus
from fetchbox.core.secrets import MASK, SecretBox, is_encrypted
from fetchbox.modules.plugins.models import PluginRecord
from fetchbox.modules.plugins.store import PluginStore
from fetchbox.plugin.host import PluginHost, PluginLoadError
from fetchbox.plugin.services import PLUGIN_MESSAGE
from fetchbox_plugin import Addon, MediaKind, Plugin, Secret, Settings
from fetchbox_plugin.manifest import PERMISSION_WORDS, SUPPORTED_APIS

log = structlog.get_logger(__name__)

GROUP = "fetchbox.plugins"
NATIVE = (".so", ".pyd", ".dylib", ".dll")
PANEL_SECONDS = 10
MAX_WHEEL = 50 * 1024 * 1024
DROP_EVERY = 30.0  # seconds between looks at the drop folder
DROP_SETTLE = 10.0  # a wheel unchanged this long has finished copying (a NAS share is slow)


class PluginError(Exception):
    """Something about installing or configuring a plugin, said plainly."""


@dataclass
class Found:
    """A plugin's package on this system (whether or not it runs)."""

    id: str
    entry: str  # module:Class
    dist: str | None
    version: str | None
    cls: type[Plugin] | None = None
    error: str | None = None  # couldn't be imported


@dataclass
class Part:
    """One contribution with settings: an add-on or a media kind."""

    id: str
    name: str
    kind: str  # addon, media_kind
    model: type[Settings] | None


@dataclass
class View:
    record: PluginRecord
    found: Found | None
    parts: list[Part] = field(default_factory=list)


def secret_fields(model: type[BaseModel]) -> list[str]:
    return [n for n, f in model.model_fields.items() if f.annotation is Secret]


def _move(f: Path, to: Path) -> None:
    """Out of the drop folder (replacing an older copy there), so it isn't installed again."""
    to.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.replace(f, to / f.name)


class PluginManager:
    def __init__(
        self,
        *,
        db: Database,
        box: SecretBox,
        host: PluginHost,
        data_dir: Path,
        enabled: bool,
        health: HealthRegistry | None = None,
    ) -> None:
        self.db = db
        self.box = box
        self.host = host
        self.lib = data_dir / "plugins" / "lib"  # wheels installed from Settings
        # .whl files copied here are installed (not approved: turned on in Settings)
        self.drop = data_dir / "plugins" / "drop"
        self.drop_problems: dict[str, str] = {}  # file name -> why it wasn't installed
        self._drop_seen: dict[str, tuple[int, float]] = {}
        self._drop_task: asyncio.Task[None] | None = None
        self.enabled = enabled  # False: started with --no-plugins
        self.found: dict[str, Found] = {}
        self.records: dict[str, PluginRecord] = {}
        self.health_registry = health
        host.settings_for = self._settings_for
        # "A message from a plugin" is offered to notification targets while one runs
        host.services.notifications.extra_events = lambda: (
            {PLUGIN_MESSAGE: "A message from a plugin"} if host.loaded else {}
        )

    # ---- discovery and startup -----------------------------------------------------------

    def _ensure_path(self) -> None:
        if self.lib.is_dir() and str(self.lib) not in sys.path:
            sys.path.append(str(self.lib))
        importlib.invalidate_caches()

    def discover(self) -> dict[str, Found]:
        self._ensure_path()
        found: dict[str, Found] = {}
        for ep in md.entry_points(group=GROUP):
            dist = ep.dist.name if ep.dist else None
            version = ep.dist.version if ep.dist else None
            f = Found(ep.name, ep.value, dist, version)
            try:
                cls = ep.load()
                if not (isinstance(cls, type) and issubclass(cls, Plugin)):
                    raise TypeError(f"{ep.value} isn't a Plugin class")
                f.cls, f.id = cls, getattr(cls, "id", ep.name)
            except Exception as exc:  # a broken plugin never stops Fetchbox
                f.error = f"couldn't be imported: {type(exc).__name__}: {exc}"
            found[f.id] = f
        self.found = found
        return found

    async def start(self) -> None:
        """At startup: note what's installed, run what's approved and turned on."""
        self.discover()
        if (spec := os.environ.get("FETCHBOX_PLUGINS", "").strip()) and self.enabled:
            await self.install_from_env(spec)
        async with self.db.sessionmaker() as s:
            rows = {r.id: r for r in (await s.execute(select(PluginRecord))).scalars()}
            for pid, f in self.found.items():
                if pid not in rows:
                    rows[pid] = PluginRecord(id=pid, approved=False, enabled=False, settings={})
                    s.add(rows[pid])
                rows[pid].version = f.version
            await s.commit()
            for r in rows.values():
                s.expunge(r)
        self.records = rows
        self._watch_health()
        if not self.enabled:
            log.warning("plugins.off", reason="started with --no-plugins")
            return
        for pid, r in rows.items():
            if r.approved and r.enabled:
                await self._load(pid)
        self.drop.mkdir(parents=True, exist_ok=True)
        await self.scan_drop(settle=0)  # what was copied there while Fetchbox was off
        self._drop_task = asyncio.create_task(self._watch_drop(), name="plugins-drop-folder")

    async def _watch_drop(self) -> None:
        while True:
            await asyncio.sleep(DROP_EVERY)
            try:
                await self.scan_drop()
            except Exception as exc:  # never stops watching
                log.warning("plugins.drop_scan_failed", error=str(exc))

    async def scan_drop(self, *, settle: float = DROP_SETTLE) -> list[str]:
        """Install each .whl in the drop folder whose copy has finished (unchanged for
        `settle` seconds), then move it to drop/installed/ (or drop/failed/, saying why in
        Health). A new plugin isn't approved: it's turned on in Settings > Plugins. A newer
        version of an installed one replaces it and stays as it was. The ids installed."""
        if not self.drop.is_dir():
            return []
        done = []
        now = time.time()
        for f in sorted(self.drop.glob("*.whl")):
            try:
                st = f.stat()
            except OSError:
                continue  # gone meanwhile
            sig = (st.st_size, st.st_mtime)
            if settle and (self._drop_seen.get(f.name) != sig or now - st.st_mtime < settle):
                self._drop_seen[f.name] = sig  # still being copied, or just arrived
                continue
            self._drop_seen.pop(f.name, None)
            try:
                if st.st_size > MAX_WHEEL:
                    raise PluginError("a plugin wheel is at most 50 MB")
                pid = await self.install_wheel(f.read_bytes(), f.name, approve=False)
            except PluginError as exc:
                self.drop_problems[f.name] = str(exc)
                _move(f, self.drop / "failed")
                log.warning("plugins.drop_failed", file=f.name, error=self.drop_problems[f.name])
                continue
            self.drop_problems.pop(f.name, None)
            _move(f, self.drop / "installed")
            log.info("plugins.drop_installed", file=f.name, plugin=pid)
            done.append(pid)
        return done

    async def stop(self) -> None:
        if self._drop_task is not None:
            self._drop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drop_task
        for pid in list(self.host.loaded):
            await self.host.unload(pid)

    async def _load(self, pid: str) -> None:
        f = self.found.get(pid)
        error: str | None = None
        if f is None:
            error = "not installed any more"
        elif f.cls is None:
            error = f.error
        else:
            try:
                await self.host.load(f.cls)
            except PluginLoadError as exc:
                error = str(exc)
            except Exception as exc:
                error = f"failed to start: {type(exc).__name__}: {exc}"
                log.exception("plugin.load_failed", plugin=pid)
        await self._set(pid, error=error)

    def _watch_health(self) -> None:
        """Health shows the plugins once there are any (nothing new before)."""
        if self.health_registry is not None and self.records:
            self.health_registry.register("plugins", self.health, fix_url="/settings/plugins")

    async def health(self) -> HealthStatus:
        failing = [
            f"{self.describe(pid)['name']}: {r.error}"
            for pid, r in self.records.items()
            if r.approved and r.enabled and pid not in self.host.loaded and r.error
        ] + [
            f"{got.plugin.name} is paused for an hour after failing repeatedly: {got.last_error}"
            for got in self.host.loaded.values() if got.paused()
        ]  # fmt: skip
        failing += [
            f"{name} in the drop folder couldn't be installed: {why}"
            for name, why in self.drop_problems.items()
        ]
        if not self.enabled:
            return HealthStatus(status="warning", message="Plugins are off (--no-plugins)")
        if failing:
            return HealthStatus(status="warning", message="; ".join(failing))
        running = len(self.host.loaded)
        return HealthStatus(status="ok", message=f"{running} plugin(s) running")

    async def _set(self, pid: str, **values: Any) -> PluginRecord:
        async with self.db.sessionmaker() as s:
            row = await s.get(PluginRecord, pid)
            if row is None:
                row = PluginRecord(id=pid, approved=False, enabled=False, settings={})
                s.add(row)
            for k, v in values.items():
                setattr(row, k, v)
            await s.commit()
            await s.refresh(row)
            s.expunge(row)
        self.records[pid] = row
        self._watch_health()
        return row

    # ---- settings --------------------------------------------------------------------------

    def parts(self, pid: str) -> list[Part]:
        f = self.found.get(pid)
        out = []
        for c in (f.cls.contributes if f and f.cls else []) or []:
            if isinstance(c, type) and issubclass(c, Addon):
                out.append(Part(c.id, c.name, "addon", c.settings_model))
            elif isinstance(c, type) and issubclass(c, MediaKind):
                out.append(Part(c.id, c.name, "media_kind", c.settings_model))
        return out

    def _part(self, pid: str, part: str) -> Part:
        for p in self.parts(pid):
            if p.id == part:
                return p
        raise PluginError(f"{pid} has no {part}")

    def _settings_for(self, pid: str, part: str) -> dict[str, Any]:
        """For the host: a contribution's stored settings, Secrets decrypted."""
        stored = dict((self.records.get(pid) or PluginRecord(settings={})).settings or {})
        data = dict(stored.get(part) or {})
        for k, v in data.items():
            if is_encrypted(v):
                data[k] = self.box.decrypt(v, aad=f"plugin:{pid}:{part}:{k}")
        return data

    def masked(self, pid: str, part: str) -> dict[str, Any]:
        """For the browser: Secrets as MASK when set, never their values."""
        p = self._part(pid, part)
        data = dict(((self.records.get(pid) and self.records[pid].settings) or {}).get(part) or {})
        for k in secret_fields(p.model) if p.model else []:
            data[k] = MASK if data.get(k) else None
        return data

    async def save_settings(self, pid: str, part: str, values: dict[str, Any]) -> list[str]:
        """Validate and keep a contribution's settings; the plugin runs with them at once.
        Sending MASK back for a Secret keeps it. The problems, empty when valid."""
        p = self._part(pid, part)
        if p.model is None:
            raise PluginError(f"{p.name} has no settings")
        current = self._settings_for(pid, part)
        merged = {**current, **values}
        for k in secret_fields(p.model):
            if values.get(k) == MASK:
                merged[k] = current.get(k)
        try:
            model = p.model.model_validate(merged)
        except ValidationError as exc:
            return [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        data = model.model_dump(mode="json")
        for k in secret_fields(p.model):
            if data.get(k):
                data[k] = self.box.encrypt(str(data[k]), aad=f"plugin:{pid}:{part}:{k}")
        rec = self.records.get(pid)
        stored = dict((rec.settings if rec else {}) or {})
        stored[part] = data
        await self._set(pid, settings=stored)
        await self.reload(pid)
        return []

    async def test_settings(self, pid: str, part: str, values: dict[str, Any]) -> tuple[bool, str]:
        """The Test button: the settings as typed (not saved yet), Secrets kept as MASK."""
        p = self._part(pid, part)
        if p.model is None:
            raise PluginError(f"{p.name} has no settings")
        current = self._settings_for(pid, part)
        merged = {**current, **values}
        for k in secret_fields(p.model):
            if values.get(k) == MASK:
                merged[k] = current.get(k)
        try:
            model = p.model.model_validate(merged)
        except ValidationError as exc:
            return False, "; ".join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors())
        if type(model).test is Settings.test:
            return False, "this plugin has no test"
        got = self.host.loaded.get(pid)
        ctx = got.ctx if got else None
        if ctx is None:
            from fetchbox.plugin.context import PluginContext

            ctx = PluginContext(
                pid, db=self.db, data_dir=self.host.data_dir, services=self.host.services
            )
        try:
            return True, str(await model.test(ctx))
        except Exception as exc:  # the plugin's message for what didn't work
            return False, str(exc) or type(exc).__name__
        finally:
            if got is None:
                await ctx.aclose()

    # ---- turning on and off --------------------------------------------------------------

    async def set_enabled(self, pid: str, enabled: bool) -> PluginRecord:
        if pid not in self.found and enabled:
            raise PluginError(f"{pid} isn't installed")
        if enabled:
            rec = await self._set(pid, approved=True, enabled=True, error=None)
            if self.enabled and pid not in self.host.loaded:
                await self._load(pid)
            return self.records[pid]
        await self.host.unload(pid)
        rec = await self._set(pid, enabled=False, error=None)
        return rec

    async def reload(self, pid: str) -> None:
        rec = self.records.get(pid)
        if pid in self.host.loaded:
            await self.host.unload(pid)
        if rec and rec.approved and rec.enabled and self.enabled:
            await self._load(pid)

    # ---- installing and removing ------------------------------------------------------------

    async def install_wheel(self, data: bytes, filename: str, *, approve: bool = True) -> str:
        """A plugin from a wheel file: unpacked into the plugins folder, approved (it's being
        installed on purpose) and turned on. Its id. With approve=False (the drop folder) a
        new plugin waits to be turned on in Settings; a newer version of one already there
        keeps its state."""
        import io

        if not filename.endswith(".whl"):
            raise PluginError("a plugin comes as a .whl file (uv build makes one)")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise PluginError(f"{filename} isn't a wheel file") from None
        names = zf.namelist()
        bad = [n for n in names if n.startswith("/") or ".." in PurePosixPath(n).parts]
        if bad:
            raise PluginError(f"{filename} writes outside its folder: {bad[0]}")
        if native := [n for n in names if n.lower().endswith(NATIVE)]:
            raise PluginError(f"{filename} has native code ({native[0]}); plugins are Python only")
        eps = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
        text = zf.read(eps[0]).decode("utf-8", "replace") if eps else ""
        if f"[{GROUP}]" not in text:
            raise PluginError(f"{filename} isn't a Fetchbox plugin (no {GROUP} entry point)")
        top = {PurePosixPath(n).parts[0] for n in names}
        for t in top:  # an older version of it: replaced
            shutil.rmtree(self.lib / t, ignore_errors=True)
        # and its older versions' records ("name-0.1.0.dist-info" isn't among a 0.2.0 wheel's
        # folders): left there, either could be read as the installed version
        dists = {_norm(t.split("-")[0]) for t in top if t.endswith(".dist-info")}
        if self.lib.is_dir():
            for old in self.lib.glob("*.dist-info"):
                if old.name not in top and _norm(old.name.split("-")[0]) in dists:
                    shutil.rmtree(old, ignore_errors=True)
        self.lib.mkdir(parents=True, exist_ok=True)
        zf.extractall(self.lib)
        for mod in [m for m in sys.modules if any(m == t or m.startswith(t + ".") for t in top)]:
            del sys.modules[mod]  # the new code, not the old
        before = set(self.found)
        self.discover()
        new = [
            pid
            for pid, f in self.found.items()
            if f.dist and _wheel_dist(filename) == _norm(f.dist)
        ]
        pid = new[0] if new else next(iter(set(self.found) - before), None)
        if pid is None:
            raise PluginError(f"{filename} was unpacked, but no plugin in it could be found")
        if pid in self.host.loaded:
            await self.host.unload(pid)
        known = self.records.get(pid)
        if approve:
            approved, enabled = True, True
        else:  # dropped: as it was, or waiting to be turned on
            approved = bool(known and known.approved)
            enabled = bool(known and known.enabled)
        await self._set(
            pid,
            approved=approved,
            enabled=enabled,
            source="file",
            version=self.found[pid].version,
            error=self.found[pid].error,
        )
        if self.enabled and enabled and self.found[pid].cls is not None:
            await self._load(pid)
        return pid

    async def install_url(self, url: str, *, transport: Any = None) -> str:
        """A plugin's wheel from an address: fetched, then installed as from a file."""
        import httpx

        if not url.startswith(("https://", "http://")):
            raise PluginError("a plugin's address starts with https:// (or http://)")
        try:
            client = httpx.AsyncClient(timeout=60, follow_redirects=True, transport=transport)
            async with client as http, http.stream("GET", url) as r:
                if r.status_code != 200:
                    raise PluginError(f"{url}: the server answered {r.status_code}")
                data = b""
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_WHEEL:
                        raise PluginError("a plugin wheel is at most 50 MB")
        except httpx.HTTPError as exc:
            raise PluginError(f"{url}: couldn't be fetched ({exc})") from None
        name = url.split("?")[0].rsplit("/", 1)[-1] or "plugin.whl"
        return await self.install_wheel(data, name)

    async def install_from_env(self, spec: str, *, transport: Any = None) -> list[str]:
        """FETCHBOX_PLUGINS (Docker): "fetchbox-comics,fetchbox-subtitles==1.2.0", or wheel
        addresses. Each one not installed yet (or of another version) is fetched from PyPI
        and installed, approved by being listed; once installed it's simply kept."""
        import httpx

        done = []
        for raw in [x.strip() for x in spec.split(",") if x.strip()]:
            try:
                if raw.startswith(("https://", "http://")):
                    done.append(await self.install_url(raw, transport=transport))
                    continue
                name, _, want = raw.partition("==")
                have = next(
                    (f for f in self.found.values() if f.dist and _norm(f.dist) == _norm(name)),
                    None,
                )
                if have is not None and (not want or have.version == want):
                    continue  # there already
                api = f"https://pypi.org/pypi/{name}/{want + '/' if want else ''}json"
                async with httpx.AsyncClient(timeout=30, transport=transport) as http:
                    r = await http.get(api)
                if r.status_code != 200:
                    raise PluginError(f"{name}: not found on PyPI ({r.status_code})")
                wheels = [
                    u
                    for u in r.json().get("urls") or []
                    if u.get("packagetype") == "bdist_wheel"
                    and u.get("filename", "").endswith("-none-any.whl")
                ]
                if not wheels:
                    raise PluginError(f"{name}: no pure-Python wheel on PyPI")
                done.append(await self.install_url(wheels[0]["url"], transport=transport))
            except PluginError as exc:
                log.warning("plugins.env_install_failed", plugin=raw, error=str(exc))
        return done

    async def remove(self, pid: str, *, keep_settings: bool) -> None:
        """Turn it off for good: unloaded; its files deleted when installed from a file;
        its settings and stored data kept or not."""
        await self.host.unload(pid)
        f = self.found.get(pid)
        if f and f.dist and self.lib.is_dir():
            for d in self.lib.glob("*.dist-info"):
                if _norm(d.name.split("-")[0]) == _norm(f.dist):
                    record = d / "RECORD"
                    lines = record.read_text("utf-8").splitlines() if record.exists() else []
                    tops = set()
                    for line in lines:
                        rel = line.split(",")[0]
                        if rel and ".." not in rel and not rel.startswith("/"):
                            (self.lib / rel).unlink(missing_ok=True)
                            tops.add(PurePosixPath(rel).parts[0])
                    for top in tops:  # its packages, with what importing them left (caches)
                        target = (self.lib / top).resolve()
                        if target.is_dir() and target.parent == self.lib.resolve():
                            shutil.rmtree(target, ignore_errors=True)
                    shutil.rmtree(d, ignore_errors=True)
            for empty in sorted((p for p in self.lib.rglob("*") if p.is_dir()), reverse=True):
                if not any(empty.iterdir()):
                    empty.rmdir()
        self.found.pop(pid, None)
        if keep_settings:
            await self._set(pid, approved=False, enabled=False, error="removed")
            return
        await PluginStore(self.db, pid).clear()
        async with self.db.sessionmaker() as s:
            row = await s.get(PluginRecord, pid)
            if row is not None:
                await s.delete(row)
                await s.commit()
        self.records.pop(pid, None)

    # ---- panels on the title page ---------------------------------------------------------

    async def panels(self, on: str, target: str) -> list[dict[str, Any]]:
        """Each running add-on's panel for a title (or an item): what it says, or why not."""
        import asyncio

        kind: str | None = None
        try:
            media = self.host.services.media
            if on == "title":
                kind = (await media.get_title(target)).kind
            elif on == "item":
                kind = (await media.get_title((await media.get_wanted(target)).title_id)).kind
        except Exception:  # an unknown title: no panels
            return []
        out = []
        for pid, got in list(self.host.loaded.items()):
            for addon_id, addon in got.addons.items():
                if addon.applies_to and kind not in addon.applies_to:
                    continue
                for method, spec in type(addon).handlers()["panel"]:
                    if spec.level != on:
                        continue
                    row: dict[str, Any] = {
                        "plugin": pid, "addon": addon_id, "method": method, "title": spec.title,
                        "status": "", "table": [], "actions": [], "error": None,
                    }  # fmt: skip
                    try:
                        async with asyncio.timeout(PANEL_SECONDS):
                            panel = await getattr(addon, method)(got.ctx, target)
                        row |= {"status": panel.status, "table": list(panel.table)[:200],
                                "actions": list(panel.actions)}  # fmt: skip
                    except TimeoutError:
                        row["error"] = f"didn't answer in {PANEL_SECONDS} s"
                    except Exception as exc:  # the plugin's failure, shown in its panel
                        row["error"] = f"{type(exc).__name__}: {exc}"
                        log.warning("plugin.panel_failed", plugin=pid, error=str(exc))
                    out.append(row)
        return out

    # ---- what the API shows ----------------------------------------------------------------

    def describe(self, pid: str) -> dict[str, Any]:
        rec = self.records.get(pid)
        f = self.found.get(pid)
        cls = f.cls if f else None
        loaded = self.host.loaded.get(pid)
        parts = []
        for p in self.parts(pid):
            schema = p.model.model_json_schema() if p.model else None
            missing = []
            if p.model:
                try:
                    p.model.model_validate(self._settings_for(pid, p.id))
                except ValidationError as exc:
                    missing = [str(e["loc"][0]) for e in exc.errors() if e["loc"]]
                except Exception:
                    missing = []
            parts.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "kind": p.kind,
                    "schema": schema,
                    "values": self.masked(pid, p.id) if p.model else {},
                    "missing": missing,
                    "has_test": bool(p.model and p.model.test is not Settings.test),
                }
            )
        actions = []
        if loaded:
            for (addon, method), _job in loaded.actions.items():
                spec = getattr(
                    getattr(type(loaded.addons[addon]), method), "__fetchbox_action__", None
                )
                if spec:
                    actions.append(
                        {"addon": addon, "method": method, "label": spec.label, "on": spec.level}
                    )
        return {
            "id": pid,
            "name": getattr(cls, "name", pid),
            "description": getattr(cls, "description", ""),
            "version": (rec.version if rec else None) or (f.version if f else None),
            "homepage": getattr(cls, "homepage", None),
            "api": getattr(cls, "fetchbox_api", None),
            "api_supported": getattr(cls, "fetchbox_api", None) in SUPPORTED_APIS,
            "permissions": [
                {"id": str(p), "words": PERMISSION_WORDS.get(p, str(p))}
                for p in sorted(getattr(cls, "permissions", ()) or (), key=str)
            ],
            "contributes": [
                {
                    "id": getattr(c, "id", c.__name__),
                    "name": getattr(c, "name", c.__name__),
                    "kind": "media_kind"
                    if isinstance(c, type) and issubclass(c, MediaKind)
                    else "addon"
                    if isinstance(c, type) and issubclass(c, Addon)
                    else "provider",
                }
                for c in (getattr(cls, "contributes", None) or [])
            ],
            "source": rec.source if rec else "installed",
            "approved": bool(rec and rec.approved),
            "enabled": bool(rec and rec.enabled),
            "running": loaded is not None,
            "paused_until": (
                datetime.fromtimestamp(loaded.paused_until, UTC)
                if loaded is not None and loaded.paused() and loaded.paused_until
                else None
            ),
            "last_error": loaded.last_error if loaded is not None else None,
            "error": (rec.error if rec else None) or (f.error if f else "not installed any more"),
            "settings": parts,
            "actions": actions,
        }

    def all(self) -> list[dict[str, Any]]:
        ids = sorted(set(self.records) | set(self.found))
        return [self.describe(pid) for pid in ids]


def _norm(name: str) -> str:
    return name.replace("-", "_").replace(".", "_").lower()


def _wheel_dist(filename: str) -> str:
    return _norm(Path(filename).name.split("-")[0])
