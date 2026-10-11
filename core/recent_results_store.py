"""Bounded on-disk copy of the in-memory recent live results."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_FILE_NAME = "recent_live_results.json"


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
    recent = getattr(runtime, "recent_results", None)
    path = recent_results_path(runtime)
    if recent is None or path is None or not path.is_file():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return
    if not isinstance(raw, list):
        return
    limit = _recent_limit(runtime)
    rows = [row for row in raw if isinstance(row, dict)][-limit:]
    recent.clear()
    recent.extend(rows)


def save_recent_results(runtime: Any) -> None:
    recent = getattr(runtime, "recent_results", None)
    path = recent_results_path(runtime)
    if recent is None or path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(list(recent)[-_recent_limit(runtime) :], ensure_ascii=False)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        return


def _recent_limit(runtime: Any) -> int:
    config = getattr(runtime, "config", None)
    try:
        limit = int(getattr(config, "recent_limit", 30) or 30)
    except (TypeError, ValueError):
        return 30
    return max(1, limit)
