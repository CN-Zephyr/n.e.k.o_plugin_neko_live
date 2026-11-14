from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
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


def test_distribution_excludes_douyin_executable_and_preserves_package_files(tmp_path):
    import hashlib

    from plugin.neko_plugin_cli.core.build import PluginBuilder
    from plugin.neko_plugin_cli.core.build_rules import load_build_rules

    destination = tmp_path / "payload"
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        rules = load_build_rules(tomllib.load(handle))
    assert "artifacts" in rules.exclude_dirs
    copied = PluginBuilder().copy_plugin_runtime_files(REPO_ROOT, destination, rules=rules)
    bridge_path = Path("vendor/douyin_bridge/windows-amd64")
    source_bridge = REPO_ROOT / bridge_path
    source_binary = source_bridge / "douyinLive.exe"
    expected = (source_bridge / "CHECKSUMS.txt").read_text().split()[0]
    assert hashlib.sha256(source_binary.read_bytes()).hexdigest() == expected
    assert not (destination / bridge_path / "douyinLive.exe").exists()
    assert not (destination / "artifacts").exists()
    assert (destination / bridge_path / "LICENSE.douyinLive").resolve() in copied
    for name in ("LICENSE", "NOTICE", "config.example.toml", "pyproject.toml"):
        assert (destination / name).resolve() in copied


def test_market_workflows_use_one_pinned_host_revision():
    workflows = REPO_ROOT / ".github" / "workflows"
    revisions = []
    for name in ("verify", "release"):
        source = (workflows / f"{name}.yml").read_text(encoding="utf-8")
        workflow = re.search(rf"plugin-market-{name}\.yml@([a-f0-9]{{40}})$", source, re.MULTILINE)
        checkout = re.search(r"^ *neko-ref: ([a-f0-9]{40})$", source, re.MULTILINE)
        assert workflow and checkout and workflow[1] == checkout[1]
        revisions.append(workflow[1])
    panel = (workflows / "panel.yml").read_text(encoding="utf-8")
    assert f"ref: {revisions[0]}" in panel
    assert revisions[0] == revisions[1]
    assert all(re.fullmatch(r"[a-f0-9]{40}", ref) for ref in re.findall(r"uses: [^\s@]+@(\S+)", panel))
    verify = (workflows / "verify.yml").read_text(encoding="utf-8")
    assert f"ref: {revisions[0]}" in verify
    assert "runs-on: windows-2025" in verify
    assert "test_credential_store.py" in verify and "test_live_bridge.py" in verify
    release = (workflows / "release.yml").read_text(encoding="utf-8")
    assert f"RELEASE_HOST_SHA: {revisions[0]}" in release
    assert "needs: release" in release
    assert '--signer-digest "$RELEASE_HOST_SHA"' in release
    assert '--source-digest "$SOURCE_SHA"' in release
