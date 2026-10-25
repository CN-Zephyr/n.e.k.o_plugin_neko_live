"""Failures that degrade to a safe default must still leave a log trace."""

from types import SimpleNamespace

import pytest
from plugin.plugins.neko_live import NekoLivePlugin
from plugin.plugins.neko_live.core.pipeline import LivePipeline


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def info(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def _broken_probe():
    raise RuntimeError("cookie=SECRET")


def test_pipeline_probe_failure_degrades_and_warns_once_without_message():
    logger = _Logger()
    ctx = SimpleNamespace(
        plugin=SimpleNamespace(logger=logger),
        live_events=SimpleNamespace(new_viewer_burst_active=_broken_probe),
    )
    pipeline = LivePipeline(ctx)

    assert pipeline._live_speaker_burst_active() is False
    assert pipeline._live_speaker_burst_active() is False

    assert logger.warnings == [
        "[neko_live] pipeline probe failed probe=new_viewer_burst_active error_type=RuntimeError"
    ]
    assert "SECRET" not in "\n".join(logger.warnings)


def test_pipeline_probe_failure_without_logger_still_degrades():
    ctx = SimpleNamespace(live_events=SimpleNamespace(batch_welcome_available=_broken_probe))

    assert LivePipeline(ctx)._batch_welcome_available() is False


@pytest.mark.asyncio
async def test_entry_failure_returns_err_and_logs_type_only():
    class Runtime:
        async def bili_login_status(self):
            raise RuntimeError("cookie=SECRET")

    logger = _Logger()
    plugin = NekoLivePlugin(SimpleNamespace(logger=logger))
    plugin.runtime = Runtime()

    result = await plugin.bili_login_status()

    assert result.is_ok() is False
    assert logger.warnings == ["[neko_live] entry=bili_login_status failed error_type=RuntimeError"]
