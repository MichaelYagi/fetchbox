"""Structured logging with secret redaction applied centrally, not at call sites."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

import structlog

REDACTED = "***"

_SECRET_KEY = re.compile(
    r"(pass(word|key)?|secret|token|api[_-]?key|apikey|cookie|authorization|credential)",
    re.IGNORECASE,
)
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Fetchbox API keys: fb_<prefix>_<secret> -> keep prefix for correlation
    (re.compile(r"\b(fb_[A-Za-z0-9]{8})_[A-Za-z0-9_-]{16,}"), r"\1_" + REDACTED),
    # Bearer tokens in strings
    (re.compile(r"(?i)(bearer\s+)[^\s\"']+"), r"\1" + REDACTED),
    # Secrets in URL query strings: passkey=, apikey=, token=, ...
    (
        re.compile(
            r"(?i)([?&](?:passkey|apikey|api_key|token|key|auth|password|pass)=)[^&\s#\"']+"
        ),
        r"\1" + REDACTED,
    ),
    # Tracker announce passkeys embedded in paths: /announce/<32+ hex>
    (re.compile(r"(?i)(/announce/|/a/)[0-9a-f]{16,}"), r"\1" + REDACTED),
    # user:password@host in URLs
    (re.compile(r"(://[^/:@\s]+:)[^@/\s]+@"), r"\1" + REDACTED + "@"),
)


def redact_string(value: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        value = pattern.sub(repl, value)
    return value


def redact_value(key: str | None, value: Any) -> Any:
    if key is not None and _SECRET_KEY.search(key) and value not in (None, ""):
        return REDACTED
    if isinstance(value, str):
        return redact_string(value)
    if isinstance(value, Mapping):
        return {k: redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(redact_value(None, v) for v in value)
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for k in list(event_dict.keys()):
        if k in {"event"}:
            event_dict[k] = redact_value(None, event_dict[k])
        else:
            event_dict[k] = redact_value(k, event_dict[k])
    return event_dict


def configure_logging(
    level: str = "info",
    fmt: str = "auto",
    logs_dir: Path | None = None,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backups: int = 10,
) -> None:
    lvl = getattr(logging, level.upper())
    console_human = fmt == "console" or (fmt == "auto" and sys.stderr.isatty())

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        redact_processor,
    ]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    def formatter(renderer: Any) -> logging.Formatter:
        return structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(lvl)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        formatter(
            structlog.dev.ConsoleRenderer()
            if console_human
            else structlog.processors.JSONRenderer()
        )
    )
    root.addHandler(console)

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            logs_dir / "fetchbox.log", maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        fh.setFormatter(formatter(structlog.processors.JSONRenderer()))
        root.addHandler(fh)

    for noisy in ("uvicorn.access", "alembic", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(lvl, logging.WARNING))


def set_module_level(module: str, level: str) -> None:
    """Runtime per-module level change (exposed via /system/logs later)."""
    logging.getLogger(module).setLevel(getattr(logging, level.upper()))
