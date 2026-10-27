"""Session-local, memory-backed runtime state shared by bridge and settings.

Nothing in this module touches a filesystem. The payload lives in a
pagefile-backed section object, so a running bridge leaves no artifact behind
and a stopped bridge leaves no stale copy either: the object dies with its
last handle, which makes "no mapping" an exact liveness signal instead of the
best-effort guess a status file allows.

Layout is fixed-size and slot-addressed so both sides can map the section
without agreeing on anything beyond the slot index. A writer publishes an odd
sequence number before touching a slot and an even one after, which lets a
reader detect and discard a torn payload without taking a lock.
"""

from __future__ import annotations

import ctypes as C
import sys
import threading
from ctypes import wintypes as W
from typing import Optional


MAGIC = b"RMIPCS01"
LAYOUT_VERSION = 1
HEADER_BYTES = 16
SLOT_HEADER_BYTES = 16
SLOT_PAYLOAD_BYTES = 4096
SLOT_COUNT = 4
SECTION_BYTES = HEADER_BYTES + SLOT_COUNT * (SLOT_HEADER_BYTES + SLOT_PAYLOAD_BYTES)

SLOT_STATUS = 0
SLOT_KEY_DETECTION = 1

NAME_TEMPLATE = "Local\\RemoteMic.Runtime.{scope}"

PAGE_READWRITE = 0x04
FILE_MAP_WRITE = 0x0002
FILE_MAP_READ = 0x0004
FILE_MAP_ALL_ACCESS = 0xF001F

# Offsets inside a slot header.
_SEQ_OFFSET = 0
_LENGTH_OFFSET = 4


class SharedStateError(RuntimeError):
    pass


class SecurityAttributes(C.Structure):
    _fields_ = [
        ("length", W.DWORD),
        ("descriptor", C.c_void_p),
        ("inherit", W.BOOL),
    ]


def _kernel():
    if sys.platform != "win32":
        raise SharedStateError("windows_only")
    k = C.WinDLL("kernel32", use_last_error=True)
    k.CreateFileMappingW.restype = W.HANDLE
    k.CreateFileMappingW.argtypes = (
        W.HANDLE,
        C.POINTER(SecurityAttributes),
        W.DWORD,
        W.DWORD,
        W.DWORD,
        W.LPCWSTR,
    )
    k.OpenFileMappingW.restype = W.HANDLE
    k.OpenFileMappingW.argtypes = (W.DWORD, W.BOOL, W.LPCWSTR)
    k.MapViewOfFile.restype = C.c_void_p
    k.MapViewOfFile.argtypes = (W.HANDLE, W.DWORD, W.DWORD, W.DWORD, C.c_size_t)
    k.UnmapViewOfFile.restype = W.BOOL
    k.UnmapViewOfFile.argtypes = (C.c_void_p,)
    k.CloseHandle.restype = W.BOOL
    k.CloseHandle.argtypes = (W.HANDLE,)
    k.LocalFree.restype = C.c_void_p
    k.LocalFree.argtypes = (C.c_void_p,)
    return k


def _advapi():
    if sys.platform != "win32":
        raise SharedStateError("windows_only")
    a = C.WinDLL("advapi32", use_last_error=True)
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = W.BOOL
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        W.LPCWSTR,
        W.DWORD,
        C.POINTER(C.c_void_p),
        C.c_void_p,
    )
    return a


def section_name(scope: str) -> str:
    return NAME_TEMPLATE.format(scope=scope or "local")


def _security_descriptor(a, sid: str) -> C.c_void_p:
    """Own-user + SYSTEM full control, matching the bridge pipe ACL."""

    descriptor = C.c_void_p()
    text = f"D:P(A;;GA;;;{sid})(A;;GA;;;SY)"
    if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        text, 1, C.byref(descriptor), None
    ):
        raise SharedStateError("section_acl_failed")
    return descriptor


def _slot_offset(slot: int) -> int:
    if not 0 <= slot < SLOT_COUNT:
        raise SharedStateError("slot_out_of_range")
    return HEADER_BYTES + slot * (SLOT_HEADER_BYTES + SLOT_PAYLOAD_BYTES)


def _stamp(k, view: int) -> None:
    """Write the header once; a mapping created by someone else keeps theirs."""

    header = C.string_at(view, HEADER_BYTES)
    if header[: len(MAGIC)] == MAGIC:
        return
    C.memmove(view, MAGIC, len(MAGIC))
    C.c_uint32.from_address(view + 8).value = LAYOUT_VERSION


def _header_is_compatible(view: int) -> bool:
    header = C.string_at(view, HEADER_BYTES)
    if header[: len(MAGIC)] != MAGIC:
        return False
    return C.c_uint32.from_address(view + 8).value == LAYOUT_VERSION


class _Writer:
    """Long-lived mapping owned by the bridge process.

    Holding the handle for the process lifetime is deliberate: the section
    must not vanish between two heartbeats just because a reader closed its
    short-lived view.
    """

    def __init__(self, scope: str, sid: str) -> None:
        self.k = _kernel()
        self.a = _advapi()
        self.name = section_name(scope)
        if sid:
            descriptor = _security_descriptor(self.a, sid)
            attributes = SecurityAttributes(
                C.sizeof(SecurityAttributes), descriptor, False
            )
            try:
                self.handle = self.k.CreateFileMappingW(
                    None,
                    C.byref(attributes),
                    PAGE_READWRITE,
                    0,
                    SECTION_BYTES,
                    self.name,
                )
            finally:
                self.k.LocalFree(descriptor)
        else:
            self.handle = self.k.CreateFileMappingW(
                None, None, PAGE_READWRITE, 0, SECTION_BYTES, self.name
            )
        if not self.handle:
            raise SharedStateError("section_create_failed")
        self.view = self.k.MapViewOfFile(
            self.handle, FILE_MAP_ALL_ACCESS, 0, 0, SECTION_BYTES
        )
        if not self.view:
            self.k.CloseHandle(self.handle)
            self.handle = None
            raise SharedStateError("section_map_failed")
        _stamp(self.k, self.view)

    def close(self) -> None:
        if self.view:
            self.k.UnmapViewOfFile(self.view)
            self.view = None
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None

    def write(self, slot: int, payload: bytes) -> None:
        if len(payload) > SLOT_PAYLOAD_BYTES:
            raise SharedStateError("payload_too_large")
        offset = _slot_offset(slot)
        view = self.view
        seq = C.c_uint32.from_address(view + offset + _SEQ_OFFSET).value
        # Odd sequence marks the slot as being rewritten.
        C.c_uint32.from_address(view + offset + _SEQ_OFFSET).value = (seq + 1) | 1
        C.c_uint32.from_address(view + offset + _LENGTH_OFFSET).value = len(payload)
        if payload:
            C.memmove(view + offset + SLOT_HEADER_BYTES, payload, len(payload))
        C.c_uint32.from_address(view + offset + _SEQ_OFFSET).value = seq + 2


def _read_slot(view: int, slot: int) -> Optional[bytes]:
    offset = _slot_offset(slot)
    seq_before = C.c_uint32.from_address(view + offset + _SEQ_OFFSET).value
    if seq_before == 0 or seq_before & 1:
        return None
    length = C.c_uint32.from_address(view + offset + _LENGTH_OFFSET).value
    if length == 0 or length > SLOT_PAYLOAD_BYTES:
        return None
    data = C.string_at(view + offset + SLOT_HEADER_BYTES, length)
    seq_after = C.c_uint32.from_address(view + offset + _SEQ_OFFSET).value
    if seq_before != seq_after:
        return None
    return data


def read_slot(scope: str, slot: int) -> Optional[bytes]:
    """Read one slot through a short-lived mapping.

    Short-lived on purpose: keeping a handle here would keep a dead bridge's
    section alive and defeat the liveness guarantee.
    """

    k = _kernel()
    handle = k.OpenFileMappingW(FILE_MAP_READ, False, section_name(scope))
    if not handle:
        return None
    try:
        view = k.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, SECTION_BYTES)
        if not view:
            return None
        try:
            if not _header_is_compatible(view):
                return None
            return _read_slot(view, slot)
        finally:
            k.UnmapViewOfFile(view)
    except OSError:
        return None
    finally:
        k.CloseHandle(handle)


_writers: dict[str, _Writer] = {}
_writers_lock = threading.Lock()


def writer(scope: str, sid: str = "") -> _Writer:
    """Return the process-wide writer for ``scope``, creating it on demand."""

    key = section_name(scope)
    with _writers_lock:
        existing = _writers.get(key)
        if existing is not None:
            return existing
        created = _Writer(scope, sid)
        _writers[key] = created
        return created


def write_slot(scope: str, slot: int, payload: bytes, *, sid: str = "") -> None:
    writer(scope, sid).write(slot, payload)


def release_writers() -> None:
    """Drop every writer mapping; used by tests and on orderly shutdown."""

    with _writers_lock:
        for held in _writers.values():
            held.close()
        _writers.clear()


def section_exists(scope: str) -> bool:
    k = _kernel()
    handle = k.OpenFileMappingW(FILE_MAP_READ, False, section_name(scope))
    if not handle:
        return False
    k.CloseHandle(handle)
    return True
