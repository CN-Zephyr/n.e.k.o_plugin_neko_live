"""SEND_GIFT_V2 / INTERACT_WORD_V2 / ONLINE_RANK_V3（data.pb protobuf）与 USER_TOAST_MSG_V2 解码与分发回归。

2026-07 起 B 站灰度下发 V2 指令，旧分发只认 SEND_GIFT / INTERACT_WORD，
导致真实直播间的礼物全部被当成未知消息丢弃。
"""

from __future__ import annotations

import base64

import pytest
from plugin.plugins.neko_live.modules.bili_live_ingest import BiliLiveIngestModule
from plugin.plugins.neko_live.modules.bili_live_ingest.danmaku_core import DanmakuListener
from plugin.plugins.neko_live.modules.bili_live_ingest.livedanmaku import LiveDanmaku
from plugin.plugins.neko_live.modules.bili_live_ingest.protobuf_commands import (
    ProtobufDecodeError,
    decode_fields,
    interact_word_v2_to_legacy,
    online_rank_v3_to_legacy,
    send_gift_v2_to_legacy,
)


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _int_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _len_field(number: int, value: bytes | str) -> bytes:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return _varint((number << 3) | 2) + _varint(len(raw)) + raw


def _gift_item(name: str, num: int, total_coin: int, coin_type: str, tid: str) -> bytes:
    return b"".join([
        _int_field(1, 31036),
        _len_field(2, name),
        _int_field(3, num),
        _int_field(5, total_coin // num),
        _int_field(7, total_coin),
        _len_field(8, coin_type),
        _len_field(9, tid),
        _int_field(10, 1790000000),
        _len_field(12, "rnd-1"),
        _len_field(18, "投喂"),
    ])


def _send_gift_v2(*items: bytes) -> dict:
    medal = _int_field(5, 12) + _len_field(6, "喵团")
    body = b"".join([
        _int_field(1, 424242),
        _len_field(2, "viewer"),
        _int_field(5, 3),
        _len_field(8, medal),
        *(_len_field(10, item) for item in items),
    ])
    return {"cmd": "SEND_GIFT_V2", "data": {"dmscore": 8, "pb": base64.b64encode(body).decode()}}


def _interact_word_v2(msg_type: int) -> dict:
    body = _int_field(1, 777) + _len_field(2, "entrant") + _int_field(5, msg_type) + _int_field(7, 1790000001)
    return {"cmd": "INTERACT_WORD_V2", "data": {"dmscore": 1, "pb": base64.b64encode(body).decode()}}


def test_send_gift_v2_translates_every_gift_item_to_legacy_packet():
    packets = send_gift_v2_to_legacy(_send_gift_v2(
        _gift_item("小花花", 1, 100, "gold", "tid-1"),
        _gift_item("辣条", 3, 300, "silver", "tid-2"),
    ))

    assert [p["cmd"] for p in packets] == ["SEND_GIFT", "SEND_GIFT"]
    first, second = (p["data"] for p in packets)
    assert first["uid"] == 424242
    assert first["uname"] == "viewer"
    assert first["guard_level"] == 3
    assert first["giftName"] == "小花花"
    assert first["num"] == 1
    assert first["total_coin"] == 100
    assert first["coin_type"] == "gold"
    assert first["tid"] == "tid-1"
    assert first["medal_info"] == {"medal_name": "喵团", "medal_level": 12, "target_id": 0, "anchor_roomid": 0}
    assert (second["giftName"], second["num"], second["coin_type"]) == ("辣条", 3, "silver")


def test_translated_gift_parses_with_existing_legacy_parser():
    packet = send_gift_v2_to_legacy(_send_gift_v2(_gift_item("薯条", 2, 200, "gold", "tid-3")))[0]

    event = LiveDanmaku.from_gift(packet)

    assert event.gift.gift_name == "薯条"
    assert event.gift.num == 2
    assert event.gift.total_coin == 200
    assert event.gift.coin_type == "gold"


async def test_send_gift_v2_dispatches_as_send_gift_with_support_metadata():
    got: list[tuple[str, object]] = []
    listener = DanmakuListener(room_id=100, callbacks={"on_event": lambda cmd, event: got.append((cmd, event))})

    await listener._dispatch_message("SEND_GIFT_V2", _send_gift_v2(_gift_item("小花花", 1, 100, "gold", "tid-9")))

    assert [cmd for cmd, _ in got] == ["SEND_GIFT"]
    event = got[0][1]
    assert event.gift.gift_name == "小花花"
    assert event.provider_event_id == "tid-9"
    assert event.provider_timestamp_ms == 1790000000 * 1000

    live_event = BiliLiveIngestModule()._to_live_event("SEND_GIFT", event)
    assert live_event.type == "gift"


async def test_interact_word_v2_dispatches_entry_and_follow():
    got: list[tuple[str, object]] = []
    entries: list[str] = []
    follows: list[str] = []
    listener = DanmakuListener(
        room_id=100,
        callbacks={
            "on_event": lambda cmd, event: got.append((cmd, event)),
            "on_entry": entries.append,
            "on_follow": follows.append,
        },
    )

    await listener._dispatch_message("INTERACT_WORD_V2", _interact_word_v2(1))
    await listener._dispatch_message("INTERACT_WORD_V2", _interact_word_v2(2))

    assert [cmd for cmd, _ in got] == ["INTERACT_WORD", "INTERACT_WORD"]
    assert got[0][1].uid == 777
    assert entries == ["entrant"]
    assert follows == ["entrant"]
    assert interact_word_v2_to_legacy(_interact_word_v2(1))["data"]["msg_type"] == 1


@pytest.mark.parametrize(
    "packet",
    [
        {"cmd": "SEND_GIFT_V2", "data": {}},
        {"cmd": "SEND_GIFT_V2", "data": {"pb": "not base64!!"}},
        {"cmd": "SEND_GIFT_V2", "data": {"pb": base64.b64encode(b"\x0a\x05ab").decode()}},
        {"cmd": "SEND_GIFT_V2", "data": "junk"},
    ],
)
async def test_malformed_v2_packets_are_dropped_without_events(packet):
    got: list[tuple[str, object]] = []
    listener = DanmakuListener(room_id=100, callbacks={"on_event": lambda cmd, event: got.append((cmd, event))})

    await listener._dispatch_message("SEND_GIFT_V2", packet)

    assert got == []


def test_decode_fields_rejects_truncated_varint():
    with pytest.raises(ProtobufDecodeError):
        decode_fields(b"\x08\xff")


def _rank_item(uid: int, uname: str, rank: int, score: str) -> bytes:
    return _int_field(1, uid) + _len_field(3, score) + _len_field(4, uname) + _int_field(5, rank)


def _online_rank_v3(field: int, *items: bytes) -> dict:
    body = _len_field(1, "online_rank") + b"".join(_len_field(field, item) for item in items)
    return {"cmd": "ONLINE_RANK_V3", "data": {"pb": base64.b64encode(body).decode()}}


@pytest.mark.parametrize("list_field", [2, 3])
def test_online_rank_v3_translates_to_legacy_v2(list_field):
    packet = online_rank_v3_to_legacy(_online_rank_v3(
        list_field, _rank_item(1, "first", 1, "520"), _rank_item(2, "second", 2, "100"),
    ))

    assert packet["cmd"] == "ONLINE_RANK_V2"
    assert packet["data"]["rank_type"] == "online_rank"
    assert [(i["uid"], i["uname"], i["rank"], i["score"]) for i in packet["data"]["list"]] == [
        (1, "first", 1, "520"),
        (2, "second", 2, "100"),
    ]


async def test_online_rank_v3_dispatches_as_online_rank_event():
    got: list[tuple[str, object]] = []
    listener = DanmakuListener(room_id=100, callbacks={"on_event": lambda cmd, event: got.append((cmd, event))})

    await listener._dispatch_message("ONLINE_RANK_V3", _online_rank_v3(2, _rank_item(1, "first", 1, "520")))

    assert [cmd for cmd, _ in got] == ["ONLINE_RANK_V2"]
    assert "first" in got[0][1].text


def _user_toast_v2(source: int, *, uid: int = 555, guard_level: int = 3) -> dict:
    return {
        "cmd": "USER_TOAST_MSG_V2",
        "data": {
            "sender_uinfo": {"uid": uid, "base": {"name": "captain", "face": ""}},
            "guard_info": {"guard_level": guard_level, "start_time": 1790000100, "end_time": 1790000100},
            "pay_info": {"num": 1, "price": 138000, "unit": "月"},
            "gift_info": {"gift_id": 10003},
            "option": {"source": source},
            "toast_msg": "captain 开通了舰长",
        },
    }


async def test_user_toast_v2_paid_source_dispatches_guard_once():
    got: list[tuple[str, object]] = []
    listener = DanmakuListener(room_id=100, callbacks={"on_event": lambda cmd, event: got.append((cmd, event))})

    await listener._dispatch_message("USER_TOAST_MSG_V2", _user_toast_v2(0))
    await listener._dispatch_message("USER_TOAST_MSG_V2", _user_toast_v2(2))

    assert [cmd for cmd, _ in got] == ["GUARD_BUY"]
    event = got[0][1]
    assert event.uid == 555
    assert event.nickname == "captain"
    live_event = BiliLiveIngestModule()._to_live_event("GUARD_BUY", event)
    assert live_event.type == "guard"


async def test_guard_buy_and_user_toast_v2_for_same_purchase_emit_once():
    got: list[tuple[str, object]] = []
    listener = DanmakuListener(room_id=100, callbacks={"on_event": lambda cmd, event: got.append((cmd, event))})
    legacy = {"cmd": "GUARD_BUY", "data": {"uid": 555, "username": "captain", "guard_level": 3, "num": 1,
                                           "price": 138000, "gift_name": "舰长", "start_time": 1790000100}}

    await listener._dispatch_message("GUARD_BUY", legacy)
    await listener._dispatch_message("USER_TOAST_MSG_V2", _user_toast_v2(0))
    await listener._dispatch_message("USER_TOAST_MSG_V2", _user_toast_v2(0, uid=556))

    assert [event.uid for _, event in got] == [555, 556]


@pytest.mark.parametrize(
    "packet",
    [
        {"cmd": "USER_TOAST_MSG_V2", "data": "junk"},
        {"cmd": "USER_TOAST_MSG_V2", "data": {"option": {"source": 0}}},
        {"cmd": "USER_TOAST_MSG_V2", "data": {"option": "x", "guard_info": {"guard_level": 3}}},
    ],
)
async def test_malformed_user_toast_v2_is_dropped(packet):
    got: list[tuple[str, object]] = []
    listener = DanmakuListener(room_id=100, callbacks={"on_event": lambda cmd, event: got.append((cmd, event))})

    await listener._dispatch_message("USER_TOAST_MSG_V2", packet)

    assert got == []
