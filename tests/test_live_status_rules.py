from types import SimpleNamespace

from plugin.plugins.neko_live.core import runtime_dashboard
from plugin.plugins.neko_live.core.live_status_active import active_engagement_status
from plugin.plugins.neko_live.core.live_status_director import live_director_status
from plugin.plugins.neko_live.core.live_status_idle import idle_hosting_status
from plugin.plugins.neko_live.core.live_status_readiness import speech_explanation
from plugin.plugins.neko_live.core.live_status_timing import (
    active_engagement_after_danmaku_interval_seconds,
    recent_hosting_output_age_sec,
    recent_live_danmaku_event_age_sec,
)
from plugin.plugins.neko_live.core.recent_output_families import spent_output_families
from plugin.plugins.neko_live.core.runtime_dashboard_api import (
    RuntimeDashboardApiMixin,
)
from plugin.plugins.neko_live.core.runtime_live_status_helpers import (
    RuntimeLiveStatusHelperMixin,
)
from plugin.plugins.neko_live.core.runtime_recent_context_api import (
    RuntimeRecentContextApiMixin,
)


class _RecentContextRuntime(RuntimeRecentContextApiMixin):
    def __init__(self, recent_results):
        self.recent_results = recent_results


def test_speech_explanation_does_not_treat_pushed_as_playback_completion():
    explanation = speech_explanation(
        live_status={"summary": "ready_to_stream", "reason": "ready"},
        live_state={"state": "engaged", "reason": "recent_activity"},
        latest_result={
            "status": "pushed",
            "reason": "dispatcher.pushed",
            "created_at": "2026-07-27T12:00:00Z",
            "event": {"source": "live_danmaku"},
        },
        iso_age_fn=lambda _value: 1.0,
    )

    assert explanation["summary"] == "recently_handed_off"
    assert explanation["reason"] == "host_handoff"


def test_live_status_rules_treat_missing_attempt_timestamp_as_never_attempted():
    active = active_engagement_status(
        config=SimpleNamespace(live_mode="solo_stream"),
        live_status={"summary": "ready_to_stream", "cooldown_remaining": 0.0},
        live_state={"state": "quiet"},
        now=120.0,
        last_attempt_at=None,
        min_interval_seconds=60.0,
        recent_danmaku_output_age=None,
        recent_danmaku_wait_seconds=45.0,
        idle_hosting_wait_remaining=None,
        idle_grace_seconds=30.0,
        idle_takeover_streak=0,
    )
    idle = idle_hosting_status(
        live_state={"idle_hosting_candidate": True},
        now=120.0,
        last_attempt_at=None,
        min_interval_seconds=60.0,
        consecutive_failures=0,
        failure_limit=3,
    )

    assert active["cooldown_remaining"] == 0.0
    assert idle["cooldown_remaining"] == 0.0


def test_recent_hosting_output_age_ignores_dry_run_results():
    results = [
        {
            "status": "dry_run",
            "response_module": "idle_hosting",
            "created_at": "2026-07-09T23:00:00Z",
        }
    ]

    assert recent_hosting_output_age_sec(results, lambda value: 1.0) is None


def test_recent_danmaku_event_age_ignores_dry_run_results():
    results = [
        {
            "status": "dry_run",
            "event": {"source": "live_danmaku"},
            "created_at": "2026-07-10T18:00:00Z",
        }
    ]

    assert recent_live_danmaku_event_age_sec(results, lambda value: 1.0) is None


def test_dry_run_danmaku_does_not_reset_actual_route_streak():
    runtime = _RecentContextRuntime(
        [
            {"status": "pushed", "response_module": "idle_hosting"},
            {"status": "pushed", "response_module": "idle_hosting"},
            {"status": "dry_run", "event": {"source": "live_danmaku"}},
        ]
    )

    assert runtime._recent_actual_route_streak_since_viewer_activity("idle_hosting") == 2


def test_spent_output_families_returns_choice_vote_once():
    families = spent_output_families("choice either_or \u4e8c\u9009\u4e00")

    assert families.count("choice_vote") == 1


def test_idle_takeover_streak_reaches_director_active_engagement_branch():
    active = active_engagement_status(
        config=SimpleNamespace(live_mode="solo_stream", activity_level="standard"),
        live_status={"summary": "ready_to_stream", "cooldown_remaining": 0.0},
        live_state={"state": "idle", "mode": "solo_stream"},
        now=120.0,
        last_attempt_at=0.0,
        min_interval_seconds=60.0,
        recent_danmaku_output_age=None,
        recent_danmaku_wait_seconds=45.0,
        idle_hosting_wait_remaining=None,
        idle_grace_seconds=30.0,
        idle_takeover_streak=3,
        recent_hosting_output_age=None,
        host_output_cooldown_seconds=90.0,
    )

    director = live_director_status(
        config=SimpleNamespace(live_mode="solo_stream"),
        live_status={"summary": "ready_to_stream"},
        live_state={"state": "idle", "mode": "solo_stream"},
        idle_hosting_status={"eligible": False, "reason": "minimum_interval"},
        active_engagement_status=active,
    )

    assert active["reason"] == "idle_hosting_streak"
    assert active["eligible"] is True
    assert director["next_auto_action"] == "active_engagement"
    assert director["reason"] == "idle_hosting_streak"


def test_idle_takeover_streak_preserves_action_during_host_output_cooldown():
    active = active_engagement_status(
        config=SimpleNamespace(live_mode="solo_stream", activity_level="standard"),
        live_status={"summary": "ready_to_stream", "cooldown_remaining": 0.0},
        live_state={"state": "idle", "mode": "solo_stream"},
        now=120.0,
        last_attempt_at=0.0,
        min_interval_seconds=60.0,
        recent_danmaku_output_age=None,
        recent_danmaku_wait_seconds=45.0,
        idle_hosting_wait_remaining=None,
        idle_grace_seconds=30.0,
        idle_takeover_streak=3,
        recent_hosting_output_age=10.0,
        host_output_cooldown_seconds=90.0,
    )

    director = live_director_status(
        config=SimpleNamespace(live_mode="solo_stream"),
        live_status={"summary": "ready_to_stream"},
        live_state={"state": "idle", "mode": "solo_stream"},
        idle_hosting_status={"eligible": False, "reason": "recent_host_output"},
        active_engagement_status=active,
    )

    assert active["reason"] == "idle_hosting_streak"
    assert active["eligible"] is False
    assert active["cooldown_remaining"] == 80.0
    assert director["next_auto_action"] == "active_engagement"
    assert director["eligible"] is False
    assert director["reason"] == "idle_hosting_streak"


def test_idle_takeover_streak_preserves_action_during_live_status_cooldown():
    active = active_engagement_status(
        config=SimpleNamespace(live_mode="solo_stream", activity_level="standard"),
        live_status={"summary": "ready_to_stream", "cooldown_remaining": 25.0},
        live_state={"state": "idle", "mode": "solo_stream"},
        now=120.0,
        last_attempt_at=0.0,
        min_interval_seconds=60.0,
        recent_danmaku_output_age=None,
        recent_danmaku_wait_seconds=45.0,
        idle_hosting_wait_remaining=None,
        idle_grace_seconds=30.0,
        idle_takeover_streak=3,
        recent_hosting_output_age=None,
        host_output_cooldown_seconds=90.0,
    )

    director = live_director_status(
        config=SimpleNamespace(live_mode="solo_stream"),
        live_status={"summary": "ready_to_stream"},
        live_state={"state": "idle", "mode": "solo_stream"},
        idle_hosting_status={"eligible": False, "reason": "cooldown"},
        active_engagement_status=active,
    )

    assert active["reason"] == "idle_hosting_streak"
    assert active["eligible"] is False
    assert active["cooldown_remaining"] == 25.0
    assert director["next_auto_action"] == "active_engagement"
    assert director["eligible"] is False
    assert director["reason"] == "idle_hosting_streak"


def test_idle_takeover_streak_preserves_action_during_minimum_interval():
    active = active_engagement_status(
        config=SimpleNamespace(live_mode="solo_stream", activity_level="standard"),
        live_status={"summary": "ready_to_stream", "cooldown_remaining": 0.0},
        live_state={"state": "idle", "mode": "solo_stream"},
        now=120.0,
        last_attempt_at=100.0,
        min_interval_seconds=60.0,
        recent_danmaku_output_age=None,
        recent_danmaku_wait_seconds=45.0,
        idle_hosting_wait_remaining=None,
        idle_grace_seconds=30.0,
        idle_takeover_streak=3,
        recent_hosting_output_age=10.0,
        host_output_cooldown_seconds=90.0,
    )

    director = live_director_status(
        config=SimpleNamespace(live_mode="solo_stream"),
        live_status={"summary": "ready_to_stream"},
        live_state={"state": "idle", "mode": "solo_stream"},
        idle_hosting_status={"eligible": False, "reason": "minimum_interval"},
        active_engagement_status=active,
    )

    assert active["reason"] == "idle_hosting_streak"
    assert active["eligible"] is False
    assert active["cooldown_remaining"] == 40.0
    assert director["next_auto_action"] == "active_engagement"
    assert director["eligible"] is False
    assert director["reason"] == "idle_hosting_streak"


def test_idle_takeover_streak_preserves_action_during_danmaku_cooldown():
    active = active_engagement_status(
        config=SimpleNamespace(live_mode="solo_stream", activity_level="standard"),
        live_status={"summary": "ready_to_stream", "cooldown_remaining": 0.0},
        live_state={"state": "idle", "mode": "solo_stream"},
        now=120.0,
        last_attempt_at=0.0,
        min_interval_seconds=60.0,
        recent_danmaku_output_age=10.0,
        recent_danmaku_wait_seconds=45.0,
        idle_hosting_wait_remaining=None,
        idle_grace_seconds=30.0,
        idle_takeover_streak=3,
    )

    director = live_director_status(
        config=SimpleNamespace(live_mode="solo_stream"),
        live_status={"summary": "ready_to_stream"},
        live_state={"state": "idle", "mode": "solo_stream"},
        idle_hosting_status={"eligible": False, "reason": "recent_danmaku_output"},
        active_engagement_status=active,
    )

    assert active["reason"] == "idle_hosting_streak"
    assert active["eligible"] is False
    assert active["cooldown_remaining"] == 35.0
    assert director["next_auto_action"] == "active_engagement"
    assert director["eligible"] is False
    assert director["reason"] == "idle_hosting_streak"


def test_runtime_dashboard_api_keeps_module_level_compatibility_exports(monkeypatch):
    health_rows = [{"id": "pipeline", "status": "ok"}]
    actions = [{"id": "refresh", "label": "Refresh"}]
    monkeypatch.setattr(runtime_dashboard, "runtime_health_rows", lambda runtime: health_rows)
    monkeypatch.setattr(runtime_dashboard, "dashboard_actions", lambda: actions)

    runtime = RuntimeDashboardApiMixin()

    assert runtime.runtime_health_rows() == health_rows
    assert runtime.dashboard_actions() == actions


def test_count_recent_live_replies_filters_and_stops_at_window():
    from collections import deque

    from plugin.plugins.neko_live.core.live_status_timing import count_recent_live_replies

    def row(age, *, status="pushed", source="live_danmaku", module="danmaku_response"):
        return {"created_at": age, "status": status, "event": {"source": source}, "response_module": module}

    rows = deque([
        row(90.0),                      # outside window: loop stops before reaching it
        row(None),                      # undated
        row(30.0, module="idle_hosting"),
        row(20.0, source="sandbox"),
        row(10.0, status="blocked"),
        row(5.0, status="dry_run", module="avatar_roast"),
        row(1.0),
        "junk",
    ])
    def age_fn(value):
        return value

    assert count_recent_live_replies(rows, window_seconds=60.0, age_fn=age_fn, count_undated=False) == 2
    assert count_recent_live_replies(rows, window_seconds=60.0, age_fn=age_fn, count_undated=True) == 3
    assert count_recent_live_replies(rows, window_seconds=60.0, age_fn=None, count_undated=False) == 0
    assert count_recent_live_replies(None, window_seconds=60.0, age_fn=age_fn, count_undated=True) == 0


def _quiet_active_status(**overrides):
    kwargs = dict(
        config=SimpleNamespace(live_mode="solo_stream"),
        live_status={"summary": "ready_to_stream", "cooldown_remaining": 0.0},
        live_state={"state": "quiet"},
        now=120.0,
        last_attempt_at=0.0,
        min_interval_seconds=60.0,
        recent_danmaku_output_age=None,
        recent_danmaku_wait_seconds=90.0,
        idle_hosting_wait_remaining=None,
        idle_grace_seconds=30.0,
        idle_takeover_streak=0,
    )
    kwargs.update(overrides)
    return active_engagement_status(**kwargs)


def test_active_engagement_yields_to_pending_danmaku():
    blocked = _quiet_active_status(danmaku_pending=True)
    allowed = _quiet_active_status(danmaku_pending=False)

    assert blocked["candidate"] is True
    assert blocked["eligible"] is False
    assert blocked["reason"] == "danmaku_pending"
    assert blocked["danmaku_pending"] is True
    assert allowed["eligible"] is True
    assert allowed["reason"] == "eligible"


def test_idle_takeover_active_engagement_yields_to_pending_danmaku():
    status = _quiet_active_status(
        live_state={"state": "idle", "mode": "solo_stream"},
        idle_takeover_streak=3,
        danmaku_pending=True,
    )

    assert status["eligible"] is False
    assert status["reason"] == "danmaku_pending"


def test_idle_hosting_yields_to_pending_danmaku():
    kwargs = dict(
        live_state={"idle_hosting_candidate": True},
        now=120.0,
        last_attempt_at=0.0,
        min_interval_seconds=60.0,
        consecutive_failures=0,
        failure_limit=3,
    )
    blocked = idle_hosting_status(**kwargs, danmaku_pending=True)
    allowed = idle_hosting_status(**kwargs)

    assert blocked["eligible"] is False
    assert blocked["reason"] == "danmaku_pending"
    assert blocked["danmaku_pending"] is True
    assert allowed["eligible"] is True
    assert allowed["reason"] == "eligible"


def test_active_engagement_after_danmaku_interval_per_activity_level():
    def interval(level):
        return active_engagement_after_danmaku_interval_seconds(
            SimpleNamespace(activity_level=level)
        )

    assert interval("quiet") == 210.0
    assert interval("standard") == 90.0
    assert interval("active") == 60.0
    assert interval("unknown") == 90.0


class _LiveEventsRuntime(RuntimeLiveStatusHelperMixin):
    def __init__(self, live_events):
        self.live_events = live_events


class _LiveEventsStub:
    def __init__(self, status=None, error=False):
        self._status = status
        self._error = error

    def status(self):
        if self._error:
            raise RuntimeError("boom")
        return self._status


def test_live_events_danmaku_pending_reads_window_and_buffer():
    def pending(live_events):
        return _LiveEventsRuntime(live_events)._live_events_danmaku_pending()

    assert pending(_LiveEventsStub({"window_open": True, "buffered": 0})) is True
    assert pending(_LiveEventsStub({"window_open": False, "buffered": 2})) is True
    assert pending(_LiveEventsStub({"window_open": False, "buffered": 0})) is False
    assert pending(_LiveEventsStub({"buffered": "bad"})) is False
    assert pending(_LiveEventsStub(error=True)) is False
    assert pending(_LiveEventsStub("not-a-dict")) is False
    assert pending(None) is False
