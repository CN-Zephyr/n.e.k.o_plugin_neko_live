from __future__ import annotations

import runpy
from pathlib import Path


def test_distributed_panel_matches_authored_sources():
    root = Path(__file__).resolve().parents[1]
    builder = runpy.run_path(str(root / "tools" / "build_panel_compat.py"))["build"]
    assert (root / "ui" / "panel_compat.tsx").read_text(encoding="utf-8") == builder()
