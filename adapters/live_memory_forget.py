"""Explicit, stopped-stream erasure of one platform/character memory subject."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from ..stores.memory_forget_store import ForgetRequest, subject_owner
from .live_memory_bridge import _subject_id, subject_key


async def _supports_scoped_forget(client: Any, port: int) -> bool:
    """Older hosts may ignore unknown target_epochs and erase linked accounts."""
    try:
        response = await client.get(f"http://127.0.0.1:{port}/openapi.json", timeout=5.0, follow_redirects=False)
        response.raise_for_status()
        if len(response.content) > 4 * 1024 * 1024:
            return False
        schema = response.json()
        request = schema["paths"]["/internal/memory/{lanlan_name}/scoped_forget"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        reference = request.get("$ref", "")
        if reference.startswith("#/components/schemas/"):
            request = schema["components"]["schemas"][reference.rsplit("/", 1)[-1]]
        return {"subject", "forget_epoch", "target_epochs"} <= request.get("properties", {}).keys()
    except Exception:
        return False


def _validate_scope(runtime: Any, uid: str, platform: str, target: str) -> None:
    if not all(isinstance(value, str) for value in (uid, platform, target)):
        raise ValueError("host_memory.invalid_scope")
    if platform not in {"bilibili", "douyin", "twitch"} or not 1 <= len(target) <= 128 or not target.isprintable():
        raise ValueError("host_memory.invalid_scope")
    raw_uid = uid.removeprefix(platform + ":")
    if not re.fullmatch(r"[A-Za-z0-9_=-]{1,128}", raw_uid):
        raise ValueError("host_memory.invalid_uid")
    if runtime.config.live_enabled or runtime.safety_guard.connected:
        raise ValueError("host_memory.stop_live_first")
    if platform != runtime.live_provider.platform or target != runtime.dispatcher.live_target_lanlan():
        raise ValueError("host_memory.scope_changed")


def _profile_scope(runtime: Any) -> str:
    try:
        return runtime.viewer_store.deletion_scope()
    except Exception:
        raise OSError("host_memory.profile_scope_unavailable") from None


async def _erase_host(client: Any, request: ForgetRequest, *, retrying: bool, store: Any) -> None:
    base_url = f"http://127.0.0.1:{request.port}/internal/memory/{quote(request.target, safe='')}"
    key = subject_key(request.platform, request.uid)
    try:
        response = await client.post(
            f"{base_url}/scoped_forget",
            json={
                "subject": {"subject_kind": "participant", "subject_id": _subject_id(request.platform, request.uid)},
                "forget_epoch": request.epoch,
                # Reject linked-account expansion before any host erasure.
                "target_epochs": {key: request.epoch},
            },
            timeout=30.0, follow_redirects=False,
        )
    except Exception:
        raise OSError("host_memory.erase_unconfirmed") from None
    if response.status_code in {404, 409, 422}:
        if not retrying:
            await store.remove(request)
        raise OSError("host_memory.unsupported_or_linked_scope")
    try:
        response.raise_for_status()
        receipt = response.json()
        expected = {"subject_kind": "participant", "subject_id": _subject_id(request.platform, request.uid), "scope": key}
        if not isinstance(receipt, dict) or receipt.get("status") != "forgotten" or receipt.get("subject") != expected or receipt.get("forgotten_subjects") != [expected]:
            raise ValueError("unconfirmed receipt")
    except Exception:
        raise OSError("host_memory.erase_unconfirmed") from None


async def _continue_forget(bridge: Any, *, uid: str, platform: str, target: str) -> dict[str, Any]:
    runtime, store = bridge.runtime, bridge.forget_store
    # Reload only for explicit user actions, never on every incoming chat.
    await store.reload()
    owner = subject_owner(target, platform, uid)
    request = store.entries.get(owner)
    retrying = request is not None
    port = await bridge._memory_port()
    if not 1 <= port <= 65535:
        raise OSError("host_memory.unavailable")
    profile_scope = _profile_scope(runtime)
    if request is not None and (request.port != port or request.profile_scope != profile_scope):
        raise ValueError("host_memory.scope_changed")
    client = None
    if request is None or request.phase == "prepared":
        try:
            from utils.internal_http_client import get_internal_http_client

            client = get_internal_http_client()
        except Exception:
            raise OSError("host_memory.unavailable") from None
        if not await _supports_scoped_forget(client, port):
            raise OSError("host_memory.unsupported_or_linked_scope")
        if request is None:
            base_url = f"http://127.0.0.1:{port}/internal/memory/{quote(target, safe='')}"
            current = await bridge._forget_epoch(client, base_url, subject_key(platform, uid))
            if current is None:
                raise OSError("host_memory.unavailable")
            proposed = ForgetRequest(target, platform, uid, current + 1, port, profile_scope)
            # Checkpoint must reach disk before the first destructive request.
            request, created = await store.reserve(proposed)
            retrying = not created
    _validate_scope(runtime, uid, platform, target)
    if request.port != port or request.profile_scope != _profile_scope(runtime):
        raise ValueError("host_memory.scope_changed")
    if request.phase == "prepared":
        await _erase_host(client, request, retrying=retrying, store=store)
        request = await store.checkpoint(request, "host_confirmed")
    _validate_scope(runtime, uid, platform, target)
    local_deleted = request.phase == "local_deleted"
    if not local_deleted:
        try:
            local = await runtime.viewer_store.delete_profile(request.uid, expected_scope=request.profile_scope)
        except Exception:
            raise OSError("host_memory.local_delete_failed") from None
        if not local.get("applied"):
            raise OSError("host_memory.local_delete_failed")
        local_deleted = bool(local.get("deleted"))
        request = await store.checkpoint(request, "local_deleted")
    runtime.pipeline.session.clear_uid(request.uid)
    await store.remove(request)
    runtime.audit.record("viewer_memory_forgotten", "scoped host memory and local profile deleted")
    return {"forgotten": True, "local_deleted": local_deleted, "platform": platform, "target": target, "uid": request.uid}


async def forget_viewer(bridge: Any, *, uid: str, platform: str, target: str) -> dict[str, Any]:
    _validate_scope(bridge.runtime, uid, platform, target)
    owner = subject_owner(target, platform, uid)
    async with bridge._forget_lock:
        _validate_scope(bridge.runtime, uid, platform, target)
        bridge._forget_active.add(owner)
        bridge._forget_generation += 1
        bridge.discard_subject(target, platform, uid)
        try:
            return await _continue_forget(bridge, uid=uid, platform=platform, target=target)
        finally:
            bridge._forget_active.discard(owner)
