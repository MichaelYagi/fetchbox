"""``fetchbox plugin check <path>``: a plugin checked against API 1, with how to fix each problem.

It reads the project's pyproject.toml, imports the plugin (declaration mistakes surface
then, with their own messages), checks the manifest and what it contributes, and reads the
source to compare the permissions declared with what the code uses and to flag imports of
Fetchbox's internals. Also runs without Fetchbox: ``python -m fetchbox_plugin check <path>``.
"""

from __future__ import annotations

import ast
import importlib
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fetchbox_plugin.addon import Addon
from fetchbox_plugin.kind import MediaKind
from fetchbox_plugin.manifest import PLUGIN_ID, SUPPORTED_APIS, Permission, Plugin
from fetchbox_plugin.settings import Settings

ENTRY_POINT = "fetchbox.plugins"
API_PACKAGE = "fetchbox-plugin-api"

# What in the code means a permission is used
CTX_USES: dict[str, Permission] = {
    "http": Permission.NETWORK,
    "files": Permission.LIBRARY_FILES,
    "notify": Permission.NOTIFY,
}
LIBRARY_CHANGES = {"wanted", "search"}  # ctx.library.<these> change what's wanted
NETWORK_MODULES = {"httpx", "requests", "aiohttp", "urllib", "http", "socket", "websockets"}


@dataclass(frozen=True)
class Problem:
    level: Literal["error", "warning"]
    where: str  # "pyproject.toml", "Comics", "fetchbox_comics/kind.py:12"
    message: str
    fix: str = ""


@dataclass
class Report:
    plugin: str | None = None  # the plugin's id, once found
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(p.level == "error" for p in self.problems)

    def error(self, where: str, message: str, fix: str = "") -> None:
        self.problems.append(Problem("error", where, message, fix))

    def warn(self, where: str, message: str, fix: str = "") -> None:
        self.problems.append(Problem("warning", where, message, fix))

    def text(self) -> str:
        lines = []
        for p in self.problems:
            lines.append(f"{p.level}: {p.where}: {p.message}")
            if p.fix:
                lines.extend(f"    fix: {line}" for line in p.fix.splitlines())
        errors = sum(p.level == "error" for p in self.problems)
        warnings = len(self.problems) - errors
        name = self.plugin or "the plugin"
        if not self.problems:
            lines.append(f"{name}: no problems found (API {max(SUPPORTED_APIS)})")
        else:
            lines.append(f"{name}: {errors} error(s), {warnings} warning(s)")
        return "\n".join(lines)


def check_plugin(path: str | Path) -> Report:
    report = Report()
    root = Path(path).resolve()
    project = root / "pyproject.toml"
    if not project.is_file():
        report.error(str(root), "no pyproject.toml here", "run it on the plugin's project folder")
        return report
    try:
        meta = tomllib.loads(project.read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        report.error("pyproject.toml", f"can't be read: {exc}")
        return report
    target = _project(meta, report)
    if target is None:
        return report
    key, module, attr = target
    cls = _load(root, module, attr, report)
    if cls is None:
        return report
    report.plugin = getattr(cls, "id", None)
    _manifest(cls, key, report)
    _contributions(cls, report)
    _source(root, module, cls, report)
    return report


def _project(meta: dict, report: Report) -> tuple[str, str, str] | None:  # type: ignore[type-arg]
    proj = meta.get("project") or {}
    name = str(proj.get("name") or "")
    if name and not name.startswith("fetchbox-"):
        report.warn(
            "pyproject.toml",
            f"the package is {name!r}",
            f'name plugins fetchbox-<name>, like "fetchbox-{name}"',
        )
    deps = [str(d) for d in proj.get("dependencies") or []]
    if not any(d.replace("_", "-").lower().startswith(API_PACKAGE) for d in deps):
        report.error(
            "pyproject.toml",
            f"{API_PACKAGE} isn't a dependency",
            f'dependencies = ["{API_PACKAGE}>=1,<2"]',
        )
    eps = (proj.get("entry-points") or {}).get(ENTRY_POINT) or {}
    if not eps:
        report.error(
            "pyproject.toml",
            f"no {ENTRY_POINT} entry point: Fetchbox won't find it",
            f'[project.entry-points."{ENTRY_POINT}"]\n<id> = "<package>:<Plugin class>"',
        )
        return None
    if len(eps) > 1:
        report.error(
            "pyproject.toml",
            f"{len(eps)} {ENTRY_POINT} entry points",
            "one package is one plugin: list everything in its contributes",
        )
    key, value = next(iter(eps.items()))
    module, _, attr = str(value).partition(":")
    if not module or not attr:
        report.error(
            "pyproject.toml",
            f"entry point {key} = {value!r}",
            'it names the Plugin class: "fetchbox_comics:Comics"',
        )
        return None
    return str(key), module.strip(), attr.strip()


def _load(root: Path, module: str, attr: str, report: Report) -> type | None:
    where = next((d for d in (root / "src", root) if (d / module.split(".")[0]).exists()), None)
    if where is None:
        report.error("pyproject.toml", f"the entry point's module {module} isn't in src/ or here")
        return None
    sys.path.insert(0, str(where))
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # a declaration mistake, or any error importing it
        report.error(module, f"importing it failed: {type(exc).__name__}: {exc}")
        return None
    finally:
        sys.path.remove(str(where))
    cls = getattr(mod, attr, None)
    if not (isinstance(cls, type) and issubclass(cls, Plugin)):
        report.error(
            f"{module}:{attr}",
            "isn't a Plugin class",
            "class Comics(Plugin): ... and point the entry point at it",
        )
        return None
    return cls


def _manifest(cls: type, key: str, report: Report) -> None:
    where = cls.__name__
    pid = getattr(cls, "id", None)
    if not isinstance(pid, str) or not PLUGIN_ID.match(pid):
        report.error(
            where, f"id {pid!r}", "2 to 40 lowercase letters, digits, - or _, from a letter"
        )
    elif pid != key:
        report.error(
            "pyproject.toml",
            f"the entry point is {key!r} but the plugin's id is {pid!r}",
            f'{pid} = "..."  (the same as the id)',
        )
    if not str(getattr(cls, "name", "") or "").strip():
        report.error(where, "no name", 'name = "Comics"  (what people see)')
    if not str(getattr(cls, "description", "") or "").strip():
        report.warn(where, "no description", "one sentence, shown before it's installed")
    api = getattr(cls, "fetchbox_api", None)
    if api not in SUPPORTED_APIS:
        report.error(
            where,
            f"fetchbox_api = {api!r}: this API is {sorted(SUPPORTED_APIS)}",
            "fetchbox_api = 1",
        )
    perms: set[object] = set(getattr(cls, "permissions", set()) or set())
    bad = [p for p in perms if not isinstance(p, Permission)]
    if bad:
        report.error(
            where,
            f"permissions: {bad} aren't Permission values",
            "permissions = {Permission.NETWORK, ...}",
        )
    if not getattr(cls, "contributes", None):
        report.error(
            where,
            "contributes nothing",
            "contributes = [YourKind, YourAddon]  (media kinds, add-ons, providers)",
        )


def _contributions(cls: type, report: Report) -> None:
    seen: dict[str, str] = {}
    for c in getattr(cls, "contributes", None) or []:
        name = getattr(c, "__name__", repr(c))
        if not isinstance(c, type):
            report.error(
                cls.__name__,
                f"contributes {name}: a class, not an instance",
                f"contributes = [{type(c).__name__}]",
            )
            continue
        if issubclass(c, MediaKind):
            what = "media kind"
            model = getattr(c, "settings", None)
        elif issubclass(c, Addon):
            what = "add-on"
            model = c.settings_model
            if not str(getattr(c, "name", "") or "").strip():
                report.error(name, "no name", 'name = "Subtitles"')
            if not c.handlers()["on"] and not c.handlers()["action"] and not c.handlers()["every"]:
                report.warn(name, "has no @on, @action or @every: it never runs")
        elif hasattr(c, "Settings") and hasattr(c, "test"):
            continue  # a provider (indexer, notifier...): Fetchbox's provider contract
        else:
            report.error(
                name,
                "isn't a MediaKind, an Addon or a provider",
                "subclass MediaKind or Addon from fetchbox_plugin",
            )
            continue
        cid = getattr(c, "id", None)
        if not isinstance(cid, str) or not cid:
            report.error(name, f"the {what} has no id")
        elif cid in seen:
            report.error(
                name, f"id {cid!r} is also {seen[cid]}'s", "each contribution has its own id"
            )
        else:
            seen[cid] = name
        if model is not None and not (isinstance(model, type) and issubclass(model, Settings)):
            report.error(
                name,
                "settings isn't a Settings model",
                "class MySettings(Settings): ...  then settings = MySettings",
            )


def _source(root: Path, module: str, cls: type, report: Report) -> None:
    top = module.split(".")[0]
    pkg = next((d / top for d in (root / "src", root) if (d / top).exists()), None)
    if pkg is None:
        return
    files = sorted(pkg.rglob("*.py")) if pkg.is_dir() else [pkg.with_suffix(".py")]
    used: dict[Permission, str] = {}
    for f in files:
        rel = f.relative_to(pkg.parent).as_posix()
        try:
            tree = ast.parse(f.read_text("utf-8"), filename=rel)
        except (OSError, SyntaxError) as exc:
            report.error(rel, f"can't be read: {exc}")
            continue
        for node in ast.walk(tree):
            line = getattr(node, "lineno", 0)
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                mods = [node.module]
            for m in mods:
                head = m.split(".")[0]
                if (
                    head == "fetchbox"
                    and m != "fetchbox.plugin"
                    and not m.startswith("fetchbox.plugin.")
                ):
                    report.warn(
                        f"{rel}:{line}",
                        f"imports {m}: Fetchbox's internals, which may change in any release",
                        "use what fetchbox_plugin and ctx give you",
                    )
                if head in NETWORK_MODULES:
                    used.setdefault(Permission.NETWORK, f"{rel}:{line} (import {m})")
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
                inner = node.value
                if isinstance(inner.value, ast.Name) and inner.value.id == "ctx":
                    if inner.attr in CTX_USES:
                        used.setdefault(
                            CTX_USES[inner.attr], f"{rel}:{node.lineno} (ctx.{inner.attr})"
                        )
                    if inner.attr == "library" and node.attr in LIBRARY_CHANGES:
                        used.setdefault(
                            Permission.LIBRARY, f"{rel}:{node.lineno} (ctx.library.{node.attr})"
                        )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "notify"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ctx"
            ):
                used.setdefault(Permission.NOTIFY, f"{rel}:{node.lineno} (ctx.notify)")
    declared = set(getattr(cls, "permissions", set()) or set())
    for perm, at in sorted(used.items()):
        if perm not in declared:
            report.error(
                at,
                f"uses {perm.name} but doesn't declare it",
                f"permissions = {{..., Permission.{perm.name}}}",
            )
    for perm in sorted(declared - set(used), key=str):
        if isinstance(perm, Permission):
            report.warn(
                cls.__name__,
                f"declares {perm.name} but the code doesn't seem to use it",
                "remove it, so the install screen asks only for what's needed",
            )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["check"]:
        args = args[1:]
    if len(args) != 1:
        print("usage: python -m fetchbox_plugin check <plugin project folder>", file=sys.stderr)
        return 2
    report = check_plugin(args[0])
    print(report.text())
    return 0 if report.ok else 1
