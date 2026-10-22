"""PySide6-Essentials + Qt Quick/QML settings window (/),
replacing the previous Tk view. Every validation/save/launch/log-status
decision below still goes straight through the same pure functions in
``settings_ui.py`` - this module only bridges them to QML via a
`QAbstractListModel` (``ButtonMappingModel``) and two `QObject`s
(``SettingsController``, ``DiagnosticsController``); it never duplicates
that logic.

This module deliberately does NOT import PySide6 at module import time.
``_load_qt_classes()`` performs that import lazily (and caches the result),
so merely importing ``ovb.qt_settings_app`` - e.g. transitively via
``settings_ui`` in a ``--dry-run`` smoke check, or from a pure test that
only needs ``remote_layout``/``shell_targets`` - never requires PySide6 to
be installed. ``run_settings_window()`` is the only real entry point, and
raises ``QtUnavailableError`` with an actionable message if PySide6-
Essentials is missing (source/dev runs only: the frozen build always bundles
the Qt runtime itself - see build/RemoteMicChromecast.spec - so end users
never need to separately install Python or Qt).

That lazy import carries an ordering constraint: importing any
``winrt.windows.*`` projection *before* the first Qt import faults the
interpreter inside ``PySide6.QtCore`` (access violation, Qt 6.12 /
winrt-runtime 3.2.1 on CPython 3.14, reproduced 2026-10-19 - Qt first is
fine, and the reverse order is the only failing one). A full
``unittest discover`` run hides it because collection imports PySide6
before any test executes; running a single test module on its own does
not, which is how ``tests/test_chromecast_voice.py`` used to crash. The
GUI process is safe today only because it never imports ``winrt`` (BLE
lives in the worker process) - keep it that way, or call
``_load_qt_classes()`` at startup before any device scan touches WinRT.

``DiagnosticsController`` ('s "检查与修复" fourth page) runs every
``windows_diagnostics.run_diagnostics()`` check on a plain background
``threading.Thread`` - never on the Qt GUI thread, so a slow WinRT/PortAudio
call can never freeze the window - and delivers the result back via a
cross-thread Qt signal (``Signal(object)``), which Qt automatically queues
onto the GUI thread because the receiving ``QObject`` was constructed there;
this is the standard, documented way to marshal a background-thread result
back to a Qt object living on a different thread, and needs no ``QThread``
subclass or extra locking. An ``_is_refreshing`` guard refuses to start a
second worker while one is already running (repeated "重新检测" clicks never
overlap).

Thread lifecycle at window close / process exit: every background thread this
controller starts is tracked in ``_diagnostics_threads``, and
``_shutdown_diagnostics_workers()`` signals shutdown and makes a BOUNDED
join attempt on each (``_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS`` per
thread - DERIVED from ``windows_diagnostics.BLE_DISCOVERY_MAX_CANCELLATION_
SECONDS`` plus a safety margin, not an independently-guessed value - see
that constant's own definition below). The critical fix is WHEN this runs:
``run_settings_window()`` now calls it EXPLICITLY, synchronously, from a
``try/finally`` that starts right after ``DiagnosticsController`` is
constructed (i.e. right after its background worker could first exist) and
covers every exit path through ``app.exec()`` returning, ``engine.load()``
raising, or ``rootObjects()`` coming back empty - while every Qt/Python
object it built is still fully alive - not only via this module's
``atexit`` hook (``_shutdown_qt_settings_app_at_exit()``, still registered
as a defense-in-depth safety net for callers that bypass
``run_settings_window()``). A real Windows CI faulthandler dump proved the
old atexit-only timing insufficient: a background BLE-discovery worker was
still deep inside a native WinRT await when the interpreter itself began
finalizing, producing an ``0xC0000005`` access violation - daemon=True only
guarantees CPython does not block exit on a surviving thread, it says
nothing about whether that thread's native call can safely keep running
concurrently with interpreter teardown.

The round-1 fix for the discovery side itself - cancelling the asyncio Task
awaiting the WinRT device enumeration - was ALSO insufficient: the locked pywinrt wrapper's own post-cancel wait is
itself unbounded (see ``windows_diagnostics.py``'s "-- BLE candidate --"
section for the exact source citation), so an in-process asyncio
cancellation request could never give a real hard bound either. BLE
candidate discovery therefore now runs in a genuinely separate, disposable
OS PROCESS (``windows_diagnostics._run_ble_diagnostics_subprocess()``) that
the parent can forcibly terminate/kill and CONFIRM dead within a real,
OS-enforced bound - the shutdown event doubles as that cancellation signal
(not just an emit-skip flag - see below), so a discovery attempt in flight
when shutdown begins is actually asked to stop and confirmed to have
stopped, giving the bounded join here a realistic chance to succeed instead
of only ever timing out. Every one of these threads is still created with
``daemon=True`` too, as a last-resort backstop if a future failure mode
ever defeats the process-level isolation above.

Shutdown-vs-teardown ordering ( still true under):
a diagnostics worker still finishing around shutdown time must never emit
its result INTO a ``DiagnosticsController``/Qt runtime that
``_release_qt_classes_cache()`` may already have started tearing down. Two
things make this safe: ``_diagnostics_shutdown_event`` (a module-level
``threading.Event``) is set FIRST, before anything else, by
``_shutdown_diagnostics_workers()`` - ``refreshDiagnostics()`` refuses to
start a new worker once it is set, and a worker already running checks it
immediately before emitting and skips the emit entirely if it is set (any
exception the emit call raises anyway - the receiver could still be
mid-teardown despite the check - is caught and discarded, never crashing
the worker thread); and every worker's ``finally`` block unconditionally
calls ``_forget_diagnostics_thread()``, so the registry is cleaned up even
on that path. ``_shutdown_qt_settings_app_at_exit()`` still runs all three
shutdown steps (flag the shutdown, join outstanding workers, release the Qt
classes cache) in that explicit order, in one function - never relying on
Python's ``atexit`` LIFO-ordered execution of separately registered
functions (see that function's docstring for the full story).
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
import gc
import os
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from . import (
    __version__,
    action_executor,
    remote_selection,
    audio_playback,
    audio_output,
    bridge_control_windows,
    bridge_launcher,
    bridge_runtime_status,
    config,
    device_catalog,
    element_navigation_control_windows,
    element_navigation_runtime,
    frida_compat,
    hid_elevation_windows,
    hid_helper_consumers,
    hotkey,
    hotkey_capture_windows,
    key_detection_bridge,
    key_mapping,
    mapping_presets,
    log_export,
    logging_setup,
    product_identity,
    dev_session,
    remote_layout,
    raw_input_windows,
    resources,
    settings_ui,
    shell_targets,
    single_instance,
    startup_windows,
    vb_cable_bundle,
    voice_hotkey_sync_windows,
    voice_program_manager,
    wetype_control_windows,
    win32_keys,
    window_chrome_windows,
    windows_diagnostics,
)

_VOICE_HOTKEY_TASK_TIMEOUT_MS = 2500


@dataclass
class _VoiceHotkeyTransaction:
    """Own one side-effecting provider transaction across all of its steps."""

    owner_key: str
    provider_id: str
    trigger: str
    settings_revision: int
    cancel_event: threading.Event
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _worker_token: int = 0
    _worker_active: bool = False
    _terminal_payload: object = None
    _reconciled: bool = False

    def begin_worker(self, token: int) -> None:
        with self._lock:
            self._worker_token = token
            self._worker_active = True
            self._terminal_payload = None

    def record_terminal(self, token: int, payload: object) -> None:
        with self._lock:
            if token != self._worker_token:
                return
            self._worker_active = False
            self._terminal_payload = payload

    def consume_terminal(self, token: int) -> bool:
        with self._lock:
            if token != self._worker_token or self._terminal_payload is None:
                return False
            self._terminal_payload = None
            return True

    def mark_reconciled(self) -> None:
        with self._lock:
            self._reconciled = True
            self._terminal_payload = None

    def is_unsettled(self) -> bool:
        with self._lock:
            return self._worker_active or (
                self._terminal_payload is not None and not self._reconciled
            )

    def matches(self, config_value: dict, provider_id: str, trigger: str,
                settings_revision: int) -> bool:
        return (
            provider_id == self.provider_id
            and trigger == self.trigger
            and settings_revision == self.settings_revision
            and remote_selection.active_key(config_value) == self.owner_key
        )

# Only the physical microphone button can own remote audio. Other buttons use
# ordinary actions; secondary gestures are ordinary actions for every button.
_ORDINARY_PRIMARY_ACTION_OPTIONS: List[str] = list(
    dict.fromkeys(
        settings_ui._PRESET_KEY_COMBOS
    )
)
_MIC_PRIMARY_ACTION_OPTIONS: List[str] = list(
    dict.fromkeys(
        settings_ui._PRIMARY_VOICE_DISPLAYS + tuple(_ORDINARY_PRIMARY_ACTION_OPTIONS)
    )
)
_SECONDARY_ACTION_OPTIONS: List[str] = list(
    dict.fromkeys(
        (settings_ui.SECONDARY_UNCONFIGURED_DISPLAY,)
        + tuple(
            option
            for option in settings_ui._PRESET_KEY_COMBOS
            if option != "禁用"
        )
    )
)

class QtUnavailableError(RuntimeError):
    """Raised by run_settings_window() when PySide6-Essentials is not
    importable. See module docstring.
    """


def _apply_application_icon(app, window, icon_type, icon_path: Path) -> None:
    """Applies one state icon to Qt's default and the loaded native window."""

    icon = icon_type(str(icon_path))
    set_application_icon = getattr(app, "setWindowIcon", None)
    if callable(set_application_icon):
        set_application_icon(icon)
    set_window_icon = getattr(window, "setIcon", None)
    if callable(set_window_icon):
        set_window_icon(icon)


def _apply_application_identity(app) -> None:
    """Applies the shared user-visible name to Qt's process identity."""

    set_application_name = getattr(app, "setApplicationName", None)
    if callable(set_application_name):
        set_application_name(product_identity.DISPLAY_NAME)


def _connect_application_exit(app, controller) -> None:
    """Make full exit a host responsibility instead of relying only on QML."""

    controller.applicationExitReady.connect(app.quit)


def _qml_directory() -> Path:
    """Locates the ``qml/`` directory this module's QML files live in,
    mirroring resources.py's frozen-vs-source-checkout lookup: in a frozen
    (PyInstaller) build, ``build/RemoteMicChromecast.spec`` collects the
    qml sources under ``ovb_qml`` inside the COLLECT output, which the
    bootloader exposes via ``sys._MEIPASS`` at runtime (see resources.py's
    module docstring for why ``sys._MEIPASS`` - not the exe's own directory
    - is the correct base path for bundled, non-Python data in a one-dir
    build). In an unfrozen (source checkout or ``pip install``) run, the
    qml/ directory is simply this module's own sibling directory.
    """

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "ovb_qml"
    return Path(__file__).resolve().parent / "qml"


def _settings_window_handle(window: object) -> int:
    if sys.platform != "win32":
        return 0
    try:
        hwnd = int(window.winId())  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return 0
    return hwnd if hwnd > 0 else 0


def _mark_settings_window_for_activation(
    window: object,
    *,
    hwnd: Optional[int] = None,
) -> bool:
    """Mark the real native QML window for duplicate-launch reactivation."""

    resolved_hwnd = _settings_window_handle(window) if hwnd is None else int(hwnd)
    if not resolved_hwnd:
        return False
    return single_instance.mark_settings_window(resolved_hwnd)


_qt_classes_cache: Optional[dict] = None

# Every background diagnostics worker thread DiagnosticsController.
# refreshDiagnostics() starts is appended here (and discarded once it
# finishes - see _forget_diagnostics_thread()), purely so tests/diagnostics
# can observe how many are currently tracked. This is NOT what makes thread
# cleanup safe - see module docstring's "Thread lifecycle at process exit"
# section  _join_diagnostics_threads_at_exit()
# below only ever makes a bounded, best-effort join attempt; the actual
# safety property (process exit is never blocked indefinitely) comes from
# every one of these threads being created with daemon=True.
_diagnostics_threads: "list[threading.Thread]" = []
_diagnostics_threads_lock = threading.Lock()

# Set exactly once, by _begin_diagnostics_shutdown() (only ever called from
# _shutdown_qt_settings_app_at_exit() in production - see that function's
# docstring), BEFORE anything else happens at process exit (
# 2). refreshDiagnostics() refuses to start a new worker once this is set;
# a worker already running checks it immediately before emitting its result
# and skips the emit entirely if it is set, so a diagnostics result can
# never be delivered into a DiagnosticsController/Qt runtime that may
# already be mid-teardown. Tests that set this directly (rather than via
# _shutdown_qt_settings_app_at_exit()) MUST .clear() it afterward - it is
# process-global, persistent state, not per-test.
_diagnostics_shutdown_event = threading.Event()

# Cross-controller gate for the explicit active audio test. It prevents the
# connection page from saving a different endpoint or launching the bridge
# while the synthetic signal is in flight.
_vb_cable_test_active_event = threading.Event()

# Tracks the multi-step "detect and save CABLE Input" workflow across the
# DiagnosticsController and SettingsController. Full application exit waits
# for this event to clear so a late enumeration/preflight result cannot race
# Qt teardown or persist settings after exit has begun.
_driver_action_active_event = threading.Event()

# Safety margin on top of the longest diagnostics-child cancellation bound
# below. It covers the worker thread's own small amount of Python cleanup
# after the BLE or active-audio subprocess has been confirmed stopped.
_DIAGNOSTICS_THREAD_JOIN_SAFETY_MARGIN_SECONDS = 2.0

# Per-thread bound for the best-effort atexit join below - a module-level
# constant (rather than a literal inline) specifically so a test can lower
# it and prove the join is genuinely bounded/non-hanging without waiting
# out the real default (see tests/test_qt_settings_app.py).
#
# derive this from the subprocess layer's own
# cancellation constants, rather than an independent flat guess. The max()
# keeps both BLE discovery and the explicit VB-CABLE child inside the same
# bounded shutdown contract if either implementation changes later.
_VB_CABLE_BRIDGE_RECOVERY_SECONDS = (
    bridge_control_windows.DEFAULT_EXIT_TIMEOUT_SECONDS
    + bridge_launcher.DEFAULT_GRACE_CHECKS
    * bridge_launcher.DEFAULT_POLL_INTERVAL_SECONDS
)
_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS = (
    max(
        windows_diagnostics.BLE_DISCOVERY_MAX_CANCELLATION_SECONDS,
        windows_diagnostics.VB_CABLE_LOOPBACK_MAX_CANCELLATION_SECONDS
        + _VB_CABLE_BRIDGE_RECOVERY_SECONDS,
        windows_diagnostics.OUTPUT_ENDPOINT_PREFLIGHT_MAX_CANCELLATION_SECONDS,
    )
    + _DIAGNOSTICS_THREAD_JOIN_SAFETY_MARGIN_SECONDS
)

# A cancelled input start can spend up to five seconds waiting for its native
# hook/window to become ready, then another five seconds stopping the Raw
# Input listener and HID tap. Endpoint preflight has its own process-level
# cancellation bound. Derive the shared join ceiling from the longer path and
# leave a small margin for Python cleanup before Qt objects are released.
_INPUT_WORKER_MAX_START_SECONDS = 5.0
_INPUT_WORKER_MAX_STOP_SECONDS = 5.0
_SETTINGS_BACKGROUND_JOIN_SAFETY_MARGIN_SECONDS = 2.0
_SETTINGS_BACKGROUND_JOIN_TIMEOUT_SECONDS = (
    max(
        _INPUT_WORKER_MAX_START_SECONDS + _INPUT_WORKER_MAX_STOP_SECONDS,
        windows_diagnostics.OUTPUT_ENDPOINT_PREFLIGHT_MAX_CANCELLATION_SECONDS,
    )
    + _SETTINGS_BACKGROUND_JOIN_SAFETY_MARGIN_SECONDS
)
_APPLICATION_EXIT_WAIT_SAFETY_MARGIN_SECONDS = 2.0
_APPLICATION_EXIT_WAIT_TIMEOUT_SECONDS = (
    _SETTINGS_BACKGROUND_JOIN_TIMEOUT_SECONDS
    + bridge_control_windows.DEFAULT_EXIT_TIMEOUT_SECONDS
    + _APPLICATION_EXIT_WAIT_SAFETY_MARGIN_SECONDS
)
_APPLICATION_EXIT_SAVE_WAIT_TIMEOUT_SECONDS = (
    windows_diagnostics.OUTPUT_ENDPOINT_PREFLIGHT_PROCESS_TIMEOUT_SECONDS
    + windows_diagnostics.OUTPUT_ENDPOINT_PREFLIGHT_MAX_CANCELLATION_SECONDS
    + _SETTINGS_BACKGROUND_JOIN_SAFETY_MARGIN_SECONDS
)
_APPLICATION_EXIT_POLL_INTERVAL_MS = 100
_BRIDGE_STATUS_STALE_AFTER_SECONDS = 20.0
_BRIDGE_STATUS_MISSING_AFTER_SECONDS = _BRIDGE_STATUS_STALE_AFTER_SECONDS
_BRIDGE_RECONNECT_BUSY_TIMEOUT_MS = 10_000


@dataclass(frozen=True)
class _VbCableTestWorkflowResult:
    loopback_result: object = None
    bridge_was_running: bool = False
    stop_error: str = ""
    restart_result: object = None
    restart_skipped_for_exit: bool = False


@dataclass(frozen=True)
class _InputStartResult:
    kind: str
    ok: bool
    message: str = ""
    hotkey_capture: object = None
    bridge_request: object = None
    listener: object = None
    tap: object = None
    failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class _InputStopResult:
    kind: str
    ok: bool
    message: str = ""
    hotkey_capture: object = None
    bridge_request: object = None
    listener: object = None
    tap: object = None


class _AnyEvent:
    """Read-only event view that is set when any source event is set."""

    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


def _remember_diagnostics_thread(thread: "threading.Thread") -> None:
    with _diagnostics_threads_lock:
        _diagnostics_threads.append(thread)


def _forget_diagnostics_thread(thread: "threading.Thread") -> None:
    with _diagnostics_threads_lock:
        if thread in _diagnostics_threads:
            _diagnostics_threads.remove(thread)


def _begin_diagnostics_shutdown() -> None:
    """Flags that process shutdown has begun - see
    ``_diagnostics_shutdown_event``'s own comment above and
    ``_shutdown_qt_settings_app_at_exit()``'s docstring below for the full
    ordering contract this is one step of. Idempotent (``Event.set()`` is
    always safe to call more than once); kept as its own tiny function
    purely so a test can call/assert on this ONE step in isolation from the
    join/cache-release steps that follow it.
    """

    _diagnostics_shutdown_event.set()


def _join_diagnostics_threads_at_exit() -> None:
    """Best-effort courtesy only - see module docstring's "Thread lifecycle
    at process exit" section. Never treat this function returning as proof
    every thread it attempted to join has actually stopped. Deliberately
    does NOT itself touch ``_diagnostics_shutdown_event`` (that is
    ``_begin_diagnostics_shutdown()``'s job, called separately, first, by
    ``_shutdown_qt_settings_app_at_exit()``) - kept orthogonal so tests can
    still call this function alone (as tests already do)
    without it leaving global shutdown state behind for later tests.
    """

    with _diagnostics_threads_lock:
        threads = list(_diagnostics_threads)
    for thread in threads:
        thread.join(timeout=_DIAGNOSTICS_THREAD_JOIN_TIMEOUT_SECONDS)


def _release_qt_classes_cache() -> None:
    """Drops this module's cached dynamically-created
    ``QObject``/``QAbstractListModel`` subclasses and asks the cyclic
    garbage collector to reclaim them while the interpreter is still fully
    alive, rather than leaving that to CPython's own shutdown-time
    finalization pass. Only ever called from
    ``_shutdown_qt_settings_app_at_exit()`` below (see that function's
    docstring for why it must run LAST, after diagnostics shutdown/join).

    Root cause this works around: ``PySide6.QtCore.Property`` descriptors
    hold a reference cycle back to their owning class that shiboken's C
    extension type does not fully support ``tp_clear`` for - reproduced
    with a minimal, completely unrelated repro (a single trivial
    ``Property``-having ``QObject`` subclass, left referenced at module
    scope with no closures, no caching, nothing else from this project
    involved). ``gc.collect()`` called explicitly *before* shutdown resolves
    that same cycle cleanly; the identical cycle left for ``Py_FinalizeEx``'s
    own final collection pass instead prints
    ``ResourceWarning: gc: N uncollectable objects at shutdown`` - a
    substring this project's ``windows-google-ci.yml`` test-suite step
    explicitly greps for and fails the build on (see
    tests/test_qt_lifecycle_cleanup.py for the subprocess-based regression
    proving this exact command stays clean).

    A deliberate no-op whenever ``_load_qt_classes()`` was never actually
    called in this process (the cache is still ``None``), which covers
    every non-Qt test process and ``--dry-run``.
    """

    global _qt_classes_cache
    if _qt_classes_cache is None:
        return
    _qt_classes_cache = None
    gc.collect()


def _shutdown_diagnostics_workers() -> None:
    """The ONE production shutdown contract for in-flight diagnostics
    workers  flag shutdown, THEN bounded-wait for every tracked
    worker thread - steps 1+2 of ``_shutdown_qt_settings_app_at_exit()``'s
    three steps, factored out into their own function so both
    ``run_settings_window()`` (called explicitly right after ``app.exec()``
    returns, while every Qt/Python object it built is still fully alive -
    see that function) and the real QML load probe
    (``tests/test_qt_settings_app.py``'s ``_QML_LOAD_PROBE_SCRIPT``, which
    reproduces a settings window closing before a real background BLE
    discovery finishes) call the EXACT SAME helper, instead of each
    reimplementing shutdown or relying solely on this module's ``atexit``
    hook.

    Why this matters ( red evidence): a Windows CI
    run's faulthandler dump showed the real crash thread still deep inside a
    WinRT ``find_all_async_aqs_filter`` await, called from a
    ``DiagnosticsController`` background worker, well after this module's
    own ``atexit``-only join had already returned (its 2-second best-effort
    bound elapsing without the thread actually stopping) - by the time the
    interpreter itself began finalizing, that native WinRT call was still
    running concurrently with CPython/shiboken teardown, producing the
    observed ``0xC0000005`` access violation. Calling this function
    EXPLICITLY, synchronously, at the natural point the window is closing
    (not only via ``atexit``, which can fire arbitrarily late relative to
    Qt/native object teardown) gives every in-flight BLE discovery a real,
    bounded chance to be forcibly terminated and CONFIRMED dead at the OS
    process level (see
    ``windows_diagnostics._run_ble_diagnostics_subprocess()``) before that
    teardown ever begins - not merely a best-effort join on a thread nothing
    ever asked to stop.

    Idempotent - ``_begin_diagnostics_shutdown()`` is (``Event.set()`` is
    always safe to call more than once) and ``_join_diagnostics_threads_at_
    exit()`` is (an empty/already-finished registry joins instantly) - safe
    to call once here and again later via ``_shutdown_qt_settings_app_at_
    exit()`` as a defense-in-depth safety net for any path that does not go
    through ``run_settings_window()``.
    """

    _begin_diagnostics_shutdown()
    _join_diagnostics_threads_at_exit()


def _shutdown_qt_settings_app_at_exit() -> None:
    """The ONLY function this module registers via ``atexit``. Runs every
    shutdown step in one explicit, hard-coded order:

    1.-2. ``_shutdown_diagnostics_workers()`` - flags shutdown, then makes a
       bounded join attempt on every tracked worker thread (see that
       function's own docstring for why this is also called explicitly by
       ``run_settings_window()``, not only reached here);
    3. ``_release_qt_classes_cache()`` - only now, after every worker has
       either finished or had its bounded join time out, release the
       cached Qt classes.

    Why this is ONE function instead of separately ``atexit.register()``-ing
    each step (which is what the original fix did): ``atexit`` runs its registered functions in LIFO order
    (last registered, first executed) - registering the join hook first and
    the cache-release hook second, as before, meant the cache release
    actually ran FIRST at real process exit, the reverse of the order this
    module's own docstring already claimed. A diagnostics worker finishing
    right around that window could then emit its result into a
    ``DiagnosticsController``/Qt runtime whose cached classes were already
    being torn down. Collapsing every step into one function and
    registering that ONE function removes the dependency on ``atexit``'s
    LIFO ordering (and the silent-breakage risk of some future edit
    reordering two separate ``atexit.register()`` calls) entirely - the
    order is just ordinary, explicit Python statement order here.

    This remains registered as a defense-in-depth safety net (e.g. for a
    caller that never reaches ``run_settings_window()``'s own explicit
    call) - production windows must never depend on ``atexit`` alone; see
    ``_shutdown_diagnostics_workers()``'s docstring for why.
    """

    _shutdown_diagnostics_workers()
    _release_qt_classes_cache()


atexit.register(_shutdown_qt_settings_app_at_exit)


def _load_qt_classes() -> dict:
    """Imports PySide6 and returns every QObject/QAbstractListModel subclass
    this module needs, DEFINED IN ``settings_qt_classes`` - not here - so
    that importing ``qt_settings_app`` itself never requires PySide6 (see
    module docstring and that module's own docstring). Cached after the first
    successful call within a process; a missing-PySide6 failure is never
    cached, so a caller that installs PySide6 into the same running process
    (unlikely in practice, but exercised by tests) would see a subsequent
    call succeed.
    """

    global _qt_classes_cache
    if _qt_classes_cache is not None:
        return _qt_classes_cache

    try:
        from PySide6.QtCore import (
            Property,
            QAbstractListModel,
            QByteArray,
            QEvent,
            QModelIndex,
            QObject,
            QStandardPaths,
            QTimer,
            Qt,
            QUrl,
            Signal,
            Slot,
        )
        from PySide6.QtGui import QIcon
        from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterSingletonInstance
        from PySide6.QtQuickControls2 import QQuickStyle
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        raise QtUnavailableError(
            "PySide6-Essentials 未安装，无法打开 Qt 设置界面。源码运行请先在本项目"
            "的虚拟环境中执行 `pip install PySide6-Essentials`；打包后的 "
            f"{product_identity.windows_executable_name(__version__)} 自带 Qt 运行"
            "时，不需要终端用户单独安装 Python 或 Qt。"
        ) from exc

    # Imported here, not at module level: settings_qt_classes imports this
    # module's constants back, so a top-level import would be circular.
    from . import settings_qt_classes

    classes = settings_qt_classes.build(
        Property=Property,
        QAbstractListModel=QAbstractListModel,
        QApplication=QApplication,
        QByteArray=QByteArray,
        QEvent=QEvent,
        QModelIndex=QModelIndex,
        QObject=QObject,
        QStandardPaths=QStandardPaths,
        QTimer=QTimer,
        Qt=Qt,
        QUrl=QUrl,
        Signal=Signal,
        Slot=Slot,
    )

    _qt_classes_cache = {
        # Keep the established cache key for tests/callers, but construct the
        # QWidget-capable application required by embedded element navigation.
        "QGuiApplication": QApplication,
        "QIcon": QIcon,
        "QQmlApplicationEngine": QQmlApplicationEngine,
        "QQuickStyle": QQuickStyle,
        "QUrl": QUrl,
        "Qt": Qt,
        "qmlRegisterSingletonInstance": qmlRegisterSingletonInstance,
        "ButtonMappingModel": classes["ButtonMappingModel"],
        "SettingsController": classes["SettingsController"],
        "DiagnosticsController": classes["DiagnosticsController"],
    }
    return _qt_classes_cache

# QML module/type names for the two QML singletons below - never
# "controller"/"model" (see run_settings_window()'s registration call for
# why).
_QML_MODULE_URI = "OvbSettings"
_QML_CONTROLLER_TYPE_NAME = "SettingsController"
_QML_MAPPING_MODEL_TYPE_NAME = "ButtonMappingModel"
_QML_DIAGNOSTICS_TYPE_NAME = "DiagnosticsController"


def run_settings_window(
    *, start_hidden: bool = False, start_bridge: bool = False
) -> int:
    """Builds and runs the Qt Quick/QML settings window. Blocks until the
    window is closed (``QGuiApplication.exec()``), then returns its exit
    code. Raises ``QtUnavailableError`` (via ``_load_qt_classes()``) if
    PySide6-Essentials is not installed, or if ``main.qml`` fails to load at
    all (e.g. a corrupted/incomplete frozen build missing its bundled qml/
    directory) - never silently opens a blank/broken window.
    """

    classes = _load_qt_classes()
    QGuiApplication = classes["QGuiApplication"]
    QIcon = classes["QIcon"]
    QQmlApplicationEngine = classes["QQmlApplicationEngine"]
    QQuickStyle = classes["QQuickStyle"]
    QUrl = classes["QUrl"]
    qmlRegisterSingletonInstance = classes["qmlRegisterSingletonInstance"]
    ButtonMappingModel = classes["ButtonMappingModel"]
    SettingsController = classes["SettingsController"]
    DiagnosticsController = classes["DiagnosticsController"]

    # Windows 11 Fluent look: QQuickStyle
    # must be set before the QGuiApplication/engine is constructed. Qt 6.7+
    # ships "FluentWinUI3" specifically to emulate the Windows 11 Fluent
    # design language for Qt Quick Controls; it renders (as a software
    # fallback, not native WinUI3) on non-Windows hosts too, which is what
    # this candidate's offscreen render/screenshot step below relies on.
    QQuickStyle.setStyle("FluentWinUI3")

    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    _apply_application_identity(app)
    set_quit_on_last_window_closed = getattr(
        app, "setQuitOnLastWindowClosed", None
    )
    if callable(set_quit_on_last_window_closed):
        set_quit_on_last_window_closed(False)

    model = ButtonMappingModel()
    controller = SettingsController(
        model,
        start_hidden=start_hidden,
        start_bridge=start_bridge,
    )
    _connect_application_exit(app, controller)

    root_window = None

    def update_application_icon() -> None:
        path = resources.find_app_icon(controller._tray_icon_state())
        if path is not None:
            _apply_application_icon(app, root_window, QIcon, path)

    update_application_icon()
    controller.trayStateChanged.connect(update_application_icon)
    diagnostics_controller = DiagnosticsController(
        controller,
        config.config_root(),
        auto_refresh=False,
    )

    # SettingsController has already started its endpoint/program status
    # workers here. Diagnostics are intentionally deferred until QML reports
    # its first frame, but may also exist before app.exec() returns. Every exit
    # path from this point therefore shares the same cleanup chain, including
    # engine.load() failures and an empty rootObjects() result.
    try:
        # Exposed to QML as SINGLETONS (resolved through the type/import
        # system at document-compile time), not as engine.rootContext()
        # context properties: a root-context property is resolved
        # dynamically through each QML object's context chain, and -
        # empirically, reproduced with a minimal isolated repro during this
        # task - a context property can observe a transient/incorrectly-null
        # value the first time it is read from a binding evaluated during a
        # child component's own construction (e.g. a ListView's
        # currentIndex binding, or any property evaluated inside a
        # ScrollView's deferred content), before every containing component
        # has finished having its own externally-supplied properties
        # assigned. A qmlRegisterSingletonInstance()-registered type has no
        # such hazard: every file that `import`s this module gets the exact
        # same already-fully-constructed instance immediately, with no
        # per-context propagation/ordering involved at all.
        qmlRegisterSingletonInstance(
            SettingsController,
            _QML_MODULE_URI,
            1,
            0,
            _QML_CONTROLLER_TYPE_NAME,
            controller,
        )
        qmlRegisterSingletonInstance(
            ButtonMappingModel,
            _QML_MODULE_URI,
            1,
            0,
            _QML_MAPPING_MODEL_TYPE_NAME,
            model,
        )
        qmlRegisterSingletonInstance(
            DiagnosticsController,
            _QML_MODULE_URI,
            1,
            0,
            _QML_DIAGNOSTICS_TYPE_NAME,
            diagnostics_controller,
        )

        engine = QQmlApplicationEngine()
        qml_dir = _qml_directory()
        engine.addImportPath(str(qml_dir))

        main_qml = qml_dir / "main.qml"
        engine.load(QUrl.fromLocalFile(str(main_qml)))
        root_objects = engine.rootObjects()
        if not root_objects:
            raise QtUnavailableError(f"无法加载 QML 设置界面：{main_qml} 未能成功加载。")
        root_window = root_objects[0]
        update_application_icon()
        window_chrome_windows.apply_settings_window_chrome(root_window)

        # The real QML window and full-exit signal are ready here. Publish the
        # capability before optional element navigation startup, whose bounded
        # native hook waits must not make another version misclassify this copy.
        settings_window_hwnd = _settings_window_handle(root_window)
        controller._settings_window_hwnd = settings_window_hwnd
        single_instance.register_settings_window_restore_event(settings_window_hwnd)
        _mark_settings_window_for_activation(
            root_window,
            hwnd=settings_window_hwnd,
        )

        if (
            sys.platform == "win32"
            and os.environ.get("OVB_DISABLE_LIVE_INPUT") != "1"
        ):
            try:
                navigation_runtime = (
                    element_navigation_runtime.start_embedded_element_navigation(
                        app
                    )
                )
            except Exception as exc:
                logging_setup.get_logger(config.config_root()).warning(
                    "element navigation unavailable in main process: %s",
                    type(exc).__name__,
                )
            else:
                element_navigation_control_windows.bind_embedded_element_navigation(
                    navigation_runtime
                )

        return app.exec()
    finally:
        try:
            single_instance.release_settings_window_restore_event(
                controller._settings_window_hwnd
            )
        except Exception:
            # Notification cleanup must not skip input/audio shutdown.
            pass
        try:
            provider_settled = controller.shutdownForProcessExit()
            if not provider_settled:
                logging_setup.get_logger(config.config_root()).error(
                    "voice hotkey provider transaction did not settle before desktop shutdown"
                )
        finally:
            try:
                bridge_stopped = bridge_launcher.stop_in_process_bridge(reason="process_exit")
                if bridge_stopped is False:
                    logging_setup.get_logger(config.config_root()).error(
                        "in-process bridge did not stop before desktop shutdown"
                    )
            finally:
                try:
                    element_navigation_control_windows.shutdown_element_navigation()
                finally:
                    try:
                        # called HERE, synchronously - whether
                        # app.exec() returned normally, engine.load() raised,
                        # rootObjects() was empty, input cleanup raised, or
                        # anything else in this block raised.
                        _shutdown_diagnostics_workers()
                    finally:
                        audio_playback.cleanup_retained_portaudio_test_resources(
                            blocking=False
                        )
