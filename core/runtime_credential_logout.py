"""Complete local logout and retire the signed-out provider's listener."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..stores.credential_store import CredentialDeleteError
from .runtime_live_listener import stop_live_session_for_platform


async def remove_local_credential(
    runtime: Any,
    *,
    platform: str,
    cache_attr: str,
    delete: Callable[[], Awaitable[list[str]]],
    audit_op: str,
) -> dict[str, Any]:
    removed: list[str] = []
    failure = ""
    try:
        removed = await delete()
    except CredentialDeleteError:
        failure = "credential_removal_failed"
    finally:
        setattr(runtime, cache_attr, None)
        try:
            await stop_live_session_for_platform(runtime, platform)
        except Exception as exc:
            failure = failure or "listener_stop_failed"
            runtime.audit.record(
                "credential_listener_stop_failed",
                "signed-out provider could not be stopped",
                level="warning",
                detail={"platform": platform, "error_type": type(exc).__name__},
            )

    result: dict[str, Any] = {"logged_out": not failure, "logged_in": False, "removed": removed}
    if failure:
        result["logout_error"] = failure
        runtime.audit.record(
            f"{audit_op}_failed", "local logout could not be completed",
            level="warning", detail={"reason": failure},
        )
    else:
        runtime.audit.record(audit_op, "logged out (local credential removed)", detail={"files": removed})
    return result
