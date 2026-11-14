"""Protect local credential keys with the current Windows account."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_UI_FORBIDDEN = 1


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]


def protect_key(key: bytes) -> bytes:
    return _crypt_key("CryptProtectData", key)


def unprotect_key(protected: bytes) -> bytes:
    return _crypt_key("CryptUnprotectData", protected)


def _crypt_key(function: str, data: bytes) -> bytes:
    crypt = getattr(ctypes.WinDLL("crypt32", use_last_error=True), function)
    crypt.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,  # Optional description, unused in both directions.
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt.restype = wintypes.BOOL
    free = ctypes.WinDLL("kernel32").LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.addressof(buffer))
    result = _DataBlob()
    if not crypt(ctypes.byref(source), None, None, None, None, _UI_FORBIDDEN, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        free(result.data)
