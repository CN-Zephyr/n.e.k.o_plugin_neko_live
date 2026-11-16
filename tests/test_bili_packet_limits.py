from __future__ import annotations

import json
import sys
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from plugin.plugins.neko_live.modules.bili_live_ingest import danmaku_core as packets


@pytest.mark.parametrize("codec_name,version", [("zlib", packets.PROTOCOL_VERSION_ZLIB), ("brotli", packets.PROTOCOL_VERSION_BROTLI)])
@pytest.mark.parametrize("size", [128, 129])
def test_decompression_enforces_output_budget(monkeypatch, codec_name, version, size):
    codec = zlib if codec_name == "zlib" else pytest.importorskip("brotli", minversion="1.2.0")
    source = b"x" * size
    compressed = codec.compress(source)

    def unbounded_decompress(*_args):
        raise AssertionError("unbounded decompression must not run")

    monkeypatch.setattr(codec, "decompress", unbounded_decompress)
    if size == 128:
        assert packets._decompress(compressed, version, max_bytes=128) == source
    else:
        with pytest.raises(ValueError, match="packet_decompression_limit_exceeded"):
            packets._decompress(compressed, version, max_bytes=128)


@pytest.mark.parametrize("codec_name,version", [("zlib", packets.PROTOCOL_VERSION_ZLIB), ("brotli", packets.PROTOCOL_VERSION_BROTLI)])
def test_decompression_rejects_incomplete_stream(codec_name, version):
    codec = zlib if codec_name == "zlib" else pytest.importorskip("brotli", minversion="1.2.0")
    with pytest.raises(ValueError, match="packet_compressed_stream_incomplete"):
        packets._decompress(codec.compress(b"sample message")[:-1], version)


def test_brotli_without_bounded_output_fails_closed(monkeypatch):
    class LegacyDecoder:
        def process(self, data):
            raise AssertionError("legacy unbounded decompression must not run")

    monkeypatch.setitem(sys.modules, "brotli", SimpleNamespace(Decompressor=LegacyDecoder))
    with pytest.raises(ValueError, match="packet_bounded_brotli_unavailable"):
        packets._decompress(b"sample", packets.PROTOCOL_VERSION_BROTLI)


def _message():
    return packets._pack(packets.OPERATION_SEND_MSG, json.dumps({"cmd": "TEST", "data": "hello"}).encode(), 0)


def _compressed(body):
    return packets._pack(packets.OPERATION_SEND_MSG, zlib.compress(body), packets.PROTOCOL_VERSION_ZLIB)


@pytest.mark.asyncio
async def test_normal_nested_packet_reaches_dispatch():
    listener = packets.DanmakuListener(123)
    listener._dispatch_message = AsyncMock()

    await listener._process_packet(_compressed(_compressed(_message())))

    listener._dispatch_message.assert_awaited_once_with("TEST", {"cmd": "TEST", "data": "hello"})


@pytest.mark.asyncio
async def test_nested_siblings_share_output_budget_and_next_message_recovers(monkeypatch):
    message = _message()
    siblings = _compressed(message) * 2
    monkeypatch.setattr(packets, "_MAX_DECOMPRESSED_BYTES", len(siblings) + 2 * len(message) - 1)
    listener = packets.DanmakuListener(123)
    listener._dispatch_message = AsyncMock()

    await listener._process_packet(_compressed(siblings))
    assert listener._dispatch_message.await_count == 1
    await listener._process_packet(_compressed(message))
    assert listener._dispatch_message.await_count == 2


@pytest.mark.asyncio
async def test_compression_depth_is_bounded_and_next_message_recovers(monkeypatch):
    monkeypatch.setattr(packets, "_MAX_COMPRESSION_DEPTH", 1)
    listener = packets.DanmakuListener(123)
    listener._dispatch_message = AsyncMock()

    await listener._process_packet(_compressed(_compressed(_message())))
    listener._dispatch_message.assert_not_awaited()
    await listener._process_packet(_compressed(_message()))
    listener._dispatch_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_wire_size_limit_is_checked_before_dispatch(monkeypatch):
    message = _message()
    monkeypatch.setattr(packets, "_MAX_PACKET_BYTES", len(message))
    listener = packets.DanmakuListener(123)
    listener._dispatch_message = AsyncMock()

    await listener._process_packet(message + b"x")
    listener._dispatch_message.assert_not_awaited()
    await listener._process_packet(message)
    listener._dispatch_message.assert_awaited_once()
