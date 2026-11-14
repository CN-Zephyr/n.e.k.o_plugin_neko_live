"""Persist recent live diagnostics without storing conversation content."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

from .contracts_types import TriggerSource
from .recent_context_routes import _KNOWN_ROUTE_STEPS, event_signal_from_result, route_from_result
from .runtime_timeline import persisted_reason_code

_FILE_NAME = "recent_live_results.json"
_LIVE_SOURCES = frozenset(get_args(TriggerSource)) - {"developer_sandbox", "manual_live_simulation"}
_ROUTES = _KNOWN_ROUTE_STEPS | _LIVE_SOURCES
_EVENT_TYPES = {"danmaku", "gift", "guard", "super_chat", "sc", "entry", "follow", "like"}
_STATUSES = {"queued", "dry_run", "pushed", "skipped", "failed"}
_TIMING_FIELDS = ("response_latency_ms", "pipeline_latency_ms", "dispatcher_latency_ms")
_TRACE_ID = re.compile(r"tr_[0-9a-f]{12}")


def recent_results_path(runtime: Any) -> Path | None:
    plugin = getattr(runtime, "plugin", None)
    data_path = getattr(plugin, "data_path", None)
    if not callable(data_path):
        return None
    try:
        root = Path(data_path())
    except TypeError:
        return None
    except Exception:
        return None
    if not str(root):
        return None
    return root / _FILE_NAME


def load_recent_results(runtime: Any) -> None:
    recent = runtime.recent_diagnostics
    path = recent_results_path(runtime)
    if path is None:
        return
    recent.clear()
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        _discard_legacy_temporary(runtime, path)
        return
    except OSError as exc:
        _audit_storage_failure(runtime, "read", exc)
        return
    try:
        raw = json.loads(contents.decode("utf-8"))
    except ValueError:
        raw = None
    rows = _diagnostic_rows(raw, _recent_limit(runtime)) if isinstance(raw, list) else []
    recent.extend(rows)
    try:
        if rows != raw:
            _write_rows(path, rows)
    except OSError as exc:
        _audit_storage_failure(runtime, "migration", exc)
    _discard_legacy_temporary(runtime, path)


def save_recent_results(runtime: Any) -> None:
    recent = runtime.recent_diagnostics
    path = recent_results_path(runtime)
    if path is None:
        return
    try:
        _write_rows(path, _diagnostic_rows(list(recent), _recent_limit(runtime)))
    except OSError:
        return


def clear_recent_diagnostics(runtime: Any) -> bool:
    path = recent_results_path(runtime)
    if path is None:
        return False
    try:
        _legacy_temporary(path).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
    except OSError as exc:
        _audit_storage_failure(runtime, "clear", exc)
        return False
    runtime.recent_diagnostics.clear()
    runtime.runtime_timeline.clear()
    return True


def _legacy_temporary(path: Path) -> Path:
    return path.with_suffix(".json.tmp")


def _discard_legacy_temporary(runtime: Any, path: Path) -> None:
    try:
        _legacy_temporary(path).unlink(missing_ok=True)
    except OSError as exc:
        _audit_storage_failure(runtime, "migration", exc)


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _diagnostic_rows(values: list[Any], limit: int) -> list[dict[str, Any]]:
    return [_diagnostic_row(row) for row in values[-limit:] if isinstance(row, dict)]


def _diagnostic_row(row: dict[str, Any]) -> dict[str, Any]:
    event = row.get("event")
    event = event if isinstance(event, dict) else {}
    event_summary = {
        "source": _known(event.get("source"), _LIVE_SOURCES),
        "event_type": _known(event.get("event_type"), _EVENT_TYPES),
        "seen_at": _timestamp(event.get("seen_at")),
    }
    route = _known(route_from_result(row), _ROUTES) or "unknown"
    signal = event_signal_from_result({"event": event_summary})
    trace = row.get("trace_id")
    summary = {
        "status": _known(row.get("status"), _STATUSES) or "failed",
        "reason": persisted_reason_code(row.get("reason")),
        "created_at": _timestamp(row.get("created_at")),
        "response_module": route,
        "event_signal": signal,
        "event": event_summary,
    }
    if isinstance(trace, str) and _TRACE_ID.fullmatch(trace):
        summary["trace_id"] = trace
    for key in _TIMING_FIELDS:
        value = row.get(key)
        if type(value) is int and 0 <= value:
            summary[key] = value
    return summary


def _known(value: Any, choices: set[str] | frozenset[str]) -> str:
    return value if isinstance(value, str) and value in choices else ""


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 40:
        return ""
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def _audit_storage_failure(runtime: Any, operation: str, exc: OSError) -> None:
    audit = getattr(runtime, "audit", None)
    if audit is not None:
        audit.record(
            "recent_diagnostics_storage_failed", "recent diagnostics operation failed",
            level="warning", detail={"operation": operation, "error_type": type(exc).__name__},
        )


def _recent_limit(runtime: Any) -> int:
    config = getattr(runtime, "config", None)
    try:
        limit = int(getattr(config, "recent_limit", 30) or 30)
    except (TypeError, ValueError):
        return 30
    return max(1, min(200, limit))
