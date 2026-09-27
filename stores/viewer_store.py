"""Single writer for viewer profiles（本地 JSON 持久化，存储目录可由主播配置）。

不走宿主 PluginStore：其 ``store.enabled`` 在插件构造期被冻结、且 ``store.db`` 路径焊死不可配
（见 docs/devlog.md）。改为直接读写一个本地 JSON 文件，从根上绕开那个 bug，并让"改存储位置"成为可能：
- 存储目录优先用配置的 ``viewer_store_dir``（``dir_provider`` 提供），留空则用插件数据目录 ``plugin.data_path()``；
- 配置目录不可写时回退默认目录并记一次 audit；
- 原子写（tmp + os.replace）+ asyncio 锁，避免并发 upsert/mark_roasted 互相覆盖（lost update）。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..core.contracts import ViewerIdentity, ViewerProfile, utc_now_iso
from ..core.contracts_public import public_int, public_text
from ..core.viewer_preferences import (
    infer_viewer_preferences,
    merge_preference_counts,
    safe_preference_counts,
    viewer_profile_projection,
)

_STORE_FILE = "viewer_profiles.json"
_MAX_PROFILE_TEXT = 240
_PROFILE_RETENTION_DAYS = 90
_RECENT_PROFILE_CACHE_LIMIT = 200


class ViewerStoreUnavailableError(OSError):
    """档案文件存在但暂时读不了；变更操作必须放弃写回，避免覆盖真实档案。"""


class ViewerStore:
    def __init__(
        self,
        plugin: Any,
        audit: Any,
        dir_provider: Callable[[], str] | None = None,
        *,
        memory_enabled_provider: Callable[[], bool] | None = None,
    ) -> None:
        self.plugin = plugin
        self.audit = audit
        self._dir_provider = dir_provider
        self._memory_enabled_provider = memory_enabled_provider
        # 串行化读改写，避免并发 upsert/mark_roasted 互相覆盖（lost update）。
        self._lock = asyncio.Lock()
        self._fallback_warned = False
        self._load_failure_warned = False
        self._active_fallback_file: Path | None = None
        # Dashboard state is polled every few seconds while live. Cache only
        # its bounded public projection; the canonical JSON remains the sole
        # source of truth for mutations and restart recovery.
        self._recent_cache: list[dict[str, Any]] = []
        self._recent_cache_file: Path | None = None
        self._recent_cache_signature: tuple[int, int, int] | None = None
        self._recent_cache_ready = False

    # ── 存储路径解析 ──────────────────────────────────────────────

    def _audit(self, op: str, message: str, level: str = "warning") -> None:
        if self.audit is not None:
            try:
                self.audit.record(op, message, level=level)
            except Exception:  # noqa: BLE001 — 记录失败不能反过来炸存储
                pass

    def _default_dir(self) -> Path:
        try:
            base = self.plugin.data_path()
            if base:
                return Path(base)
        except Exception:  # noqa: BLE001
            pass
        # 兜底：绝不写进 cwd（会污染工作目录/仓库）；退到进程临时目录。
        # 生产中 data_path() 必然可用、不会走到这，仅防御损坏的宿主/测试桩。
        return Path(tempfile.gettempdir()) / "neko_live"

    def _configured_dir(self) -> str:
        if not self._dir_provider:
            return ""
        try:
            return str(self._dir_provider() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _memory_enabled(self) -> bool:
        if not self._memory_enabled_provider:
            return True
        try:
            return self._memory_enabled_provider() is not False
        except Exception:  # noqa: BLE001
            return False

    def _resolve_file(self) -> tuple[Path, bool]:
        """返回 (档案文件路径, 是否用了自定义目录)。纯解析、无副作用（不建目录）。"""
        configured = self._configured_dir()
        if configured:
            return Path(configured) / _STORE_FILE, True
        return self._default_dir() / _STORE_FILE, False

    @staticmethod
    def _file_signature(file: Path) -> tuple[int, int, int] | None:
        try:
            stat = file.stat()
        except OSError:
            return None
        return (int(stat.st_mtime_ns), int(stat.st_size), int(getattr(stat, "st_ino", 0)))

    def _active_store_file(self) -> Path:
        configured_file, _custom = self._resolve_file()
        return self._active_fallback_file or configured_file

    def _recent_cache_matches_store(self) -> bool:
        if not self._recent_cache_ready:
            return False
        file = self._active_store_file()
        return (
            self._recent_cache_file == file
            and self._recent_cache_signature == self._file_signature(file)
        )

    def _update_recent_cache(
        self,
        profiles: dict[str, dict[str, Any]],
        *,
        file: Path | None = None,
    ) -> None:
        target = file or self._active_store_file()
        self._recent_cache = _recent_profile_projection(
            profiles,
            _RECENT_PROFILE_CACHE_LIMIT,
        )
        self._recent_cache_file = target
        self._recent_cache_signature = self._file_signature(target)
        self._recent_cache_ready = True

    def storage_status(self) -> dict[str, Any]:
        """给面板看的存储状态：当前文件路径 + 目录能否写 + 是否自定义。"""
        configured_file, custom = self._resolve_file()
        fallback_active = self._active_fallback_file is not None
        file = self._active_fallback_file or configured_file
        directory = file.parent
        return {
            "path": str(file),
            "dir": str(directory),
            "writable": _path_is_writable(file),
            "exists": file.exists(),
            "using_custom": bool(custom and not fallback_active),
            "fallback_active": fallback_active,
            "memory_enabled": self._memory_enabled(),
            "retention_days": _PROFILE_RETENTION_DAYS,
        }

    def _write_json(self, file: Path, profiles: dict[str, dict[str, Any]]) -> bool:
        """原子写（唯一 tmp + fsync + os.replace）；成功 True，失败 False（不抛）。

        tmp 用 mkstemp 取唯一名，避免与残留/并发 tmp 撞名；替换前 fsync，
        断电时磁盘上要么是完整旧文件、要么是完整新文件，不会留下截断的 JSON。
        """
        tmp: Path | None = None
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps(profiles, ensure_ascii=False, indent=2).encode("utf-8")
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{file.name}.",
                suffix=".tmp",
                dir=file.parent,
            )
            tmp = Path(tmp_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(tmp), str(file))
            tmp = None
            return True
        except Exception:  # noqa: BLE001
            return False
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001 — cleanup failure must not mask the write result
                    pass

    async def _load_all(
        self,
        *,
        include_expired: bool = False,
    ) -> dict[str, dict[str, Any]]:
        profiles, _writable = await self._load_profiles(include_expired=include_expired)
        return profiles

    async def _load_profiles(
        self,
        *,
        include_expired: bool = False,
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """返回 (档案, 是否可安全写回)。

        文件存在但读不了（如被杀软/备份软件暂时占用）时返回 ``({}, False)``：
        调用方不得把这份空结果写回，否则会覆盖掉整份真实档案。
        内容损坏（非 JSON / 顶层非对象）时先改名隔离成 ``.corrupt-*`` 备份，再按空库继续。
        """
        file, custom = self._resolve_file()
        candidates: list[Path] = []
        if self._active_fallback_file is not None:
            candidates.append(self._active_fallback_file)
        candidates.append(file)
        if custom:
            fallback = self._default_dir() / _STORE_FILE
            if fallback not in candidates:
                candidates.append(fallback)
        unreadable = False
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                raw = await asyncio.to_thread(candidate.read_bytes)
            except OSError as exc:
                unreadable = True
                self._audit_load_failure(f"{type(exc).__name__}: {candidate.name}")
                continue
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError:
                data = None
            if not isinstance(data, dict):
                if not await self._quarantine_corrupt_file(candidate):
                    unreadable = True
                continue
            if not unreadable:
                self._load_failure_warned = False
            self._active_fallback_file = candidate if candidate != file else None
            profiles: dict[str, dict[str, Any]] = {}
            for key, value in data.items():
                if not isinstance(value, dict):
                    continue
                uid = _safe_profile_uid(value.get("uid")) or _safe_profile_uid(key)
                if not uid:
                    continue
                item = _safe_profile_item(value, fallback_uid=uid)
                if item and (
                    include_expired or not _profile_is_expired(item)
                ):
                    profiles[uid] = item
            return profiles, not unreadable
        return {}, not unreadable

    def _audit_load_failure(self, message: str) -> None:
        # 直播中每条弹幕都会读档案；同一段故障只告警一次，成功读到后再重新计数。
        if self._load_failure_warned:
            return
        self._load_failure_warned = True
        self._audit("viewer_store_load_failed", message)

    async def _quarantine_corrupt_file(self, candidate: Path) -> bool:
        """把损坏的档案文件改名隔离，成功 True；隔离失败 False（调用方需拒绝写回）。"""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = candidate.with_name(f"{candidate.name}.corrupt-{stamp}")
        try:
            await asyncio.to_thread(os.replace, str(candidate), str(target))
        except OSError as exc:
            self._audit_load_failure(f"corrupt, quarantine failed ({type(exc).__name__}): {candidate.name}")
            return False
        self._audit("viewer_store_corrupt_quarantined", f"档案文件损坏，已隔离为 {target.name}")
        return True

    async def _load_for_write(
        self,
        *,
        include_expired: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """变更路径专用：档案读不了时抛错，绝不拿空结果覆盖真实档案。"""
        profiles, writable = await self._load_profiles(include_expired=include_expired)
        if not writable:
            raise ViewerStoreUnavailableError("viewer profile store is temporarily unreadable")
        return profiles

    async def prune_expired_profiles(self) -> dict[str, Any]:
        """Delete profiles inactive for the fixed retention window."""

        async with self._lock:
            try:
                profiles = await self._load_for_write(include_expired=True)
            except ViewerStoreUnavailableError:
                return {
                    "pruned": 0,
                    "applied": False,
                    "retention_days": _PROFILE_RETENTION_DAYS,
                }
            active = {
                uid: item
                for uid, item in profiles.items()
                if not _profile_is_expired(item)
            }
            pruned = len(profiles) - len(active)
            applied = True
            if pruned:
                applied = await self._save_all(active, allow_fallback=False)
                if applied:
                    self._audit(
                        "viewer_profiles_retention_prune",
                        "expired viewer profiles pruned",
                        level="info",
                    )
            return {
                "pruned": pruned if applied else 0,
                "applied": applied,
                "retention_days": _PROFILE_RETENTION_DAYS,
            }

    async def _save_all(
        self,
        profiles: dict[str, dict[str, Any]],
        *,
        allow_fallback: bool = True,
    ) -> bool:
        file, custom = self._resolve_file()
        if await asyncio.to_thread(self._write_json, file, profiles):
            self._active_fallback_file = None
            self._update_recent_cache(profiles, file=file)
            return True
        # 自定义目录写失败 → 回退默认目录（只告警一次，避免刷屏）。
        if custom and allow_fallback:
            fallback = self._default_dir() / _STORE_FILE
            if await asyncio.to_thread(self._write_json, fallback, profiles):
                self._active_fallback_file = fallback
                self._update_recent_cache(profiles, file=fallback)
                if not self._fallback_warned:
                    self._audit("viewer_store_fallback", f"自定义目录不可写，已回退默认目录：{fallback.parent}")
                    self._fallback_warned = True
                return True
        self._audit("viewer_store_save_failed", f"档案写入失败：{file}")
        return False

    # ── 公共 API（行为与原 KV 版一致，仅底层换成 JSON）──────────────

    async def upsert_identity(self, identity: ViewerIdentity) -> ViewerProfile:
        async with self._lock:
            return await self._upsert_identity_locked(identity)

    async def _upsert_identity_locked(self, identity: ViewerIdentity) -> ViewerProfile:
        now = utc_now_iso()
        uid = _safe_profile_uid(identity.uid)
        nickname = _safe_profile_text(identity.nickname)
        avatar_url = _safe_profile_text(identity.avatar_url)
        if not uid:
            return ViewerProfile(uid="", nickname=nickname, avatar_url=avatar_url)
        try:
            profiles = await self._load_for_write()
        except ViewerStoreUnavailableError:
            # 读不到真实档案时只给本次事件一个临时画像，不写盘。
            return ViewerProfile(
                uid=uid,
                nickname=nickname or uid,
                avatar_url=avatar_url,
                first_seen_at=now,
                last_seen_at=now,
            )
        existing = profiles.get(uid)
        if existing:
            profile = ViewerProfile(
                uid=uid,
                nickname=nickname or _safe_profile_text(existing.get("nickname")) or uid,
                avatar_url=avatar_url or _safe_profile_text(existing.get("avatar_url")),
                first_seen_at=_safe_profile_text(existing.get("first_seen_at")) or now,
                last_seen_at=now,
                roast_count=public_int(existing.get("roast_count"), default=0, minimum=0),
                last_roast_at=_safe_profile_text(existing.get("last_roast_at")),
                last_result=_safe_profile_text(existing.get("last_result")),
                danmaku_count=public_int(existing.get("danmaku_count"), default=0, minimum=0),
                preference_tags=safe_preference_counts(existing.get("preference_tags")),
                favorite_topics=safe_preference_counts(existing.get("favorite_topics")),
                running_jokes=safe_preference_counts(existing.get("running_jokes")),
                interaction_style=_safe_profile_text(existing.get("interaction_style")),
                response_preference=_safe_profile_text(existing.get("response_preference")),
                last_interaction_summary=_safe_profile_text(existing.get("last_interaction_summary")),
                impression_summary=_safe_profile_text(existing.get("impression_summary")),
                avoid_guidance=_safe_profile_text(existing.get("avoid_guidance")),
                last_interaction_at=_safe_profile_text(existing.get("last_interaction_at")),
            )
        else:
            profile = ViewerProfile(
                uid=uid,
                nickname=nickname or uid,
                avatar_url=avatar_url,
                first_seen_at=now,
                last_seen_at=now,
            )
        profiles[uid] = profile.to_dict()
        await self._save_all(profiles)
        return profile

    async def record_live_danmaku(
        self,
        identity: ViewerIdentity,
        danmaku_text: str,
        *,
        remember_preferences: bool | None = None,
    ) -> ViewerProfile:
        async with self._lock:
            now = utc_now_iso()
            uid = _safe_profile_uid(identity.uid)
            nickname = _safe_profile_text(identity.nickname)
            avatar_url = _safe_profile_text(identity.avatar_url)
            if not uid:
                return ViewerProfile(uid="", nickname=nickname, avatar_url=avatar_url)
            profiles = await self._load_for_write()
            item = _safe_profile_item(profiles.get(uid) or {"uid": uid}, fallback_uid=uid)
            memory_enabled = (
                self._memory_enabled()
                if remember_preferences is None
                else bool(remember_preferences)
            )
            inference = infer_viewer_preferences(danmaku_text) if memory_enabled else {}
            profile = ViewerProfile(
                uid=uid,
                nickname=nickname or _safe_profile_text(item.get("nickname")) or uid,
                avatar_url=avatar_url or _safe_profile_text(item.get("avatar_url")),
                first_seen_at=_safe_profile_text(item.get("first_seen_at")) or now,
                last_seen_at=now,
                roast_count=public_int(item.get("roast_count"), default=0, minimum=0),
                last_roast_at=_safe_profile_text(item.get("last_roast_at")),
                last_result=_safe_profile_text(item.get("last_result")),
                danmaku_count=public_int(item.get("danmaku_count"), default=0, minimum=0) + 1,
                preference_tags=(
                    merge_preference_counts(
                        item.get("preference_tags"),
                        [str(tag) for tag in inference.get("tags", []) if str(tag).strip()],
                    )
                    if memory_enabled
                    else safe_preference_counts(item.get("preference_tags"))
                ),
                favorite_topics=(
                    merge_preference_counts(
                        item.get("favorite_topics"),
                        [str(tag) for tag in inference.get("favorite_topics", []) if str(tag).strip()],
                    )
                    if memory_enabled
                    else safe_preference_counts(item.get("favorite_topics"))
                ),
                running_jokes=(
                    merge_preference_counts(
                        item.get("running_jokes"),
                        [str(tag) for tag in inference.get("running_jokes", []) if str(tag).strip()],
                    )
                    if memory_enabled
                    else safe_preference_counts(item.get("running_jokes"))
                ),
                interaction_style=_safe_profile_text(inference.get("interaction_style"))
                or _safe_profile_text(item.get("interaction_style")),
                response_preference=_safe_profile_text(inference.get("response_preference"))
                or _safe_profile_text(item.get("response_preference")),
                last_interaction_summary=_safe_profile_text(inference.get("summary"))
                or _safe_profile_text(item.get("last_interaction_summary")),
                impression_summary=_safe_profile_text(inference.get("impression_summary"))
                or _safe_profile_text(item.get("impression_summary")),
                avoid_guidance=_safe_profile_text(inference.get("avoid_guidance"))
                or _safe_profile_text(item.get("avoid_guidance")),
                last_interaction_at=now,
            )
            profiles[uid] = profile.to_dict()
            await self._save_all(profiles)
            return profile

    async def mark_roasted(self, uid: str, output: str) -> bool:
        async with self._lock:
            return await self._mark_roasted_locked(uid, output)

    async def _mark_roasted_locked(self, uid: str, output: str) -> bool:
        safe_uid = _safe_profile_uid(uid)
        if not safe_uid:
            return False
        try:
            profiles = await self._load_for_write()
        except ViewerStoreUnavailableError:
            return False
        item = _safe_profile_item(profiles.get(safe_uid) or {"uid": safe_uid}, fallback_uid=safe_uid)
        item["roast_count"] = public_int(item.get("roast_count"), default=0, minimum=0) + 1
        item["last_roast_at"] = utc_now_iso()
        item["last_result"] = _safe_profile_text(output)
        profiles[safe_uid] = _safe_profile_item(item, fallback_uid=safe_uid)
        return await self._save_all(profiles)

    async def has_roasted(self, uid: str) -> bool:
        profiles = await self._load_all()
        safe_uid = _safe_profile_uid(uid)
        item = profiles.get(safe_uid)
        return bool(item and public_int(item.get("roast_count"), default=0, minimum=0) > 0)

    async def recent_profiles(self, limit: int = 30) -> list[dict[str, Any]]:
        requested = max(0, int(limit))
        if requested <= _RECENT_PROFILE_CACHE_LIMIT and self._recent_cache_matches_store():
            return copy.deepcopy(self._recent_cache[:requested])
        async with self._lock:
            if requested <= _RECENT_PROFILE_CACHE_LIMIT and self._recent_cache_matches_store():
                return copy.deepcopy(self._recent_cache[:requested])
            profiles, readable = await self._load_profiles()
            if not readable:
                return _recent_profile_projection(profiles, requested)
            self._update_recent_cache(profiles)
            if requested <= _RECENT_PROFILE_CACHE_LIMIT:
                return copy.deepcopy(self._recent_cache[:requested])
            return _recent_profile_projection(profiles, requested)

    async def clear_profiles(self) -> dict[str, Any]:
        async with self._lock:
            try:
                profiles = await self._load_for_write()
            except ViewerStoreUnavailableError:
                profiles, persisted = {}, False
            else:
                persisted = await self._save_all({}, allow_fallback=False)
            cleared = len(profiles)
            file, _custom = self._resolve_file()
            if self._active_fallback_file is not None:
                file = self._active_fallback_file
            return {
                "cleared": cleared if persisted else 0,
                "applied": persisted,
                "path": str(file),
            }

    async def delete_profile(self, uid: str) -> dict[str, Any]:
        key = _safe_profile_uid(uid)
        if not key:
            raise ValueError("uid is required")
        async with self._lock:
            try:
                profiles = await self._load_for_write()
            except ViewerStoreUnavailableError:
                profiles, persisted = {}, False
                found = False
            else:
                found = key in profiles
                profiles.pop(key, None)
                persisted = await self._save_all(profiles, allow_fallback=False)
            file, _custom = self._resolve_file()
            if self._active_fallback_file is not None:
                file = self._active_fallback_file
            return {
                "uid": key,
                "deleted": bool(found and persisted),
                "applied": persisted,
                "path": str(file),
            }

    async def reset_profile_impression(self, uid: str) -> dict[str, Any]:
        key = _safe_profile_uid(uid)
        if not key:
            raise ValueError("uid is required")
        impression_fields = (
            "preference_tags",
            "favorite_topics",
            "running_jokes",
            "interaction_style",
            "response_preference",
            "last_interaction_summary",
            "impression_summary",
            "avoid_guidance",
            "last_interaction_at",
        )
        async with self._lock:
            # 读不了时直接抛 OSError，避免被上层误报成“档案不存在”。
            profiles = await self._load_for_write()
            item = profiles.get(key)
            found = isinstance(item, dict)
            persisted = False
            if found:
                for field in impression_fields:
                    if field in ("preference_tags", "favorite_topics", "running_jokes"):
                        item[field] = {}
                    else:
                        item[field] = ""
                profiles[key] = item
                persisted = await self._save_all(profiles, allow_fallback=False)
            file, _custom = self._resolve_file()
            if self._active_fallback_file is not None:
                file = self._active_fallback_file
            return {
                "uid": key,
                "found": found,
                "reset": bool(found and persisted),
                "applied": persisted,
                "path": str(file),
                "preserved_first_appearance": bool(
                    found
                    and persisted
                    and public_int(item.get("roast_count"), default=0, minimum=0) > 0
                ),
            }


def _recent_profile_projection(
    profiles: dict[str, dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    ordered = sorted(
        profiles.values(),
        key=lambda item: str(item.get("last_seen_at") or ""),
        reverse=True,
    )
    result: list[dict[str, Any]] = []
    for item in ordered[: max(0, int(limit))]:
        safe_item = _safe_profile_item(item, fallback_uid=item.get("uid"))
        if safe_item:
            result.append({**safe_item, **viewer_profile_projection(safe_item)})
    return result


def _safe_profile_uid(value: Any) -> str:
    text = public_text(value, max_len=120)
    if "[redacted]" in text:
        return ""
    return text


def _safe_profile_text(value: Any) -> str:
    return public_text(value, max_len=_MAX_PROFILE_TEXT)


def _path_is_writable(file: Path) -> bool:
    """Best-effort status probe without creating directories or files."""

    try:
        if file.exists() and (not file.is_file() or not os.access(str(file), os.W_OK)):
            return False
        directory = file.parent
        probe = directory
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return probe.is_dir() and os.access(str(probe), os.W_OK)
    except Exception:  # noqa: BLE001
        return False


def _profile_is_expired(item: dict[str, Any]) -> bool:
    observed_at: list[datetime] = []
    for value in (item.get("last_interaction_at"), item.get("last_seen_at")):
        raw = _safe_profile_text(value)
        if not raw:
            continue
        try:
            seen_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=timezone.utc)
        observed_at.append(seen_at)
    if not observed_at:
        return False
    return datetime.now(timezone.utc) - max(observed_at) > timedelta(
        days=_PROFILE_RETENTION_DAYS
    )


def _safe_profile_item(value: dict[str, Any], *, fallback_uid: Any) -> dict[str, Any]:
    uid = _safe_profile_uid(value.get("uid")) or _safe_profile_uid(fallback_uid)
    if not uid:
        return {}
    return ViewerProfile(
        uid=uid,
        nickname=_safe_profile_text(value.get("nickname")) or uid,
        avatar_url=_safe_profile_text(value.get("avatar_url")),
        first_seen_at=_safe_profile_text(value.get("first_seen_at")),
        last_seen_at=_safe_profile_text(value.get("last_seen_at")),
        roast_count=public_int(value.get("roast_count"), default=0, minimum=0),
        last_roast_at=_safe_profile_text(value.get("last_roast_at")),
        last_result=_safe_profile_text(value.get("last_result")),
        danmaku_count=public_int(value.get("danmaku_count"), default=0, minimum=0),
        preference_tags=safe_preference_counts(value.get("preference_tags")),
        favorite_topics=safe_preference_counts(value.get("favorite_topics")),
        running_jokes=safe_preference_counts(value.get("running_jokes")),
        interaction_style=_safe_profile_text(value.get("interaction_style")),
        response_preference=_safe_profile_text(value.get("response_preference")),
        last_interaction_summary=_safe_profile_text(value.get("last_interaction_summary")),
        impression_summary=_safe_profile_text(value.get("impression_summary")),
        avoid_guidance=_safe_profile_text(value.get("avoid_guidance")),
        last_interaction_at=_safe_profile_text(value.get("last_interaction_at")),
    ).to_dict()
