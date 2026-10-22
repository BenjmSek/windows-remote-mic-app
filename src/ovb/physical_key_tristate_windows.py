"""Three-state behavior for one operator-chosen physical modifier key.

The desktop already owns a marker-based physicalizer hook for voice edges.
This module adds a *separate*, independent low-level hook that only watches
one physical key and decides what to do with a press:

* a short tap leaves the system alone, so the key keeps its native meaning;
* a hold past the threshold presses the configured voice shortcut, excluding
  the watched key itself because the operator's own finger already holds it;
* a second key cancels the shortcut and lets the native
  ``physical key + other key`` combination reach the system untouched. Before
  the threshold decides, nothing was injected yet and the chord stays purely
  physical; once the hold was applied, the injected keys are lifted first.

The hook never swallows an event it does not own: everything is forwarded
with ``CallNextHookEx``. Edges carrying this process's own voice marker are
ignored so the right-Alt mute path and the remote voice key stay intact.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import win32_input, win32_keys

WH_KEYBOARD_LL = 13
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
PM_REMOVE = 0x0001
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

LLKHF_INJECTED = 0x10
LLKHF_UP = 0x80

HOLD_THRESHOLD_SECONDS = 0.3
# The hold threshold above already *is* the operator's decision window: it is
# where the program recognises whether the press was a tap or a hold. A second
# wait after that decision only adds latency for an intent the operator settled
# before touching the key, so it ships disabled. Kept tunable: a non-zero value
# still buys a post-decision window in which no key has been injected yet.
GRACE_PERIOD_SECONDS = 0.0

# The watched key's press lifecycle. GRACE is a real state, not the absence of
# one: during it nothing has been injected yet, and any other key must cancel
# the pending shortcut so the native chord reaches Windows untouched.
PHASE_IDLE = "idle"
PHASE_PENDING = "pending"
PHASE_GRACE = "grace"
PHASE_HOLD = "hold"


class _KeyboardEvent(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("physical key tri-state requires Windows")


def _resolve_token_vks(tokens: Sequence[str]) -> Dict[str, int]:
    """Map every resolvable token to its virtual-key code.

    Unknown tokens are skipped instead of raising: the shortcut may be
    edited while the hook is running, and one bad token must not take the
    whole observer down.
    """

    mapping: Dict[str, int] = {}
    for token in tokens:
        try:
            codes = win32_keys.resolve_vk_codes((token,))
        except win32_keys.UnknownKeyTokenError:
            continue
        if codes:
            mapping[token] = codes[0]
    return mapping


def _cancel(hook: Optional[wintypes.HHOOK], thread_id: int) -> None:
    if hook is None:
        return
    try:
        ctypes.windll.user32.UnhookWindowsHookEx(hook)  # type: ignore[attr-defined]
    except Exception:
        pass
    if thread_id:
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.PostThreadMessageW.argtypes = (
                wintypes.DWORD,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.PostThreadMessageW.restype = wintypes.BOOL
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass


class PhysicalKeyTriStateController:
    """Watches one physical key and drives the voice shortcut on a hold."""

    def __init__(
        self,
        vk_code: int,
        shortcut_tokens: Sequence[str],
        *,
        hold_threshold: float = HOLD_THRESHOLD_SECONDS,
        grace_period: float = GRACE_PERIOD_SECONDS,
        logger: Optional[object] = None,
        ensure_physicalizer: Optional[Callable[[], bool]] = None,
        voice_press: Optional[Callable[[Sequence[str]], bool]] = None,
        voice_release: Optional[Callable[[Sequence[str]], bool]] = None,
    ) -> None:
        self.vk_code = int(vk_code)
        self.shortcut_tokens: Tuple[str, ...] = tuple(shortcut_tokens)
        self._shortcut_vks = _resolve_token_vks(self.shortcut_tokens)
        self.hold_threshold = float(hold_threshold)
        self.grace_period = float(grace_period)
        self._logger = logger
        # Marked delivery needs the process hook installed. Shortcuts that
        # never touch right Alt do not get it by default. Only the
        # hand-injecting fallback asks for it; the voice path below brings
        # its own hooks.
        self._ensure_physicalizer = ensure_physicalizer
        # Reuse the remote's own voice path end to end (input-profile switch,
        # Doubao Frida hook, provider control) instead of hand-rolling a
        # second sender. A hold behaves exactly like the BLE voice button.
        self._voice_press = voice_press
        self._voice_release = voice_release

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._hook: Optional[wintypes.HHOOK] = None
        self._thread_id = wintypes.DWORD(0)
        self._hookproc_keepalive = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._start_error: Optional[BaseException] = None

        self.phase = PHASE_IDLE
        self.hold_started_at = 0.0
        self._deadline = 0.0
        self._timer_token = 0
        # The exact tokens a successful hold put down. The release lifts
        # these, not whatever the shortcut says right now.
        self._sent_tokens: Tuple[str, ...] = ()

    # ``active``/``pending`` stay as readable aliases: the hook thread and the
    # runtime both describe a live hold with them.
    @property
    def active(self) -> bool:
        return self.phase == PHASE_HOLD

    @property
    def pending(self) -> bool:
        return self.phase in (PHASE_PENDING, PHASE_GRACE)

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def update_shortcut(self, tokens: Sequence[str]) -> None:
        with self._lock:
            self.shortcut_tokens = tuple(tokens)
            self._shortcut_vks = _resolve_token_vks(self.shortcut_tokens)

    def start(self, *, start_timeout: float = 5.0) -> None:
        _require_windows()
        if self.is_running:
            return
        self._ready.clear()
        self._stop.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._run,
            name="remote-mic-physical-key-tristate",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=start_timeout):
            self._stop.set()
            self._join()
            raise RuntimeError(
                f"physical key tri-state hook did not become ready within {start_timeout}s"
            )
        if self._start_error is not None:
            error = self._start_error
            self._stop.set()
            self._join()
            raise error

    def stop(self, *, join_timeout: float = 2.0) -> None:
        self._stop.set()
        hook, thread_id = None, 0
        with self._lock:
            hook = self._hook
            thread_id = int(self._thread_id.value)
            self._hook = None
        _cancel(hook, thread_id)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=join_timeout)
            if not thread.is_alive():
                self._thread = None
        # A hold that is still open would leave the shortcut keys down for
        # the whole session. Lift them before the state is discarded.
        self._release_injected_keys()
        with self._lock:
            self._reset_state_locked()

    def _join(self, timeout: float = 2.0) -> None:
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if not thread.is_alive():
            self._thread = None

    def _log(self, message: str, *args: object) -> None:
        logger = self._logger
        if logger is None:
            return
        try:
            logger.info(message, *args)
        except Exception:
            pass

    def _reset_state_locked(self) -> None:
        self.phase = PHASE_IDLE
        self.hold_started_at = 0.0
        self._deadline = 0.0
        self._timer_token += 1

    def _fallback_pump(self) -> None:
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.PeekMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.PeekMessageW.restype = wintypes.BOOL
            msg = wintypes.MSG()
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if int(msg.message) == WM_QUIT:
                    continue
        except Exception:
            pass

    def _ensure_marked_delivery(self) -> bool:
        """Install the marked-delivery hook before pressing anything.

        Without it ``send_voice_key_combo_down`` raises
        ``Win32InputUnavailableError`` and the hold would send nothing.
        """

        ensure = self._ensure_physicalizer
        if ensure is None:
            return True
        try:
            return bool(ensure())
        except Exception as exc:  # noqa: BLE001 - surfaced as a log, never fatal
            self._log(
                "physical key tri-state: marked delivery unavailable: %s", exc
            )
            return False

    def _send_down(self, tokens: Sequence[str]) -> bool:
        try:
            win32_input.send_voice_key_combo_down(
                list(tokens),
                physicalize=True,
                _key_down_query=lambda _vk: False,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - surfaced as a log, never fatal
            self._log(
                "physical key tri-state: shortcut down failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False

    def _send_up(self, tokens: Sequence[str]) -> None:
        try:
            win32_input.send_voice_key_combo_up(
                list(tokens),
                physicalize=True,
                _key_down_query=lambda _vk: False,
            )
        except Exception as exc:  # noqa: BLE001 - a stuck key must not kill the hook
            self._log("physical key tri-state: shortcut up failed: %s", exc)

    def _apply_hold(self) -> bool:
        """Drive the remote's own voice path, exactly like the BLE mic button.

        The preferred sender is ``voice_press``: it reuses the remote voice
        pipeline (input-profile switch, provider control, and for Doubao the
        in-process Frida hook). Hand-injecting the remaining keys is only a
        fallback, and it still needs the marked-delivery hook or the send is
        refused before it ever reaches the provider.
        """

        with self._lock:
            tokens = tuple(self.shortcut_tokens)
        # The operator's finger already holds the watched key, so it must NOT
        # be re-injected: the preflight guard refuses a key that is physically
        # down and the whole combo is dropped.
        inject = self._inject_tokens(tokens)
        if not inject:
            self._log(
                "physical key tri-state: shortcut skipped; nothing left to "
                "send without the watched key"
            )
            return False
        # The fallback sender needs the marked-delivery hook; the voice
        # pipeline installs its own hooks and does not ask for this one.
        press = self._voice_press
        if press is not None:
            try:
                delivered = bool(press(inject))
            except Exception as exc:  # noqa: BLE001 - surfaced as a log
                self._log(
                    "physical key tri-state: voice press failed: %s: %s",
                    type(exc).__name__,
                    exc,
                )
                return False
            if not delivered:
                self._log(
                    "physical key tri-state: voice press was not delivered"
                )
                return False
            self._remember_sent(inject)
            return True
        if not self._ensure_marked_delivery():
            self._log(
                "physical key tri-state: shortcut skipped; marked delivery "
                "hook is not installed"
            )
            return False
        if not self._send_down(inject):
            return False
        self._remember_sent(inject)
        return True

    def _inject_tokens(self, tokens: Sequence[str]) -> List[str]:
        """Everything the shortcut needs except the key the user is holding.

        A token whose virtual-key code cannot be resolved is kept: the send
        layer reports it as an unknown key, which is more useful than
        silently dropping it here.
        """

        with self._lock:
            vks = dict(self._shortcut_vks)
        return [
            token
            for token in tokens
            if vks.get(token) != self.vk_code
        ]

    def _remember_sent(self, tokens: Sequence[str]) -> None:
        """Record exactly what went down, so the release lifts the same keys.

        The shortcut may be reconfigured while the operator is still holding
        the key. Releasing the *current* shortcut then would lift keys that
        were never pressed and leave the pressed ones stuck.
        """

        with self._lock:
            self._sent_tokens = tuple(tokens)

    def _release_injected_keys(self) -> None:
        """Lift exactly what ``_apply_hold`` put down.

        The caller decides *whether* a release is owed: it captures the phase
        before resetting it, because once the phase is back to IDLE this state
        no longer records that the keys were ever pressed.
        """

        with self._lock:
            sent = self._sent_tokens
            self._sent_tokens = ()
        if not sent:
            return
        release = self._voice_release
        if release is not None:
            try:
                release(sent)
            except Exception as exc:  # noqa: BLE001 - a stuck key must not kill the hook
                self._log(
                    "physical key tri-state: voice release failed: %s: %s",
                    type(exc).__name__,
                    exc,
                )
            return
        self._send_up(sent)

    def _begin_pending(self) -> None:
        now = time.monotonic()
        with self._lock:
            self.phase = PHASE_PENDING
            self.hold_started_at = now
            self._deadline = now + self.hold_threshold
            token = self._timer_token
        timer = threading.Timer(
            self.hold_threshold,
            lambda: self._on_deadline(token),
        )
        timer.daemon = True
        timer.start()

    def _on_deadline(self, token: int) -> None:
        inject_now = False
        grace_token = 0
        with self._lock:
            if token != self._timer_token or self.phase != PHASE_PENDING:
                return
            if self.grace_period <= 0:
                # No post-decision window: the threshold just decided the
                # press was a hold, so inject without a second timer.
                self.phase = PHASE_HOLD
                inject_now = True
            else:
                # Nothing is injected yet. Give the operator a short window to
                # press a second key and get the native chord instead.
                self.phase = PHASE_GRACE
                self._deadline = time.monotonic() + self.grace_period
                grace_token = self._timer_token
        if inject_now:
            self._complete_hold()
            return
        timer = threading.Timer(
            self.grace_period,
            lambda: self._on_grace_expired(grace_token),
        )
        timer.daemon = True
        timer.start()

    def _on_grace_expired(self, token: int) -> None:
        with self._lock:
            if token != self._timer_token or self.phase != PHASE_GRACE:
                return
            if time.monotonic() < self._deadline:
                return
            # Claim the state before sending: the hook thread keeps running
            # while the keys go out, and a second grace timer must not start.
            self.phase = PHASE_HOLD
        self._complete_hold()

    def _complete_hold(self) -> None:
        applied = self._apply_hold()
        if not applied:
            with self._lock:
                if self.phase == PHASE_HOLD:
                    self.phase = PHASE_IDLE
            return
        self._log(
            "physical key tri-state: hold applied vk=0x%02x tokens=%s",
            self.vk_code,
            "+".join(self.shortcut_tokens),
        )

    def _handle_key_down(self, vk_code: int) -> None:
        """Return True when the caller must keep the event from the system."""

        if vk_code == self.vk_code:
            with self._lock:
                if self.phase != PHASE_IDLE:
                    return False
            self._begin_pending()
            return False
        with self._lock:
            if self.phase == PHASE_IDLE:
                return False
            # Any second key cancels the shortcut. In PENDING/GRACE nothing
            # was injected yet, so the chord is still purely physical and
            # Windows sees exactly ``watched key + this key``.
            self._timer_token += 1
            was_active = self.phase == PHASE_HOLD
            self.phase = PHASE_IDLE
            self._deadline = 0.0
        if was_active:
            self._release_injected_keys()
            self._log(
                "physical key tri-state: hold cancelled by vk=0x%02x; native combo passed through",
                vk_code,
            )
        return False

    def _handle_key_up(self, vk_code: int) -> None:
        if vk_code != self.vk_code:
            return
        with self._lock:
            self._timer_token += 1
            was_active = self.phase == PHASE_HOLD
            self.phase = PHASE_IDLE
            self.hold_started_at = 0.0
            self._deadline = 0.0
        if was_active:
            self._release_injected_keys()
            self._log(
                "physical key tri-state: hold released vk=0x%02x",
                self.vk_code,
            )

    def _hook_callback(
        self,
        code: int,
        message: int,
        pointer: wintypes.LPARAM,
        user32: object,
    ) -> int:
        try:
            if code >= 0 and message in (
                WM_KEYDOWN,
                WM_KEYUP,
                WM_SYSKEYDOWN,
                WM_SYSKEYUP,
            ):
                event = ctypes.cast(pointer, ctypes.POINTER(_KeyboardEvent)).contents
                flags = int(event.flags)
                if flags & LLKHF_INJECTED:
                    return int(user32.CallNextHookEx(None, code, message, pointer))
                vk_code = int(event.vkCode)
                if message in (WM_KEYUP, WM_SYSKEYUP):
                    self._handle_key_up(vk_code)
                else:
                    if flags & LLKHF_UP:
                        self._handle_key_up(vk_code)
                    else:
                        self._handle_key_down(vk_code)
        except Exception:
            pass
        return int(user32.CallNextHookEx(None, code, message, pointer))

    def _run(self) -> None:
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

            hook_proc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.CallNextHookEx.argtypes = (
                wintypes.HHOOK,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            user32.CallNextHookEx.restype = ctypes.c_ssize_t

            def callback(code, message, pointer):
                return self._hook_callback(code, int(message), pointer, user32)

            hookproc = hook_proc_type(callback)
            self._hookproc_keepalive = hookproc

            kernel32.GetCurrentThreadId.argtypes = ()
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            self._thread_id = wintypes.DWORD(kernel32.GetCurrentThreadId())

            kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            user32.SetWindowsHookExW.argtypes = (
                ctypes.c_int,
                hook_proc_type,
                wintypes.HINSTANCE,
                wintypes.DWORD,
            )
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
            user32.UnhookWindowsHookEx.restype = wintypes.BOOL
            user32.PeekMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.PeekMessageW.restype = wintypes.BOOL

            hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL,
                hookproc,
                kernel32.GetModuleHandleW(None),
                0,
            )
            if not hook:
                raise RuntimeError("SetWindowsHookExW failed")
            with self._lock:
                self._hook = hook

            # Create the message queue before reporting ready.
            probe = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(probe), None, 0, 0, PM_NOREMOVE)
            self._ready.set()

            msg = wintypes.MSG()
            user32.GetMessageW.argtypes = (
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            )
            user32.GetMessageW.restype = ctypes.c_int
            while not self._stop.is_set():
                if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) <= 0:
                    break
        except BaseException as exc:  # noqa: BLE001 - surfaced to start()
            self._start_error = exc
            self._ready.set()
        finally:
            hook = None
            with self._lock:
                hook = self._hook
                self._hook = None
            if hook:
                try:
                    ctypes.windll.user32.UnhookWindowsHookEx(hook)  # type: ignore[attr-defined]
                except Exception:
                    pass
            self._fallback_pump()
            with self._lock:
                self._reset_state_locked()
