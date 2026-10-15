"""Exercise budget and submission outcomes through the real pipeline."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.neko_live.adapters.neko_dispatcher import NekoDispatcher
from plugin.plugins.neko_live.core.contracts import (
    InteractionRequest,
    LiveConfig,
    ViewerEvent,
    ViewerIdentity,
    ViewerProfile,
)
from plugin.plugins.neko_live.core.permission_gate import PermissionGate
from plugin.plugins.neko_live.core.pipeline import LivePipeline
from plugin.plugins.neko_live.core.safety_guard import SafetyDecision


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["budget", "rejected", "submitted"])
async def test_pipeline_records_real_dispatch_outcome_before_marking_viewer(outcome):
    identity = ViewerIdentity(uid="42", nickname="test viewer")
    profile = ViewerProfile(uid="42", nickname="test viewer")
    messages = []
    audits = []
    failures = []

    def submit(**kwargs):
        messages.append(kwargs)
        return {"submitted": outcome != "rejected", "error": "private-payload"}

    def build_request(event, identity, profile):
        text = "[NEKO Live first-appearance roast]\nRules:\n- Output only NEKO's line."
        if outcome == "budget":
            text += "\n- Unknown required constraint: " + "mandatory " * 3000
        return InteractionRequest(
            event=event, identity=identity, profile=profile,
            prompt_text=text, live_mode=event.live_mode, strength="normal",
        )

    config = LiveConfig(live_enabled=True, roast_once_per_uid=True, dry_run=False)
    viewer_profile = SimpleNamespace(
        upsert=AsyncMock(return_value=profile),
        has_roasted=AsyncMock(return_value=False),
        mark_roasted=AsyncMock(),
    )
    ctx = SimpleNamespace(
        config=config,
        permission_gate=PermissionGate(config),
        audit=SimpleNamespace(record=lambda *args, **kwargs: audits.append((args, kwargs))),
        safety_guard=SimpleNamespace(
            before_event=lambda _: SafetyDecision(True),
            before_output=lambda _: SafetyDecision(True),
            after_event=lambda: None,
            record_failure=lambda *args: failures.append(args),
        ),
        bili_identity=SimpleNamespace(resolve=AsyncMock(return_value=identity)),
        viewer_profile=viewer_profile,
        avatar_roast=SimpleNamespace(build_request=build_request),
        dispatcher=NekoDispatcher(SimpleNamespace(push_message=submit)),
        results=[],
    )
    ctx.record_result = ctx.results.append
    result = await LivePipeline(ctx).handle_event(
        ViewerEvent(uid="42", nickname="test viewer", danmaku_text="hello",
                    source="live_danmaku", target_lanlan="CatA"),
    )

    assert result.status == {"budget": "skipped", "rejected": "failed", "submitted": "pushed"}[outcome]
    assert result.accepted is (outcome == "submitted")
    assert ctx.results == [result]
    assert viewer_profile.mark_roasted.await_count == (1 if outcome == "submitted" else 0)
    assert len(messages) == (0 if outcome == "budget" else 1)
    assert "private-payload" not in repr(audits)
    assert "private-payload" not in repr(failures)
    if outcome == "budget":
        assert result.reason == "dispatcher.prompt_budget_exceeded"
        assert not failures
    if outcome == "rejected":
        assert failures
