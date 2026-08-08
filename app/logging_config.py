"""Structured logging with rotation, correlation context, and secret redaction.

Every record carries the run id and, where one is in scope, the correlation id,
so a single trade lifecycle (signal -> risk decision -> order -> broker
response -> fill) can be reconstructed from the logs alone.

Redaction is applied by a :class:`logging.Filter`, which means it applies to
*every* handler including third-party libraries that log through the standard
``logging`` module. It is a backstop, not a licence to pass secrets around: the
application never holds broker credentials in the first place.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Final, TextIO

from app.config import Config
from app.utilities.timeutils import utc_now

_run_id_var: ContextVar[str] = ContextVar("run_id", default="-")
_correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")

#: Substrings that mark a field as sensitive. Matching is case-insensitive and
#: applies to keys in structured payloads.
SENSITIVE_KEY_PARTS: Final = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "cookie",
    "session_key",
    "private_key",
    "privatekey",
    "credential",
    "ssh_key",
    "access_key",
    "client_secret",
)

REDACTED: Final = "***REDACTED***"

#: Free-text patterns redacted from message bodies as a defence in depth.
#:
#: Each entry pairs a pattern with how to rewrite a match. A ``key=value`` match
#: keeps its key, so the log still records *what* was redacted; a bare
#: credential is replaced outright, because no part of it is safe to keep.
_TEXT_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|cookie|"
            r"credential)\b(\s*[:=]\s*)\S+"
        ),
        rf"\1\2{REDACTED}",
    ),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        REDACTED,
    ),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), REDACTED),  # GitHub tokens
)

#: Log record attributes that are not part of the caller's structured payload.
_STANDARD_RECORD_FIELDS: Final = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


# ---------------------------------------------------------------------------
# Correlation context
# ---------------------------------------------------------------------------


def set_run_id(run_id: str) -> None:
    _run_id_var.set(run_id)


def get_run_id() -> str:
    return _run_id_var.get()


def set_correlation_id(correlation_id: str) -> None:
    _correlation_id_var.set(correlation_id)


def get_correlation_id() -> str:
    return _correlation_id_var.get()


class correlation_scope:  # noqa: N801 -- used as a context manager, reads as a verb
    """Bind a correlation id for the duration of a ``with`` block."""

    def __init__(self, correlation_id: str) -> None:
        self._correlation_id = correlation_id
        self._token: Any = None

    def __enter__(self) -> str:
        self._token = _correlation_id_var.set(self._correlation_id)
        return self._correlation_id

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _correlation_id_var.reset(self._token)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def redact_value(key: str, value: object, *, depth: int = 0) -> object:
    """Recursively redact a structured value."""
    if depth > 6:
        return "***DEPTH_LIMIT***"
    if _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact_value(str(k), v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_value(key, item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    """Strip credential-looking substrings from free text."""
    result = text
    for pattern, replacement in _TEXT_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def mask_account_id(account_id: str | None) -> str:
    """Mask a brokerage account identifier for display.

    ``DU1234567`` becomes ``DU***567``. Enough to distinguish accounts in an
    incident, not enough to be worth harvesting from a log aggregator.
    """
    if not account_id:
        return "-"
    cleaned = account_id.strip()
    if len(cleaned) <= 4:
        return "*" * len(cleaned)
    return f"{cleaned[:2]}***{cleaned[-3:]}"


class RedactionFilter(logging.Filter):
    """Applies redaction to the message and to every structured extra."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_RECORD_FIELDS or key.startswith("_"):
                continue
            record.__dict__[key] = redact_value(key, value)
        return True


class ContextFilter(logging.Filter):
    """Attaches run id and correlation id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = _run_id_var.get()
        if not hasattr(record, "correlation_id"):
            record.correlation_id = _correlation_id_var.get()
        return True


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """One JSON object per line, UTC timestamps, structured extras preserved."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": utc_now().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.funcName),
            "message": record.getMessage(),
            "run_id": getattr(record, "run_id", "-"),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact_text(self.formatStack(record.stack_info))
        return json.dumps(payload, default=_json_default, separators=(",", ":"))


def _json_default(value: object) -> str:
    return str(value)


class TextFormatter(logging.Formatter):
    """Human-readable format for local development."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-8s [%(run_id)s/%(correlation_id)s] %(name)s: %(message)s"
        )

    def formatTime(  # noqa: N802 -- overriding a stdlib method name
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        return utc_now().isoformat()


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def configure_logging(
    config: Config,
    *,
    run_id: str,
    log_to_file: bool = True,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Install handlers on the root logger. Idempotent within a process.

    ``stream`` defaults to stdout, which is what the trading process wants --
    Docker captures it. Operator commands that print a machine-readable report
    on stdout pass ``sys.stderr`` instead, so log lines cannot interleave with
    the report and ``2>/dev/null`` still yields parseable JSON.
    """
    set_run_id(run_id)

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.log_level, logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter: logging.Formatter = (
        JsonFormatter() if config.log_format == "json" else TextFormatter()
    )
    filters: Iterable[logging.Filter] = (ContextFilter(), RedactionFilter())

    stream_handler = logging.StreamHandler(stream=stream if stream is not None else sys.stdout)
    stream_handler.setFormatter(formatter)
    for log_filter in filters:
        stream_handler.addFilter(log_filter)
    root.addHandler(stream_handler)

    if log_to_file:
        log_dir = Path(config.log_dir)
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_dir / "trading.log",
                maxBytes=config.log_max_bytes,
                backupCount=config.log_backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            for log_filter in (ContextFilter(), RedactionFilter()):
                file_handler.addFilter(log_filter)
            root.addHandler(file_handler)
        except OSError as exc:
            # Losing the file sink must not take the process down; stdout
            # logging (captured by Docker) remains.
            root.warning(
                "file logging unavailable, continuing with stdout only",
                extra={"event": "logging.file_unavailable", "error": str(exc)},
            )

    return logging.getLogger("app")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("app") else f"app.{name}")


__all__ = [
    "REDACTED",
    "ContextFilter",
    "JsonFormatter",
    "RedactionFilter",
    "TextFormatter",
    "configure_logging",
    "correlation_scope",
    "get_correlation_id",
    "get_logger",
    "get_run_id",
    "mask_account_id",
    "redact_text",
    "redact_value",
    "set_correlation_id",
    "set_run_id",
]
