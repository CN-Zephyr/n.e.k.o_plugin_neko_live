"""Measure synthetic profile writes and event-loop lag; never open user stores.

Run from the host pyproject directory with uv run python and this script path.
All generated profiles live in a verified workspace temporary directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from plugin.plugins.neko_live.core.contracts import ViewerIdentity, ViewerProfile
from plugin.plugins.neko_live.stores.viewer_store import ViewerStore


def summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {name: round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)], 2)
            for name, fraction in (("p50_ms", 0.5), ("p95_ms", 0.95), ("max_ms", 1.0))}


async def measure(count: int, iterations: int, rich: bool) -> dict:
    workspace = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix=".viewer-store-bench-", dir=workspace) as folder:
        directory = Path(folder).resolve()
        if not directory.is_relative_to(workspace.resolve()):
            raise RuntimeError("benchmark directory escaped workspace")
        file = directory / "viewer_profiles.json"
        now = datetime.now(timezone.utc).isoformat()
        history = {"impression_summary": "测" * 180, "last_interaction_summary": "测" * 160, "last_result": "测" * 30} if rich else {}
        seed = {str(i): ViewerProfile(uid=str(i), nickname="synthetic viewer", first_seen_at=now, last_seen_at=now, **history).to_dict() for i in range(count)}
        store = ViewerStore(SimpleNamespace(data_path=lambda: directory), SimpleNamespace(record=lambda *args, **kwargs: None))
        # Seed through the store's normal bounded atomic writer, outside timing.
        if not await asyncio.to_thread(store._write_json, file, seed):
            raise RuntimeError("benchmark seed could not be saved")
        identity = ViewerIdentity(uid="0", nickname="synthetic viewer")
        await store.record_live_danmaku(identity, "synthetic warmup")
        latencies, lags = [], []
        stopped = asyncio.Event()

        async def sample_loop():
            while not stopped.is_set():
                expected = time.perf_counter() + 0.005
                await asyncio.sleep(0.005)
                lags.append(max(0.0, (time.perf_counter() - expected) * 1000))

        sampler = asyncio.create_task(sample_loop())
        await asyncio.sleep(0)
        try:
            for index in range(iterations):
                started = time.perf_counter()
                profile = await store.record_live_danmaku(identity, f"synthetic sample {index}")
                if profile.danmaku_count != index + 2 or store.storage_status()["capacity_reasons"]:
                    raise RuntimeError("benchmark write was not persisted")
                latencies.append((time.perf_counter() - started) * 1000)
        finally:
            stopped.set()
            await sampler
        persisted = json.loads(file.read_text(encoding="utf-8"))
        if len(persisted) != count or persisted["0"]["danmaku_count"] != iterations + 1:
            raise RuntimeError("benchmark final persisted result mismatch")
        return {"profiles": count, "rich_profiles": rich, "file_bytes": file.stat().st_size, "samples": iterations,
                "record_live_danmaku": summary(latencies), "loop_lag_5ms_sampler": summary(lags)}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=int, nargs="+", default=[1000, 10000])
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--rich", action="store_true", help="populate bounded synthetic history fields near the file-size limit")
    args = parser.parse_args()
    if not 5 <= args.iterations <= 100 or not 1 <= len(args.profiles) <= 4 or any(not 1 <= count <= 10000 for count in args.profiles):
        parser.error("use 1-4 profile counts in 1..10000 and 5..100 iterations")
    results = [await measure(count, args.iterations, args.rich) for count in args.profiles]
    print(json.dumps({"synthetic_only": True, "python": platform.python_version(), "platform": platform.system(), "results": results}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
