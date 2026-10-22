"""Mute the system default playback endpoint while a voice hold is active.

Audio ducking keeps the loudspeaker quiet for the moment the voice key is
handed to the input method, so whatever the machine is playing cannot bleed
into the microphone the input method listens on.

Only the *system default render* endpoint is touched. The application's own
output endpoint (normally a virtual cable input) carries the remote audio the
input method must capture; muting it would cut the very signal being sent.

The user's own mute is authoritative: a device that is already muted is left
muted, and ducking never removes that mute on release.
"""

from __future__ import annotations

import atexit
import ctypes
import sys
import threading
import uuid
from contextlib import ExitStack
from typing import Callable, ContextManager, Optional

_P = ctypes.c_void_p
_U = ctypes.c_uint32
_I = ctypes.c_int32
_GUID = ctypes.c_ubyte * 16

_CLSID_MM_DEVICE_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
_IID_IMM_DEVICE_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
_IID_IAUDIO_ENDPOINT_VOLUME = "5CDF2C82-841E-4546-9722-0CF74078229A"
_EVENT_CONTEXT_GUID = "AD39D42D-6B86-4BF7-90B1-C18E25035170"

# IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eConsole, ...)
_E_RENDER = 0
_E_CONSOLE = 0
_CLSCTX_ALL = 23

_DEFAULT_RESTORE_DELAY_SECONDS = 0.3

_enabled_lock = threading.Lock()
_enabled = False


def _guid(value: str):
    return _GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


def _check(result: int) -> None:
    if result < 0:
        raise OSError(f"Core Audio HRESULT 0x{result & 0xffffffff:08x}")


def _call(pointer, index: int, types: tuple, *args) -> None:
    if not pointer:
        raise OSError("Core Audio returned a null interface")
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(_P))).contents
    method = ctypes.WINFUNCTYPE(_I, _P, *types)(table[index])
    _check(int(method(pointer, *args)))


def _release(pointer) -> None:
    if pointer:
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(_P))).contents
        ctypes.WINFUNCTYPE(_U, _P)(table[2])(pointer)


def set_enabled(value: bool) -> None:
    """Process-wide switch, applied by whoever owns the persisted config.

    A voice hold is handled by this process, and an independent worker cannot
    see a settings write, so the flag is pushed explicitly instead of being
    read back from disk on every keypress.
    """

    global _enabled
    with _enabled_lock:
        _enabled = bool(value)


def is_enabled() -> bool:
    with _enabled_lock:
        return _enabled


class DefaultEndpointVolume:
    """``IAudioEndpointVolume`` of the default playback device, per thread."""

    def __init__(self) -> None:
        self._ole = None
        self._resources = ExitStack()
        self._volume = None

    def _pointer(self):
        pointer = _P()
        self._resources.callback(_release, pointer)
        return pointer

    def __enter__(self):
        if sys.platform != "win32":
            raise OSError("audio ducking requires Windows Core Audio")
        try:
            self._ole = ctypes.WinDLL("ole32")
            self._ole.CoInitializeEx.argtypes = (_P, _U)
            self._ole.CoInitializeEx.restype = _I
            self._ole.CoUninitialize.argtypes = ()
            self._ole.CoUninitialize.restype = None
            self._ole.CoCreateInstance.argtypes = (_P, _P, _U, _P, _P)
            self._ole.CoCreateInstance.restype = _I
            result = int(self._ole.CoInitializeEx(None, 0))
            if result >= 0:
                self._resources.callback(self._ole.CoUninitialize)
            elif result & 0xffffffff != 0x80010106:  # Already STA is fine too.
                _check(result)
            enumerator = self._pointer()
            _check(int(self._ole.CoCreateInstance(
                ctypes.byref(_guid(_CLSID_MM_DEVICE_ENUMERATOR)),
                None,
                1,
                ctypes.byref(_guid(_IID_IMM_DEVICE_ENUMERATOR)),
                ctypes.byref(enumerator),
            )))
            device = self._pointer()
            _call(enumerator, 4, (_I, _I, _P), _E_RENDER, _E_CONSOLE,
                  ctypes.byref(device))
            self._volume = self._pointer()
            _call(device, 3, (_P, _U, _P, _P),
                  ctypes.byref(_guid(_IID_IAUDIO_ENDPOINT_VOLUME)),
                  _CLSCTX_ALL, None, ctypes.byref(self._volume))
            return self
        except Exception:
            self._resources.close()
            raise

    def __exit__(self, *_args) -> None:
        self._resources.close()

    @property
    def muted(self) -> bool:
        value = _I()
        _call(self._volume, 15, (_P,), ctypes.byref(value))
        return bool(value.value)

    def set_mute(self, muted: bool) -> None:
        _call(self._volume, 14, (_I, _P), 1 if muted else 0,
              ctypes.byref(_guid(_EVENT_CONTEXT_GUID)))


class PlaybackDucker:
    """Mute on voice start, restore on voice end, never fight the user."""

    def __init__(
        self,
        *,
        logger,
        enabled: Callable[[], bool] = is_enabled,
        endpoint_factory: Callable[[], ContextManager] = DefaultEndpointVolume,
        timer_factory=threading.Timer,
        restore_delay_seconds: float = _DEFAULT_RESTORE_DELAY_SECONDS,
    ) -> None:
        self._logger = logger
        self._enabled = enabled
        self._endpoint_factory = endpoint_factory
        self._timer_factory = timer_factory
        self._restore_delay = float(restore_delay_seconds)
        self._lock = threading.Lock()
        self._muted = False
        self._timer = None
        self._exit_hook_registered = False

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._muted

    def begin(self) -> bool:
        """Mute the default playback device unless it already is muted."""

        if not self._enabled():
            return False
        with self._lock:
            self._cancel_timer_locked()
            if self._muted:
                # A restore may still be pending from the previous hold.
                # Cancelling it here keeps the device muted for this hold
                # instead of letting that tail unmute it mid-press.
                return True
            try:
                with self._endpoint_factory() as endpoint:
                    previous = bool(endpoint.muted)
                    if not previous:
                        endpoint.set_mute(True)
            except Exception as exc:  # noqa: BLE001 - ducking is best effort
                self._muted = False
                self._logger.warning(
                    "audio ducking mute failed: %s", type(exc).__name__
                )
                return False
            self._muted = not previous
            if self._muted:
                self._register_exit_hook_locked()
            return self._muted

    def end(self, *, delay: Optional[float] = None) -> None:
        """Undo :meth:`begin`, after a short tail so key-up is not captured."""

        with self._lock:
            self._cancel_timer_locked()
            if not self._muted:
                return
            # Ownership stays with the ducker until the tail expires: a hold
            # that starts before then re-uses this mute instead of muting
            # twice and unmuting once.
            wait = self._restore_delay if delay is None else float(delay)
            if wait <= 0:
                self._restore_locked()
                return
            try:
                timer = self._timer_factory(wait, self._restore_locked)
                timer.daemon = True
                self._timer = timer
                timer.start()
            except Exception:  # noqa: BLE001 - never leave the device muted
                self._logger.exception("audio ducking restore timer failed")
                self._restore_locked()

    def restore_now(self) -> None:
        with self._lock:
            self._cancel_timer_locked()
            if self._muted:
                self._restore_locked()

    def _restore_locked(self) -> None:
        self._timer = None
        self._muted = False
        try:
            with self._endpoint_factory() as endpoint:
                endpoint.set_mute(False)
        except Exception as exc:  # noqa: BLE001 - logged, cannot be retried
            self._logger.warning(
                "audio ducking restore failed: %s", type(exc).__name__
            )

    def _cancel_timer_locked(self) -> None:
        timer = self._timer
        self._timer = None
        if timer is not None:
            cancel = getattr(timer, "cancel", None)
            if callable(cancel):
                cancel()

    def _register_exit_hook_locked(self) -> None:
        if self._exit_hook_registered:
            return
        try:
            atexit.register(self.restore_now)
        except Exception:  # noqa: BLE001 - a missing hook only risks a mute
            return
        self._exit_hook_registered = True
