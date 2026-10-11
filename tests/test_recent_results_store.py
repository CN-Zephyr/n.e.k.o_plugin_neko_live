"""Recent live results survive a runtime restart."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from plugin.plugins.neko_live.core.recent_results_store import (
    load_recent_results,
    save_recent_results,
)


class _Plugin:
    def __init__(self, root: Path) -> None:
        self._root = root

    def data_path(self) -> Path:
        return self._root


def test_recent_live_results_round_trip(tmp_path: Path) -> None:
    runtime = SimpleNamespace(
        plugin=_Plugin(tmp_path),
        config=SimpleNamespace(recent_limit=2),
        recent_results=deque(maxlen=2),
    )
    runtime.recent_results.append({"status": "pushed", "output": "第一句"})
    runtime.recent_results.append({"status": "pushed", "output": "第二句"})
    runtime.recent_results.append({"status": "pushed", "output": "第三句"})

    save_recent_results(runtime)

    stored = json.loads((tmp_path / "recent_live_results.json").read_text(encoding="utf-8"))
    assert [row["output"] for row in stored] == ["第二句", "第三句"]

    restarted = SimpleNamespace(
        plugin=_Plugin(tmp_path),
        config=SimpleNamespace(recent_limit=2),
        recent_results=deque(maxlen=2),
    )
    load_recent_results(restarted)

    assert [row["output"] for row in restarted.recent_results] == ["第二句", "第三句"]
