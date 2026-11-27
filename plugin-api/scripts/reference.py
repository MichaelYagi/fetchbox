"""Write docs/plugin-api.md, the plugin API reference, from the API itself: every name in
``fetchbox_plugin.__all__`` with its signature, fields and docstrings. Run after changing the
API (``uv run python plugin-api/scripts/reference.py``); a test fails while it's stale."""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from pathlib import Path

import fetchbox_plugin as api
import fetchbox_plugin.context as ctx_module

OUT = Path(__file__).resolve().parents[2] / "docs" / "plugin-api.md"

GROUPS = {
    "The plugin": ["Plugin", "Permission", "PERMISSION_WORDS", "API_VERSION"],
    "Add-ons": ["Addon", "on", "action", "every", "panel", "Panel", "PublicEvent", "PUBLIC_EVENTS"],
    "Media kinds": [
        "MediaKind",
        "Hit",
        "TitleDetails",
        "Group",
        "Item",
        "Query",
        "Parsed",
        "File",
        "Qualities",
        "Naming",
        "KindDeclarationError",
    ],
    "Settings": ["Settings", "Field", "Secret", "Url", "Folder"],
    "What ctx gives you": ["Context", "TitleInfo", "ItemInfo", "FileInfo", "SidecarInfo"],
    "Errors": ["NotFound", "Refused"],
}


CONTEXT_PARTS = ["Http", "Cache", "Store", "Files", "Library", "Jobs", "Job", "Notify"]


def _doc(obj: object) -> str:
    """Its own docstring (not one inherited, like Protocol's)."""
    own = obj.__dict__.get("__doc__") if inspect.isclass(obj) else getattr(obj, "__doc__", None)
    return inspect.cleandoc(own or "")


def _sig(obj: object) -> str:
    try:
        sig = str(inspect.signature(obj))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "(...)"
    sig = sig.replace("'", "")  # annotations are strings (from __future__ import annotations)
    for first in ("(self, ", "(cls, "):
        sig = sig.replace(first, "(", 1) if sig.startswith(first) else sig
    return "()" + sig[len("(self)") :] if sig.startswith(("(self)", "(cls) ")) else sig


def _fields(cls: type) -> list[str]:
    """The class's own declared attributes, as written (with their comments)."""
    try:
        src = textwrap.dedent(inspect.getsource(cls))
    except (OSError, TypeError):
        return []
    node = ast.parse(src).body[0]
    lines = src.splitlines()
    out = []
    for stmt in getattr(node, "body", []):
        if isinstance(stmt, ast.AnnAssign | ast.Assign):
            text = "\n".join(lines[stmt.lineno - 1 : stmt.end_lineno])
            name = text.split(":")[0].split("=")[0].strip()
            if not name.startswith("_") and "# fmt" not in text:
                out.append(textwrap.dedent(text).strip())
    return out


def _methods(cls: type) -> list[tuple[str, object]]:
    out = []
    for name, v in vars(cls).items():
        if name.startswith("_"):
            continue
        fn = v.__func__ if isinstance(v, classmethod | staticmethod) else v
        if inspect.isfunction(fn) or isinstance(v, property):
            out.append((name, v))
    return out


def _entry(name: str, obj: object = None) -> list[str]:
    obj = getattr(api, name) if obj is None else obj
    out = [f"### `{name}`", ""]
    if inspect.isclass(obj):
        bases = [b.__name__ for b in obj.__bases__ if b is not object]
        out += [f"`class {obj.__name__}" + (f"({', '.join(bases)})`" if bases else "`"), ""]
        if doc := _doc(obj):
            out += [doc, ""]
        if fields := _fields(obj):
            out += ["```python", *fields, "```", ""]
        for mname, m in _methods(obj):
            if isinstance(m, property):
                ret = inspect.signature(m.fget).return_annotation if m.fget else ""
                ret = str(ret).replace("'", "") if ret is not inspect.Signature.empty else ""
                doc = _doc(m.fget) if m.fget else ""
                out += [f"- **`{mname}`**" + (f" → `{ret}`" if ret else "")
                        + (f": {doc}" if doc else "")]  # fmt: skip
                continue
            fn = m.__func__ if isinstance(m, classmethod | staticmethod) else m
            prefix = "async " if inspect.iscoroutinefunction(fn) else ""
            doc = _doc(fn).replace("\n", " ")
            out += [f"- **`{prefix}{mname}{_sig(fn)}`**" + (f": {doc}" if doc else "")]
        if _methods(obj):
            out.append("")
    elif callable(obj):
        out += [f"```python\n{name}{_sig(obj)}\n```", ""]
        if doc := _doc(obj):
            out += [doc, ""]
    elif isinstance(obj, dict):
        out += [f"`{name}`: a dict", ""]
        out += [f"- `{getattr(k, 'value', k)}`: {v}" for k, v in obj.items()] + [""]
    elif isinstance(obj, frozenset | set | tuple | list):
        out += [f"`{name}`:", ""] + [f"- `{x}`" for x in sorted(map(str, obj))] + [""]
    else:
        out += [f"`{name} = {obj!r}`", ""]
    return out


def render() -> str:
    listed = [n for names in GROUPS.values() for n in names]
    missing = sorted(set(api.__all__) - set(listed))
    if missing:
        raise SystemExit(f"add to GROUPS in {__file__}: {', '.join(missing)}")
    out = [
        f"# Plugin API reference (API {api.API_VERSION})",
        "",
        "Everything a plugin can use, from `fetchbox_plugin`. Generated from the API itself by",
        "`plugin-api/scripts/reference.py`; for how the pieces fit together, see the guide,",
        "[Writing Fetchbox plugins](plugins.md). Within API 1 this only grows: nothing is",
        "removed, renamed or changes meaning.",
        "",
    ]
    for group, names in GROUPS.items():
        out += [f"## {group}", ""]
        for n in names:
            out += _entry(n)
            if n == "Context":  # what its attributes are: ctx.http, ctx.cache, ...
                for part in CONTEXT_PARTS:
                    out += _entry(f"ctx: {part}", getattr(ctx_module, part))
    return "\n".join(out).rstrip() + "\n"


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        sys.exit(0 if OUT.read_text(encoding="utf-8") == text else 1)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")
