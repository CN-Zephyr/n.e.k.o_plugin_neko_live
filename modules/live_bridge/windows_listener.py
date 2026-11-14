"""Verify that the Windows bridge owns the TCP listener it will use."""

from __future__ import annotations

import ctypes
import socket
from ctypes import wintypes

_TCP_TABLE_OWNER_PID_LISTENER = 3
_ERROR_INSUFFICIENT_BUFFER = 122
_MAX_TABLE_BYTES = 16 * 1024 * 1024
_TCP4_LOOPBACK = int.from_bytes(socket.inet_aton("127.0.0.1"), "little")
_TCP6_MAPPED_LOOPBACK = socket.inet_pton(socket.AF_INET6, "::ffff:127.0.0.1")


class _Tcp4Row(ctypes.Structure):
    _fields_ = [
        ("state", wintypes.DWORD),
        ("local_addr", wintypes.DWORD),
        ("local_port", wintypes.DWORD),
        ("remote_addr", wintypes.DWORD),
        ("remote_port", wintypes.DWORD),
        ("pid", wintypes.DWORD),
    ]


class _Tcp6Row(ctypes.Structure):
    _fields_ = [
        ("local_addr", ctypes.c_ubyte * 16),
        ("local_scope", wintypes.DWORD),
        ("local_port", wintypes.DWORD),
        ("remote_addr", ctypes.c_ubyte * 16),
        ("remote_scope", wintypes.DWORD),
        ("remote_port", wintypes.DWORD),
        ("state", wintypes.DWORD),
        ("pid", wintypes.DWORD),
    ]


def owns_listener(pid: int, port: int) -> bool:
    get_table = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    get_table.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        wintypes.ULONG, wintypes.DWORD, wintypes.ULONG,
    ]
    get_table.restype = wintypes.DWORD

    return any(
        row.pid == pid and socket.ntohs(row.local_port & 0xFFFF) == port and _reachable_from_loopback(row)
        for family, row_type in ((socket.AF_INET, _Tcp4Row), (socket.AF_INET6, _Tcp6Row))
        for row in _listeners(get_table, family, row_type)
    )


def _reachable_from_loopback(row: _Tcp4Row | _Tcp6Row) -> bool:
    if isinstance(row, _Tcp4Row):
        return row.local_addr in (0, _TCP4_LOOPBACK)
    return bytes(row.local_addr) in (bytes(16), _TCP6_MAPPED_LOOPBACK)


def _listeners(get_table, family: int, row_type):
    size = wintypes.DWORD()
    get_table(None, ctypes.byref(size), False, family, _TCP_TABLE_OWNER_PID_LISTENER, 0)
    for _ in range(3):
        if not 0 < size.value <= _MAX_TABLE_BYTES:
            break
        table = ctypes.create_string_buffer(size.value)
        result = get_table(table, ctypes.byref(size), False, family, _TCP_TABLE_OWNER_PID_LISTENER, 0)
        if result == _ERROR_INSUFFICIENT_BUFFER:
            continue
        if result != 0:
            raise ctypes.WinError(result)
        offset = ctypes.sizeof(wintypes.DWORD)
        count = wintypes.DWORD.from_buffer(table).value
        if offset + count * ctypes.sizeof(row_type) > ctypes.sizeof(table):
            break
        return (row_type * count).from_buffer(table, offset)
    raise OSError("Windows TCP listener table is unavailable")
