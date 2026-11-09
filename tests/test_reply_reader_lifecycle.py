from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from plugin.plugins.neko_live.adapters.host_reply_reader import ReplyScope
from plugin.plugins.neko_live.core import runtime as runtime_module
from plugin.plugins.neko_live.core import runtime_config
from plugin.plugins.neko_live.core.runtime import LiveRuntime
from plugin.plugins.neko_live.core.runtime_config_activation import activate_config
from plugin.plugins.neko_live.core.runtime_live_session import (
    begin_live_session,
    invalidate_live_session,
)


def _reply(request_id: str = "submitted-request") -> dict:
    return {
        "lanlan_name": "LiveCat",
        "turn_type": "proactive_reply",
        "content": "Owned live reply.",
        "timestamp": 1.0,
        "metadata": {
            "role": "cat",
            "event_id": "observed-reply",
            "reply_provenance": {
                "version": 1,
                "complete": True,
                "sources": [{
                    "kind": "plugin",
                    "plugin_id": "neko_live",
                    "request_id": request_id,
                }],
            },
        },
    }


def _set_getter(runtime: LiveRuntime, getter) -> None:
    runtime.plugin.ctx = SimpleNamespace(
        bus=SimpleNamespace(conversations=SimpleNamespace(get=getter)),
    )


async def _seed_replies(runtime: LiveRuntime):
    async def get(**_):
        return [_reply()]

    _set_getter(runtime, get)
    reader = runtime.dispatcher._reply_reader
    scope = ReplyScope("LiveCat", 1, 1)
    reader.submitted(scope, "submitted-request")
    assert await reader.prepare(scope) == ["Owned live reply."]
    return reader, scope


async def _noop(*_args, **_kwargs) -> None:
    return None


def _stub_start_dependencies(runtime: LiveRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "reload_config", _noop)
    monkeypatch.setattr(runtime.viewer_store, "prune_expired_profiles", _noop)
    monkeypatch.setattr(runtime_module, "load_recent_results", lambda _runtime: None)
    monkeypatch.setattr(runtime, "reload_credential", _noop)
    monkeypatch.setattr(runtime, "reload_douyin_credential", _noop)
    monkeypatch.setattr(runtime, "reload_twitch_credential", _noop)
    monkeypatch.setattr(runtime.registry, "setup_all", _noop)
    monkeypatch.setattr(runtime, "_start_idle_hosting_loop", lambda: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [begin_live_session, invalidate_live_session])
async def test_session_boundary_discards_observed_replies(
    runtime: LiveRuntime, boundary,
) -> None:
    reader, scope = await _seed_replies(runtime)

    boundary(runtime)

    assert await reader.prepare(scope) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("old_dry_run,new_dry_run", [(False, True), (True, False), (False, False), (True, True)])
async def test_dry_run_transition_discards_only_the_previous_mode_replies(
    runtime: LiveRuntime, old_dry_run: bool, new_dry_run: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.config.dry_run = old_dry_run
    reader, scope = await _seed_replies(runtime)

    monkeypatch.setattr(runtime_config, "reconcile_live_listener_after_config", _noop)
    await runtime_config._commit_loaded_config(runtime, replace(runtime.config, dry_run=new_dry_run))

    expected = [] if old_dry_run != new_dry_run else ["Owned live reply."]
    assert await reader.prepare(scope) == expected


def test_initial_config_accepts_a_legacy_dispatcher(runtime: LiveRuntime) -> None:
    config = replace(runtime.config, dry_run=True)
    runtime.dispatcher = SimpleNamespace()
    del runtime.config

    assert activate_config(runtime, config) is config
    begin_live_session(runtime)
    invalidate_live_session(runtime)


@pytest.mark.asyncio
async def test_stop_closes_reader_before_cleanup_and_rejects_late_reply(
    runtime: LiveRuntime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def delayed_get(**_):
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # A transport may complete after cancellation was requested.
            await release.wait()
        return [_reply()]

    _set_getter(runtime, delayed_get)
    reader = runtime.dispatcher._reply_reader
    scope = ReplyScope("LiveCat", 1, 1)
    reader.submitted(scope, "submitted-request")
    pending = asyncio.create_task(reader.prepare(scope))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    cleanup_seen = False

    async def first_cleanup() -> None:
        nonlocal cleanup_seen
        cleanup_seen = True
        assert runtime._stopping is True
        assert reader._closed is True
        release.set()

    monkeypatch.setattr(runtime, "_stop_idle_hosting_loop", first_cleanup)
    try:
        await runtime.stop()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)

    assert cleanup_seen is True
    reader.submitted(scope, "after-stop-request")
    assert await reader.prepare(scope) == []
    assert calls == 1
    assert reader._closed is True


@pytest.mark.asyncio
async def test_start_reopens_reader_before_loading_config(
    runtime: LiveRuntime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.dispatcher.close_reply_reader()
    _stub_start_dependencies(runtime, monkeypatch)
    loaded = False

    async def reload_config() -> None:
        nonlocal loaded
        loaded = True
        await _seed_replies(runtime)

    monkeypatch.setattr(runtime, "reload_config", reload_config)

    await runtime.start()

    assert loaded is True
    assert runtime._stopping is False


@pytest.mark.asyncio
async def test_start_and_stop_support_legacy_dispatcher_without_reader_hooks(
    runtime: LiveRuntime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.dispatcher = SimpleNamespace()
    _stub_start_dependencies(runtime, monkeypatch)

    await runtime.start()
    await runtime.stop()

    assert runtime._shutdown_complete is True
