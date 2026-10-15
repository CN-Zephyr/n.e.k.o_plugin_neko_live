from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _require_git_checkout() -> str:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git executable not found")
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("requires Git checkout metadata to inspect tracked files")
    return git


def test_plugin_runtime_artifacts_are_ignored_for_distribution():
    git = _require_git_checkout()

    for path in (
        "plugin.toml.lock",
        ".codex-live-screen.png",
    ):
        completed = subprocess.run(
            [git, "check-ignore", "--no-index", "--quiet", path],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr or path


def test_neko_live_runtime_lock_is_not_tracked():
    git = _require_git_checkout()

    completed = subprocess.run(
        [git, "ls-files", "plugin.toml.lock"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout.strip() == ""


def test_distribution_copy_contains_verified_bridge_and_license(tmp_path):
    import hashlib

    from plugin.neko_plugin_cli.core.build import PluginBuilder
    from plugin.neko_plugin_cli.core.build_rules import load_build_rules

    destination = tmp_path / "payload"
    copied = PluginBuilder().copy_plugin_runtime_files(REPO_ROOT, destination, rules=load_build_rules(None))
    vendor = destination / "vendor" / "douyin_bridge" / "windows-amd64"
    binary = vendor / "douyinLive.exe"
    assert binary.resolve() in copied
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == (vendor / "CHECKSUMS.txt").read_text().split()[0]
    assert list((destination / "vendor" / "douyin_bridge").rglob("*LICENSE*"))
