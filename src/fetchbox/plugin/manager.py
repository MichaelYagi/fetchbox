"""Installed plugins: found, approved, loaded at startup, configured, installed and removed.

Plugins are Python packages with a ``fetchbox.plugins`` entry point: installed into
Fetchbox's environment (pip), or from a wheel file into ``<data>/plugins/lib`` (Settings >
Plugins, which works in the desktop apps too). A plugin runs only once someone approved it:
one that turns up by itself (copied in by hand) stays off until approved.

Each plugin's settings are kept per contribution (an add-on's, a media kind's), with its
``Secret`` fields encrypted like every other secret of Fetchbox.
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from fetchbox.core.db import Database
from fetchbox.core.secrets import MASK, SecretBox, is_encrypted
from fetchbox.modules.plugins.models import PluginRecord
from fetchbox.modules.plugins.store import PluginStore
from fetchbox.plugin.host import PluginHost, PluginLoadError
from fetchbox_plugin import Addon, MediaKind, Plugin, Secret, Settings
from fetchbox_plugin.manifest import PERMISSION_WORDS, SUPPORTED_APIS

log = structlog.get_logger(__name__)

GROUP = "fetchbox.plugins"
NATIVE = (".so", ".pyd", ".dylib", ".dll")


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


class PluginManager:
    def __init__(
        self, *, db: Database, box: SecretBox, host: PluginHost, data_dir: Path, enabled: bool
    ) -> None:
        self.db = db
        self.box = box
        self.host = host
        self.lib = data_dir / "plugins" / "lib"  # wheels installed from Settings
        self.enabled = enabled  # False: started with --no-plugins
        self.found: dict[str, Found] = {}
        self.records: dict[str, PluginRecord] = {}
        host.settings_for = self._settings_for

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
        if not self.enabled:
            log.warning("plugins.off", reason="started with --no-plugins")
            return
        for pid, r in rows.items():
            if r.approved and r.enabled:
                await self._load(pid)

    async def stop(self) -> None:
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

    async def install_wheel(self, data: bytes, filename: str) -> str:
        """A plugin from a wheel file: unpacked into the plugins folder, approved (it's being
        installed on purpose) and turned on. Its id."""
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
        await self._set(
            pid,
            approved=True,
            enabled=True,
            source="file",
            version=self.found[pid].version,
            error=self.found[pid].error,
        )
        if self.enabled and self.found[pid].cls is not None:
            await self._load(pid)
        return pid

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
