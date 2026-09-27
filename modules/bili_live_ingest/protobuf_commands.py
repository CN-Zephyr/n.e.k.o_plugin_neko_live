"""Bilibili 直播 protobuf 编码的 *_V2 指令 → 旧版 JSON 包形状。

2026-07 起 B 站灰度下发 ``SEND_GIFT_V2`` / ``INTERACT_WORD_V2``，业务字段放在
``data.pb``（base64 protobuf）里，外层不再有 ``giftName`` / ``uname`` 等字段。
这里只做最小的无 schema wire 解码，并把结果翻译成旧版 ``SEND_GIFT`` /
``INTERACT_WORD`` 包，让下游解析、EventBus 与 combo 聚合保持不变。

字段编号参考 xfgryujk/blivedm ``blivedm/models/pb.py``。
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Dict, List, Tuple

_WIRE_VARINT = 0
_WIRE_FIXED64 = 1
_WIRE_LEN = 2
_WIRE_FIXED32 = 5
_MAX_PB_BYTES = 256 * 1024


class ProtobufDecodeError(ValueError):
    pass


def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtobufDecodeError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift >= 70:
            raise ProtobufDecodeError("varint too long")


def decode_fields(buf: bytes) -> Dict[int, List[Any]]:
    """把一条 protobuf 消息解成 ``{字段号: [值, ...]}``；长度分隔字段保留 bytes。"""
    fields: Dict[int, List[Any]] = {}
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        number, wire = key >> 3, key & 0x07
        if number <= 0:
            raise ProtobufDecodeError("invalid field number")
        if wire == _WIRE_VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire == _WIRE_LEN:
            size, pos = _read_varint(buf, pos)
            if pos + size > len(buf):
                raise ProtobufDecodeError("truncated length-delimited field")
            value, pos = buf[pos:pos + size], pos + size
        elif wire == _WIRE_FIXED64:
            if pos + 8 > len(buf):
                raise ProtobufDecodeError("truncated fixed64")
            value, pos = int.from_bytes(buf[pos:pos + 8], "little"), pos + 8
        elif wire == _WIRE_FIXED32:
            if pos + 4 > len(buf):
                raise ProtobufDecodeError("truncated fixed32")
            value, pos = int.from_bytes(buf[pos:pos + 4], "little"), pos + 4
        else:
            raise ProtobufDecodeError(f"unsupported wire type {wire}")
        fields.setdefault(number, []).append(value)
    return fields


def _int(fields: Dict[int, List[Any]], number: int) -> int:
    for value in fields.get(number, ()):
        if isinstance(value, int):
            return value - (1 << 64) if value >= 1 << 63 else value
    return 0


def _str(fields: Dict[int, List[Any]], number: int) -> str:
    for value in fields.get(number, ()):
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
    return ""


def _msg(fields: Dict[int, List[Any]], number: int) -> Dict[int, List[Any]]:
    for value in fields.get(number, ()):
        if isinstance(value, bytes):
            return decode_fields(value)
    return {}


def _pb_bytes(packet: dict) -> bytes:
    inner = packet.get("data") if isinstance(packet, dict) else None
    raw = inner.get("pb") if isinstance(inner, dict) else None
    if not isinstance(raw, str) or not raw:
        raise ProtobufDecodeError("missing data.pb")
    if len(raw) > _MAX_PB_BYTES * 2:
        raise ProtobufDecodeError("data.pb too large")
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProtobufDecodeError("invalid base64 data.pb") from exc


def send_gift_v2_to_legacy(packet: dict) -> List[dict]:
    """``SEND_GIFT_V2`` → 若干条旧版 ``SEND_GIFT`` 包（一条 V2 可含多件礼物）。"""
    top = decode_fields(_pb_bytes(packet))
    medal = _msg(top, 8)
    blind = _msg(top, 9)
    user = {
        "uid": _int(top, 1),
        "uname": _str(top, 2),
        "face": _str(top, 3),
        "guard_level": _int(top, 5),
    }
    medal_info = {
        "medal_name": _str(medal, 6),
        "medal_level": _int(medal, 5),
        "target_id": _int(medal, 1),
        "anchor_roomid": _int(medal, 4),
    } if medal else {}
    blind_gift = {
        "original_gift_name": _str(blind, 3),
        "original_gift_price": _int(blind, 6),
    } if blind else None

    packets: List[dict] = []
    for raw_item in top.get(10, ()):
        if not isinstance(raw_item, bytes):
            continue
        item = decode_fields(raw_item)
        gift_name = _str(item, 2)
        if not gift_name:
            continue
        data = {
            **user,
            "giftId": _int(item, 1),
            "giftName": gift_name,
            "num": max(1, _int(item, 3)),
            "giftType": _int(item, 4),
            "price": _int(item, 5),
            "total_coin": _int(item, 7),
            "coin_type": _str(item, 8) or "silver",
            "tid": _str(item, 9),
            "timestamp": _int(item, 10),
            "rnd": _str(item, 12),
            "action": _str(item, 18),
            "medal_info": medal_info,
        }
        if blind_gift:
            data["blind_gift"] = blind_gift
        packets.append({"cmd": "SEND_GIFT", "data": data})
    return packets


def _rank_items(raw_items: List[Any]) -> List[dict]:
    items: List[dict] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, bytes):
            continue
        item = decode_fields(raw_item)
        items.append({
            "uid": _int(item, 1),
            "face": _str(item, 2),
            "score": _str(item, 3),
            "uname": _str(item, 4),
            "rank": _int(item, 5),
            "guard_level": _int(item, 6),
        })
    return items


def online_rank_v3_to_legacy(packet: dict) -> dict:
    """``ONLINE_RANK_V3`` → 旧版 ``ONLINE_RANK_V2`` 包。

    proto 参考 pskdje/bilibili_live_message_stream ``rankdb/v1.proto``：
    rank_type=1，高能榜 list=2，在线榜 online_list=3（实测在线榜走 3）。
    """
    top = decode_fields(_pb_bytes(packet))
    items = _rank_items(top.get(2, [])) or _rank_items(top.get(3, []))
    return {
        "cmd": "ONLINE_RANK_V2",
        "data": {"rank_type": _str(top, 1), "list": items},
    }


def interact_word_v2_to_legacy(packet: dict) -> dict:
    """``INTERACT_WORD_V2`` → 旧版 ``INTERACT_WORD`` 包（进场 / 关注 / 分享）。"""
    top = decode_fields(_pb_bytes(packet))
    base = _msg(_msg(top, 22), 2)
    return {
        "cmd": "INTERACT_WORD",
        "data": {
            "uid": _int(top, 1),
            "uname": _str(top, 2),
            "msg_type": _int(top, 5),
            "timestamp": _int(top, 7),
            "face": _str(base, 2),
        },
    }
