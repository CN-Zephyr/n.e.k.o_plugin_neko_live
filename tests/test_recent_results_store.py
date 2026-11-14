"""Recent live results survive a runtime restart."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from plugin.plugins.neko_live.core.contracts import InteractionResult, ViewerEvent
from plugin.plugins.neko_live.core.recent_results_store import (
    clear_recent_diagnostics,
    load_recent_results,
    save_recent_results,
)
from plugin.plugins.neko_live.core.runtime import LiveRuntime
from plugin.plugins.neko_live.core.runtime_live_session import begin_live_session


class _Plugin:
    def __init__(self, root: Path) -> None:
        self._root = root

    def data_path(self) -> Path:
        return self._root


def test_recent_live_results_round_trip(tmp_path: Path) -> None:
    runtime = SimpleNamespace(
        plugin=_Plugin(tmp_path),
        config=SimpleNamespace(recent_limit=2),
        recent_diagnostics=deque(maxlen=2),
    )
    runtime.recent_diagnostics.extend({"status": "pushed", "pipeline_latency_ms": ms} for ms in (10, 20, 30))

    save_recent_results(runtime)

    stored = json.loads((tmp_path / "recent_live_results.json").read_text(encoding="utf-8"))
    assert [row["pipeline_latency_ms"] for row in stored] == [20, 30]

    restarted = SimpleNamespace(
        plugin=_Plugin(tmp_path),
        config=SimpleNamespace(recent_limit=2),
        recent_diagnostics=deque(maxlen=2),
    )
    load_recent_results(restarted)

    assert list(restarted.recent_diagnostics) == stored


def test_recent_live_result_redacts_oauth_before_persisting(
    runtime: LiveRuntime, tmp_path: Path,
) -> None:
    runtime.record_result(
        InteractionResult(
            accepted=False,
            status="skipped",
            event=ViewerEvent(
                uid="42",
                nickname="viewer",
                danmaku_text="Authorization: OAuth synthetic-credential",
                source="live_danmaku",
            ),
        )
    )

    stored = json.loads((tmp_path / "recent_live_results.json").read_text(encoding="utf-8"))
    assert runtime.recent_results[-1]["event"]["danmaku_text"] == "[redacted]"
    assert "danmaku_text" not in stored[-1]["event"]


def test_legacy_diagnostics_are_minimized_on_reload_and_clearable(runtime: LiveRuntime, tmp_path: Path) -> None:
    path = tmp_path / "recent_live_results.json"
    stale_temp = path.with_suffix(".json.tmp")
    old = {
        "status": "pushed",
        "reason": "queued_to_neko(synthetic-dialogue)",
        "event": {
            "source": "live_danmaku", "event_type": "danmaku", "uid": "test-viewer",
            "danmaku_text": "synthetic-viewer-text",
        },
        "output": "synthetic-reply",
        "profile": {"nickname": "synthetic-nickname"},
        "request": {"prompt": "synthetic-prompt"},
        "trace_id": "tr_123456789abc",
    }
    path.write_text(json.dumps([old]), encoding="utf-8")
    stale_temp.write_text(json.dumps([old]), encoding="utf-8")

    load_recent_results(runtime)

    assert not runtime.recent_results
    assert len(runtime.recent_diagnostics) == 1
    summary = runtime.recent_diagnostics[0]
    assert summary["reason"] == "dispatcher.pushed"
    assert summary["trace_id"] == "tr_123456789abc"
    assert summary["event"] == {"source": "live_danmaku", "event_type": "danmaku", "seen_at": ""}
    assert "synthetic" not in path.read_text(encoding="utf-8")
    assert not stale_temp.exists()

    assert clear_recent_diagnostics(runtime)
    assert not runtime.recent_diagnostics
    assert not path.exists()


def test_recent_diagnostics_clear_reports_failure_and_keeps_state(runtime: LiveRuntime, tmp_path: Path) -> None:
    (tmp_path / "recent_live_results.json").mkdir()
    runtime.recent_diagnostics.append({"status": "pushed"})

    assert not clear_recent_diagnostics(runtime)
    assert runtime.recent_diagnostics[0]["status"] == "pushed"
    assert runtime.audit.recent(1)[0]["op"] == "recent_diagnostics_storage_failed"


@pytest.mark.asyncio
async def test_clear_diagnostics_keeps_only_session_context_and_does_not_resave_cleared_rows(
    runtime: LiveRuntime, tmp_path: Path,
) -> None:
    event = ViewerEvent(uid="42", source="live_danmaku", danmaku_text="Synthetic question")
    runtime.record_result(InteractionResult(accepted=True, status="pushed", event=event))
    old_trace_id = event.trace_id
    assert (await runtime.dashboard_state())["recent_results"][0]["trace_id"] == old_trace_id
    assert runtime.runtime_timeline

    assert runtime.clear_recent_diagnostics()

    state = await runtime.dashboard_state()
    assert state["recent_results"] == []
    assert state["live_explain"]["trace_id"] == ""
    assert state["speech_explanation"]["last_result_status"] == ""
    assert not runtime.runtime_timeline
    assert runtime.recent_results[-1]["event"]["danmaku_text"] == "Synthetic question"
    assert "timeline" not in runtime.recent_results[-1]

    new_event = ViewerEvent(uid="77", source="live_danmaku")
    runtime.record_result(InteractionResult(accepted=False, status="skipped", event=new_event))
    snapshot = json.loads((tmp_path / "recent_live_results.json").read_text(encoding="utf-8"))
    assert [row["trace_id"] for row in snapshot] == [new_event.trace_id]
    assert len(runtime.recent_results) == 2

    begin_live_session(runtime)
    assert not runtime.recent_results
    assert not runtime.recent_diagnostics
