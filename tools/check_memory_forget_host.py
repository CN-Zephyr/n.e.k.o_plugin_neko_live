"""Exercise plugin erasure against real host routes/stores with synthetic data.

Run from the host pyproject directory: uv run python
plugin/plugins/neko_live/tools/check_memory_forget_host.py

Uses ASGI in-process, without listeners, model calls or user data. This checks
the route/storage contract, not deployed host authentication or live acceptance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


def isolate(root: Path) -> None:
    """Install process-only isolation before importing host singletons."""
    sys.dont_write_bytecode = True
    tempfile.tempdir = str(root)
    host = Path.cwd().resolve()
    if not (host / "app/memory_server/routes.py").is_file():
        raise RuntimeError("run this check from the host project")
    sys.path.insert(0, str(host))

    def allowed(path) -> None:
        if isinstance(path, int):
            return
        if not Path(os.fsdecode(path)).resolve().is_relative_to(root):
            raise PermissionError("integration write escaped temporary root")

    def guard(event, args):
        if event == "open" and ((isinstance(args[1], str) and any(c in args[1] for c in "wax+")) or (args[2] or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
            allowed(args[0])
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.truncate"}:
            allowed(args[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            allowed(args[0])
            allowed(args[1])
        elif event in {"socket.connect", "socket.bind", "subprocess.Popen"}:
            raise PermissionError("integration external IO is disabled")

    sys.addaudithook(guard)
    # Only this short-lived process sees these roots. uv and the desktop host
    # retain their original environment and storage selection.
    os.environ["NEKO_STORAGE_SELECTED_ROOT"] = str(root / "data/N.E.K.O")
    os.environ["NEKO_STORAGE_ANCHOR_ROOT"] = str(root / "data/N.E.K.O")
    from utils import logger_config

    logs = root / "logs"
    logs.mkdir()
    logger_config.RobustLoggerConfig._get_log_directory = lambda self: logs
    from utils import config_manager

    cls = config_manager.ConfigManager
    cls._get_documents_directory = lambda self: root / "data"
    cls._get_standard_data_directory_candidates = lambda self: [root / "data"]
    cls._get_legacy_storage_candidates = lambda self: []
    cls._get_legacy_document_candidates = lambda self: []
    cls.get_legacy_app_root_candidates = lambda self: []
    cm = config_manager.get_config_manager(migrate=False)
    config_manager._config_manager_migrated = True
    assert Path(cm.memory_dir).resolve().is_relative_to(root)


async def exercise(root: Path) -> dict:
    import httpx
    from app.memory_server import idempotency, routes, runtime
    from fastapi import FastAPI
    from memory import trust_store
    from memory.fact_dedup import FactDedupResolver
    from memory.facts import FactStore
    from memory.persona import PersonaManager
    from memory.reflection import ReflectionEngine
    from memory.scopes import MemorySubject
    from plugin.plugins.neko_live.adapters.live_memory_bridge import LiveMemoryBridge
    from plugin.plugins.neko_live.adapters.live_memory_forget import forget_viewer
    from plugin.plugins.neko_live.core.contracts import LiveConfig, ViewerIdentity
    from plugin.plugins.neko_live.stores.memory_forget_store import MemoryForgetStore
    from plugin.plugins.neko_live.stores.viewer_store import ViewerStore
    from utils import internal_http_client

    trust_store.pool_path = lambda: str(root / "trust.json")
    trust_store.reset_for_tests()
    fs, persona = FactStore(), PersonaManager()
    reflection, dedup = ReflectionEngine(fs, persona), FactDedupResolver(fs)
    runtime.fact_store, runtime.persona_manager = fs, persona
    runtime.reflection_engine, runtime.fact_dedup_resolver = reflection, dedup
    name = "SyntheticCat"
    subjects = [MemorySubject.create("participant", uid) for uid in ("twitch:42", "twitch:43", "douyin:42")]
    chosen = subjects[0]
    assert Path(idempotency.keys_path(name)).resolve().is_relative_to(root)

    # Real serializers and persistence, deterministic synthetic fixtures. No LLM.
    facts = [{"id": f"fact_{i}", "text": f"synthetic {i}", **subject.as_entry_fields()} for i, subject in enumerate(subjects)]
    private = {"id": "private", "text": "synthetic private fact"}
    fs._facts[name] = [*facts, private]
    fs.save_facts(name)
    fs._facts["OtherCat"] = [facts[0]]
    fs.save_facts("OtherCat")
    persona.save_persona(name, {subject.persona_section_key: {
        **subject.as_entry_fields(), "facts": [{"id": f"persona_{i}", "text": f"synthetic {i}", **subject.as_entry_fields()}],
    } for i, subject in enumerate(subjects)})
    reflection.save_reflections(name, [{"id": f"reflection_{i}", "text": f"synthetic {i}", "status": "pending", **subject.as_entry_fields()} for i, subject in enumerate(subjects)])
    assert await dedup._asave_pending(name, [{"candidate_id": f"fact_{i}", "existing_id": f"other_{i}", "subject_key": subject.key, "scope": subject.scope} for i, subject in enumerate(subjects)])

    # Reuse the actual typed routes and generated OpenAPI; omit application
    # lifecycle/authentication middleware, which this harness does not exercise.
    app = FastAPI()
    for route in runtime.app.routes:
        if getattr(route, "endpoint", None) in {routes.forget_scoped_subject, routes.get_forget_epochs}:
            app.router.routes.append(route)
    assert len(app.router.routes) == 6
    calls = []
    lose_receipt = True

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        async def post(url, **kwargs):
            nonlocal lose_receipt
            result = await client.post(url, **kwargs)
            result.raise_for_status()
            calls.append((kwargs["json"], result.json()))
            if lose_receipt:
                lose_receipt = False
                raise TimeoutError("simulated lost receipt")
            return result

        internal_http_client.get_internal_http_client = lambda: SimpleNamespace(get=client.get, post=post)
        plugin_root = root / "plugin"
        plugin_root.mkdir()
        plugin = SimpleNamespace(data_path=lambda: plugin_root)

        def new_bridge():
            journal = MemoryForgetStore(plugin, Mock())
            store = ViewerStore(plugin, audit=None, write_blocked_provider=journal.blocks_profile)
            rt = SimpleNamespace(
                plugin=plugin, memory_forget_store=journal, viewer_store=store,
                config=LiveConfig(live_platform="twitch", live_enabled=False),
                safety_guard=SimpleNamespace(connected=False),
                dispatcher=SimpleNamespace(live_target_lanlan=lambda: name),
                live_provider=SimpleNamespace(platform="twitch"),
                pipeline=SimpleNamespace(session=SimpleNamespace(clear_uid=Mock())),
                audit=Mock(), _live_session_generation=1,
            )
            bridge = LiveMemoryBridge(rt)
            bridge._memory_port = AsyncMock(return_value=48912)
            return bridge

        async def erase(bridge):
            return await forget_viewer(bridge, uid="42", platform="twitch", target=name)

        bridge = new_bridge()
        await bridge.runtime.viewer_store.upsert_identity(ViewerIdentity(uid="42", nickname="synthetic"))
        try:
            await erase(bridge)
        except OSError as exc:
            assert str(exc) == "host_memory.erase_unconfirmed"
        else:
            raise AssertionError("lost receipt was not reported")
        assert len(calls) == 1 and calls[0][1]["status"] == "forgotten"
        assert {row["id"] for row in FactStore().load_facts_full(name)} == {"fact_1", "fact_2", "private"}
        assert FactStore().load_facts_full("OtherCat")[0]["id"] == "fact_0"
        persona_disk = json.loads(Path(persona._persona_path(name)).read_text(encoding="utf-8"))
        assert chosen.persona_section_key not in persona_disk
        assert all(subject.persona_section_key in persona_disk for subject in subjects[1:])
        assert {row["id"] for row in reflection.load_reflections(name)} == {"reflection_1", "reflection_2"}
        assert {row["candidate_id"] for row in json.loads(Path(dedup._pending_path(name)).read_text(encoding="utf-8"))} == {"fact_1", "fact_2"}
        assert len(await bridge.runtime.viewer_store.recent_profiles()) == 1

        # A host write after erasure must survive replay of the original epoch.
        fs._facts[name] = [*FactStore().load_facts_full(name), {"id": "new_fact", "text": "synthetic new generation", **chosen.as_entry_fields()}]
        fs.save_facts(name)
        bridge = new_bridge()
        assert len(calls) == 1 and bridge.forget_store.status()["pending"]
        assert (await erase(bridge))["forgotten"]
        assert len(calls) == 2 and calls[0][0] == calls[1][0]
        assert calls[1][1]["erased_subjects"] == []
        assert "new_fact" in {row["id"] for row in FactStore().load_facts_full(name)}
        assert not await bridge.runtime.viewer_store.recent_profiles()
        assert not new_bridge().forget_store.entries

        # A fresh second deletion is intentionally a new epoch. Its receipt is
        # durable before local failure; restart must not contact the host again.
        await bridge.runtime.viewer_store.upsert_identity(ViewerIdentity(uid="42", nickname="synthetic"))
        bridge.runtime.viewer_store.delete_profile = AsyncMock(side_effect=OSError("simulated local failure"))
        try:
            await erase(bridge)
        except OSError as exc:
            assert str(exc) == "host_memory.local_delete_failed"
        else:
            raise AssertionError("local failure was not reported")
        assert len(calls) == 3 and calls[2][0]["forget_epoch"] == calls[0][0]["forget_epoch"] + 1
        bridge = new_bridge()
        assert (await erase(bridge))["forgotten"]
        assert len(calls) == 3
        assert not new_bridge().forget_store.entries
        assert {row["id"] for row in FactStore().load_facts_full(name)} == {"fact_1", "fact_2", "private"}
        assert not await bridge.runtime.viewer_store.recent_profiles()
    trust_store.reset_for_tests()
    return {"passed": True, "host_posts": len(calls), "real_stores": ["facts", "persona", "reflection", "dedup", "forget_epochs", "viewer_profiles"], "lost_receipt_replay_preserved_new_memory": True, "local_retry_without_host_request": True, "other_scopes_preserved": True, "network_listeners": 0}


def main() -> None:
    workspace = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix=".memory-forget-check-", dir=workspace) as folder:
        root = Path(folder).resolve()
        if not root.is_relative_to(workspace.resolve()):
            raise RuntimeError("integration directory escaped workspace")
        # Windows creates a private socketpair for event-loop wakeups. Create
        # it before blocking network IO; all application IO remains guarded.
        try:
            with asyncio.Runner() as runner:
                runner.get_loop()
                isolate(root)
                result = runner.run(exercise(root))
        finally:
            logging.shutdown()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
