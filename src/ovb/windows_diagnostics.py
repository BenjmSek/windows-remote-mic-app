"""Qt-free "检查与修复" (check-and-repair) diagnostics .

Every check below is a plain function returning a typed, stable
``CheckResult`` - no Tk/Qt import anywhere in this module, matching this
package's existing convention (``settings_ui.py``, ``remote_layout.py``,
``shell_targets.py``) of keeping business logic directly unit-testable
without constructing a window. ``qt_settings_app.py`` is the only place that
runs ``run_diagnostics()`` off the Qt GUI thread and renders its results.

Every check that touches a real OS/WinRT/PortAudio call accepts an
injectable override (a ``probe``/``discover``/``enumerate_*`` callable) so
its PASS/FAIL/MANUAL branching is exercised directly in tests on any OS,
while the real, uninjected default degrades to ``UNSUPPORTED`` off Windows
or when an optional dependency is missing - it never fabricates a result it
cannot actually observe. The core honesty contract: a live
process or a paired device must never be reported as proof that buttons or
speech are working).

Raw Input and BLE candidate checks deliberately report only a COUNT/status,
never a device path or Bluetooth name/address - this module must not become
a second place that leaks the identifiers ``config.py``/``logging_setup.py``
already refuse to persist.

Every cross-process helper below runs its native work in a separate,
disposable OS process, not merely a background thread of this one - see the
section below for why an in-process asyncio cancellation cannot give a real
hard bound against the locked pywinrt/PortAudio wrapper's own unbounded
post-cancel wait, and how process-level termination does. Discovery, the
user-triggered VB-CABLE loopback and the output-endpoint preflight all
cross that same boundary.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Sequence, Tuple

from . import audio_output
from . import audio_playback
from . import device_catalog
from . import raw_input_windows
from . import single_instance

_REMOTE_DISPLAY_NAME = device_catalog.DEFAULT_DISPLAY_NAME

# Windows 10 version 1809's build number - the lowest build this project's
# own README/installer already claim as the supported floor (see
# installer/RemoteMicRemoteMicSetup.iss's ``MinVersion=10.0.17763``).
MIN_SUPPORTED_BUILD = 17763


class CheckStatus(Enum):
    PASS = "pass"
    FAIL = "fail"
    MANUAL = "manual"  # 待手动验证：Windows exposes no reliable automatic verdict
    UNSUPPORTED = "unsupported"  # not Windows, or a required dependency is missing


class CheckGroup(Enum):
    """Keep independent readiness areas from being collapsed together.

    - never collapsed into one another, since a passing optional-driver
    check must never look like proof that the voice bridge works.
    """

    ORDINARY_BUTTONS = "ordinary_buttons"
    VOICE_BRIDGE = "voice_bridge"
    OPTIONAL_DRIVER = "optional_driver"


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    title: str
    group: CheckGroup
    status: CheckStatus
    detail: str
    result_code: str = ""


@dataclass(frozen=True)
class DiagnosticsReport:
    checks: Tuple[CheckResult, ...]

    def get(self, check_id: str) -> Optional[CheckResult]:
        for check in self.checks:
            if check.check_id == check_id:
                return check
        return None


# -- OS version/architecture ------------------------------------------------


@dataclass(frozen=True)
class WindowsVersionInfo:
    major: int
    minor: int
    build: int
    is_64bit: bool


def _probe_windows_version() -> Optional[WindowsVersionInfo]:
    """Real probe: None off Windows. Never raises."""

    if sys.platform != "win32":
        return None
    version_info = sys.getwindowsversion()  # type: ignore[attr-defined]
    is_64bit = platform.machine().lower() in ("amd64", "x86_64", "arm64")
    return WindowsVersionInfo(
        major=version_info.major,
        minor=version_info.minor,
        build=version_info.build,
        is_64bit=is_64bit,
    )


def check_os_version(
    *, probe: Callable[[], Optional[WindowsVersionInfo]] = _probe_windows_version
) -> CheckResult:
    version_info = probe()
    if version_info is None:
        return CheckResult(
            "os_version",
            "Windows 版本与 64 位架构",
            CheckGroup.ORDINARY_BUTTONS,
            CheckStatus.UNSUPPORTED,
            "仅支持 Windows，无法检查版本与位数",
        )
    if not version_info.is_64bit:
        return CheckResult(
            "os_version",
            "Windows 版本与 64 位架构",
            CheckGroup.ORDINARY_BUTTONS,
            CheckStatus.FAIL,
            "需要 64 位 Windows 10 1809（17763）或更高版本",
        )
    if version_info.build < MIN_SUPPORTED_BUILD:
        return CheckResult(
            "os_version",
            "Windows 版本与 64 位架构",
            CheckGroup.ORDINARY_BUTTONS,
            CheckStatus.FAIL,
            f"Windows build {version_info.build}，最低要求 {MIN_SUPPORTED_BUILD}",
        )
    return CheckResult(
        "os_version",
        "Windows 版本与 64 位架构",
        CheckGroup.ORDINARY_BUTTONS,
        CheckStatus.PASS,
        f"Windows {version_info.build}（64 位）",
    )


# -- Raw Input (ordinary buttons) -------------------------------------------


def check_raw_input(
    *,
    enumerate_paths: Callable[
        [], Sequence[str]
    ] = raw_input_windows.enumerate_matching_device_paths,
    selected_key: str | None = None,
) -> CheckResult:
    try:
        paths = enumerate_paths()
        if selected_key is not None:
            from . import remote_selection, hid_identity
            try:
                paths = [remote_selection.selected_raw_path(list(paths), selected_key)]
            except hid_identity.HidIdentityError:
                paths = []
    except raw_input_windows.RawInputUnavailableError:
        if sys.platform != "win32":
            return CheckResult(
                "raw_input",
                "Raw Input 按键设备",
                CheckGroup.ORDINARY_BUTTONS,
                CheckStatus.UNSUPPORTED,
                "仅 Windows 可检测 Raw Input 按键设备",
                result_code="unsupported_platform",
            )
        # This used to interpolate str(exc)
        # here. RawInputUnavailableError's real production message is
        # API-only today, but nothing stops a future/injected raise from
        # carrying a real Raw Input device path in its message - and this
        # module's own contract (see module docstring) is that Raw Input
        # output never surfaces an identifier, not "never surfaces one
        # unless the exception happens to be safe". Generic and actionable
        # instead, with no exception text at all.
        return CheckResult(
            "raw_input",
            "Raw Input 按键设备",
            CheckGroup.ORDINARY_BUTTONS,
            CheckStatus.FAIL,
            "Raw Input 检测失败；检查遥控器连接后重新检测",
            result_code="probe_failed",
        )

    count = len(paths)
    # Never report the path itself - count/status only.
    if count == 0:
        return CheckResult(
            "raw_input",
            "Raw Input 按键设备",
            CheckGroup.ORDINARY_BUTTONS,
            CheckStatus.FAIL,
            f"未找到{_REMOTE_DISPLAY_NAME}的按键设备；请先完成蓝牙配对",
            result_code="no_device",
        )
    if count > 1:
        return CheckResult(
            "raw_input",
            "Raw Input 按键设备",
            CheckGroup.ORDINARY_BUTTONS,
            CheckStatus.FAIL,
            f"找到 {count} 个匹配设备；请只保留 1 个已连接设备",
            result_code="ambiguous",
        )
    return CheckResult(
        "raw_input",
        "Raw Input 按键设备",
        CheckGroup.ORDINARY_BUTTONS,
        CheckStatus.PASS,
        f"{_REMOTE_DISPLAY_NAME} 按键设备已找到",
        result_code="ready",
    )


# -- BLE candidate (voice bridge) -------------------------------------------
#
# the diagnostics-only BLE enumeration below runs in a
# genuinely separate OS PROCESS, not merely a cancellable asyncio Task in
# this process. tried the latter (cancel the asyncio Task
# wrapping the WinRT device enumeration, relying on the locked pywinrt >=3.2
# asyncio-cancellation-propagates-to-WinRT documented behaviour) and found
# the real, locked winrt-runtime==3.2.1
# ``winrt/runtime/_internals.py::wrap_async()`` source:
#
#     except asyncio.CancelledError:
#         op.cancel()
#         await event.wait()   # <-- UNBOUNDED: waits for the native
#         raise                #     completion callback with no timeout
#
# ``IAsyncInfo.cancel()`` only REQUESTS cancellation (Microsoft's own ABI
# docs give no completion-time guarantee) - the Python-level ``await task``
# after calling ``task.cancel()`` can therefore still hang indefinitely
# waiting for a native completion callback that may never fire, which is
# exactly the real Windows CI 0xC0000005 crash this task chases: a
# same-process background thread still executing native WinRT code when the
# interpreter starts finalizing. No in-process asyncio timeout can bound an
# operation whose own cancellation primitive is itself unbounded.
#
# The only way to get a REAL, OS-enforced hard bound is process isolation:
# ``_run_ble_diagnostics_subprocess()`` spawns a disposable child process
# (this same interpreter re-invoked with the caller's own hidden flag) that
# does the actual WinRT call; if it does not report back within ``BLE_DISCOVERY_TIMEOUT_SECONDS``,
# the parent terminates it and, if that alone is not confirmed within
# ``_SUBPROCESS_TERMINATE_WAIT_SECONDS``, escalates to a forceful kill and
# waits again - each step individually bounded, and each step's SUCCESS is
# verified via the child's confirmed exit (``Popen.wait()`` returning), not
# merely "we called terminate()". A forcefully-killed OS process cannot
# leave any native code running that could touch THIS interpreter's memory
# during ITS OWN finalization - the crash's precondition (a still-running
# native WinRT call sharing this process/interpreter) is structurally
# impossible once the enumeration lives in a separate process. Only the
# DIAGNOSTICS-ONLY device listing this module performs is isolated this way.
#
# IPC transport (correction - NOT stdout): a real
# PyInstaller ``console=False`` (windowed) build - which is exactly what
# ``build/RemoteMicChromecast.spec`` produces - has ``sys.stdout``,
# ``sys.stderr`` AND ``sys.stdin`` set to ``None`` by the bootloader,
# unconditionally, even when a parent process redirects the child's OS-level
# std handles (see PyInstaller's own documentation, "Common issues and
# pitfalls" -> "windowed/noconsole application" section:
# https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html). A
# ``print()``-to-stdout IPC contract - what this section originally shipped
# - would raise ``AttributeError`` inside the FROZEN child the very first
# time it tried to report a result, defeating the whole design silently (the
# parent would just see a nonzero exit / no output and report a generic
# "error", masking that the mechanism never worked in a frozen build at
# all). Every value that crosses the process boundary therefore goes
# through a RESULT FILE instead: the parent creates a private, per-attempt
# temp directory (``tempfile.mkdtemp()`` - restricted to the owning user by
# construction on every platform this runs on) and passes the file path
# inside it to the child as a plain argv value; the child (which never
# touches ``sys.stdout``/``sys.stderr``/``sys.stdin`` anywhere in this
# module) writes ONE small, strictly-allow-listed JSON object to that path
# ATOMICALLY (write to a sibling temp file, then ``os.replace()``), and the
# parent reads it back only after the child's exit has already been
# confirmed. The parent ALWAYS deletes the temp directory afterward (its own
# ``finally``), regardless of outcome.
#
# Content contract: ``{"verdict": "single_match" | "no_candidate" |
# "winrt_unavailable" | "error"}`` or ``{"verdict": "ambiguous", "count":
# N}`` (2 <= N <= ``_MAX_PLAUSIBLE_AMBIGUOUS_COUNT``) - and NOTHING else;
# the parent's parser (``_sanitize_verdict_payload()``) strictly allow-lists
# every key, type, and value and turns anything else into the same honest
# "error" a real in-child failure would have produced - it never trusts
# arbitrary file content enough to, say, allocate a candidate list sized by
# an attacker/corruption-controlled number. Never a device name, address,
# handle, or raw exception text - the name-matching decision runs INSIDE the
# child, where real device names are visible; only the verdict/count ever
# crosses the process boundary. The child's stdout/stderr/stdin are all DEVNULL'd by the parent
# (belt-and-suspenders on top of never being read from in this module), so
# even an unexpected traceback from third-party code can never reach this
# process.

VB_CABLE_LOOPBACK_SUBPROCESS_FLAG = "--diagnose-vb-cable-loopback"
OUTPUT_ENDPOINT_PREFLIGHT_SUBPROCESS_FLAG = "--preflight-output-endpoint"

# Absolute upper bound on how long the parent waits for the child to report
# a verdict ON ITS OWN before beginning forced termination. Generous enough
# for real BLE enumeration (a handful of seconds at most).
BLE_DISCOVERY_TIMEOUT_SECONDS = 10.0

# How often the parent re-checks cancel_event/the child's exit status while
# a discovery attempt is still in flight.
_SUBPROCESS_POLL_SECONDS = 0.1

# Individually-bounded wait after each escalating termination step
# (terminate() then, if still alive, kill()) - see
# _terminate_and_confirm_exit()'s docstring. On Windows both map to the same
# TerminateProcess() call (Python's subprocess module aliases kill() to
# terminate() there - there is no SIGKILL/SIGTERM distinction on Windows),
# so this escalation is mainly meaningful cross-platform (POSIX dev/test
# hosts, where a process CAN choose to ignore SIGTERM but never SIGKILL);
# on Windows the first wait already confirms exit in virtually all cases.
_SUBPROCESS_TERMINATE_WAIT_SECONDS = 2.0
_SUBPROCESS_KILL_WAIT_SECONDS = 2.0

# Public (not underscore-prefixed) on purpose: the worst-case wall-clock
# time _run_ble_diagnostics_subprocess() can spend AFTER deciding to cancel
# (poll-detection latency + both escalating termination waits) before it
# raises either BleDiscoveryCancelledError or
# BleDiscoverySubprocessShutdownUnconfirmedError. qt_settings_app.py's own
# worker-thread join timeout is DERIVED from this value (
# #2: the previous flat 2.0s join guess was independent of, and smaller
# than, this module's own termination bound - aligning them by construction
# means they can never silently drift apart again).
BLE_DISCOVERY_MAX_CANCELLATION_SECONDS = (
    _SUBPROCESS_POLL_SECONDS + _SUBPROCESS_TERMINATE_WAIT_SECONDS + _SUBPROCESS_KILL_WAIT_SECONDS
)

# The child includes interpreter start-up, endpoint enumeration, PortAudio
# stream construction/start/stop/close, and signal analysis. The in-child
# stream loop has its own shorter deadline, while this parent-side deadline
# is the real hard bound if any native PortAudio call never returns.
VB_CABLE_LOOPBACK_PROCESS_TIMEOUT_SECONDS = 8.0
VB_CABLE_LOOPBACK_MAX_CANCELLATION_SECONDS = (
    _SUBPROCESS_POLL_SECONDS + _SUBPROCESS_TERMINATE_WAIT_SECONDS + _SUBPROCESS_KILL_WAIT_SECONDS
)
OUTPUT_ENDPOINT_PREFLIGHT_PROCESS_TIMEOUT_SECONDS = 8.0
OUTPUT_ENDPOINT_PREFLIGHT_MAX_CANCELLATION_SECONDS = (
    _SUBPROCESS_POLL_SECONDS + _SUBPROCESS_TERMINATE_WAIT_SECONDS + _SUBPROCESS_KILL_WAIT_SECONDS
)

# Defensive ceiling on a reported ambiguous count (/D):
# this module never trusts an unbounded or implausible number from a result
# file it did not fully control the writer of - a real Chromecast candidate list
# can never plausibly reach even double digits, and this also stops an
# ambiguous count from ever being turned into an unbounded placeholder
# candidate list.
_MAX_PLAUSIBLE_AMBIGUOUS_COUNT = 64


class BleDiscoveryCancelledError(Exception):
    """Raised when the diagnostics subprocess did not report a verdict within
    ``BLE_DISCOVERY_TIMEOUT_SECONDS`` (or ``cancel_event`` was set first) and
    parent CONFIRMED the child process actually exited before raising this
    (see ``_terminate_and_confirm_exit()``). Carries no device identifier -
    matches this module's never-leak-an-identifier contract (see module
    docstring).
    """


class BleDiscoverySubprocessShutdownUnconfirmedError(Exception):
    """Raised in the (expected to be extremely rare) case where even a
    forceful kill could not be confirmed to have actually stopped the
    diagnostics subprocess within its own bounded wait. This must never be silently treated as equivalent to a
    normal cancellation/timeout - the caller could not verify the isolation
    boundary this whole design exists for actually held, and must say so
    honestly rather than claim a safe outcome it cannot prove.
    """


class VbCableLoopbackCancelledError(Exception):
    """The isolated active-audio test was cancelled or exceeded its bound."""


class VbCableLoopbackSubprocessShutdownUnconfirmedError(Exception):
    """The active-audio child could not be confirmed to have stopped."""


class OutputEndpointPreflightCancelledError(Exception):
    """The isolated output-endpoint preflight was cancelled or timed out."""


class OutputEndpointPreflightShutdownUnconfirmedError(Exception):
    """The endpoint-preflight child could not be confirmed to have stopped."""


class BleDiagnosticsVerdict(Enum):
    """The complete, closed set of outcomes the diagnostics subprocess can
    ever legitimately report - see this section's module-level comment for
    the full IPC contract. Any raw file content that does not map cleanly
    onto exactly one of these becomes ``ERROR`` (see
    ``_sanitize_verdict_payload()``), never a fabricated success.
    """

    SINGLE_MATCH = "single_match"
    NO_CANDIDATE = "no_candidate"
    AMBIGUOUS = "ambiguous"
    WINRT_UNAVAILABLE = "winrt_unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class BleDiagnosticsResult:
    """The parent's fully-validated, in-memory representation of what the
    diagnostics subprocess reported - never constructed directly from raw
    file content anywhere except ``_sanitize_verdict_payload()``.
    """

    verdict: BleDiagnosticsVerdict
    count: Optional[int] = None


_ERROR_RESULT = BleDiagnosticsResult(verdict=BleDiagnosticsVerdict.ERROR)




def build_vb_cable_loopback_subprocess_command(
    request_path: str,
    result_path: str,
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> List[str]:
    """Build the hidden child command for the active VB-CABLE test."""

    is_frozen = frozen if frozen is not None else bool(getattr(sys, "frozen", False))
    exe = executable if executable is not None else sys.executable
    if is_frozen:
        return [
            exe,
            VB_CABLE_LOOPBACK_SUBPROCESS_FLAG,
            request_path,
            result_path,
        ]
    return [
        exe,
        "-m",
        "ovb",
        VB_CABLE_LOOPBACK_SUBPROCESS_FLAG,
        request_path,
        result_path,
    ]


def build_output_endpoint_preflight_subprocess_command(
    request_path: str,
    result_path: str,
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
) -> List[str]:
    """Build the hidden child command for one endpoint-open preflight."""

    is_frozen = frozen if frozen is not None else bool(getattr(sys, "frozen", False))
    exe = executable if executable is not None else sys.executable
    if is_frozen:
        return [
            exe,
            OUTPUT_ENDPOINT_PREFLIGHT_SUBPROCESS_FLAG,
            request_path,
            result_path,
        ]
    return [
        exe,
        "-m",
        "ovb",
        OUTPUT_ENDPOINT_PREFLIGHT_SUBPROCESS_FLAG,
        request_path,
        result_path,
    ]


def _write_verdict_atomically(result_path: str, payload: dict) -> None:
    """Writes ``payload`` to ``result_path`` atomically: write to a sibling
    temp file in the SAME directory, then ``os.replace()`` it into place.
    The parent only ever reads ``result_path`` after confirming this
    process's exit, but this still guarantees it can never observe a
    partially-written file even under an unlucky signal/kill timing.
    """

    directory = os.path.dirname(result_path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".ble-verdict-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_path, result_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        raise




def _read_vb_cable_loopback_request(
    request_path: Optional[str],
) -> Optional[Tuple[str, str]]:
    if not request_path:
        return None
    try:
        with open(request_path, "r", encoding="utf-8") as handle:
            text = handle.read(4097)
    except (FileNotFoundError, OSError):
        return None
    if not text.strip() or len(text) > 4096:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or set(parsed) != {
        "saved_output_name",
        "saved_output_host_api",
    }:
        return None
    name = parsed.get("saved_output_name")
    host_api = parsed.get("saved_output_host_api")
    if not isinstance(name, str) or not isinstance(host_api, str):
        return None
    if len(name) > 512 or len(host_api) > 128:
        return None
    return name, host_api


def run_vb_cable_loopback_subprocess_entrypoint(
    request_path: Optional[str], result_path: Optional[str]
) -> int:
    """Run the complete active PortAudio check inside a disposable child."""

    request = _read_vb_cable_loopback_request(request_path)
    if request is None or not result_path:
        return 1

    try:
        result = check_vb_cable_loopback(*request)
    except Exception:  # noqa: BLE001 - sanitize at the process boundary
        result = CheckResult(
            "vb_cable_loopback",
            "VB-CABLE 本地通道",
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "VB-CABLE 通道测试出现意外错误，未得到可信结果。",
        )

    payload = {
        "status": result.status.value,
        "detail": result.detail,
    }
    try:
        _write_verdict_atomically(result_path, payload)
    except OSError:
        return 1
    return 0


def run_output_endpoint_preflight_subprocess_entrypoint(
    request_path: Optional[str], result_path: Optional[str]
) -> int:
    """Open/start/stop/close one endpoint inside a disposable child."""

    request = _read_vb_cable_loopback_request(request_path)
    if request is None or not result_path:
        return 1

    try:
        audio_playback.preflight_output_endpoint(*request)
    except Exception:  # noqa: BLE001 - sanitize at the process boundary
        payload = {"verdict": "unavailable"}
    else:
        payload = {"verdict": "ok"}
    try:
        _write_verdict_atomically(result_path, payload)
    except OSError:
        return 1
    return 0


def _popen_kwargs() -> dict:
    kwargs: dict = dict(
        # No IPC ever crosses stdout/stderr/stdin (see this section's
        # module-level "IPC transport" comment) - DEVNULL'd for all three
        # so nothing produced by this process or anything it transitively
        # runs can ever reach this one, not merely "produced but never
        # read".
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    if sys.platform == "win32":
        # Prevents a console window flash from a real console-less
        # (console=False) frozen .exe re-invoking itself, or from `python`
        # in a source checkout - CREATE_NO_WINDOW only exists on Windows.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return kwargs


def _attempt_termination_step(
    proc: "subprocess.Popen", signal_fn: Callable[[], None], wait_timeout: float
) -> bool:
    """Runs ONE escalating termination attempt (``signal_fn`` is
    ``proc.terminate`` or ``proc.kill``) and returns True ONLY if the
    child's exit is CONFIRMED afterward via a real ``wait()``/``poll()``
    observation - never merely because ``signal_fn()`` didn't raise.

    (Codex red evidence): ``proc.terminate()``/
    ``proc.kill()``/``proc.wait()`` can each raise an ``OSError``
    (``PermissionError`` was the exact case reproduced) that is neither a
    confirmed exit nor a ``TimeoutExpired`` - the previous version let such
    an exception escape this function as a raw, undifferentiated
    ``OSError``, which meant ``_run_ble_diagnostics_subprocess()`` could
    never reach its own honest ``BleDiscoverySubprocessShutdownUnconfirmedError``
    contract for exactly the case that contract exists for: "this process
    could not confirm the diagnostics subprocess actually stopped".

    Every process-control exception here is therefore treated
    conservatively as "not yet confirmed", never as "must have failed" or
    "must have succeeded": ``signal_fn()`` raising is swallowed (the
    process may already have exited in a race - ``ProcessLookupError`` on
    POSIX - or this process may simply lack permission to signal it either
    way, only a real ``wait()``/``poll()`` observation ever counts), and
    ``wait()`` raising anything other than ``TimeoutExpired`` falls back to
    one single, non-blocking ``poll()`` check (never a second unbounded
    wait) so a process that happens to have already exited despite
    ``wait()`` itself failing is still correctly recognized as confirmed.
    ``poll()`` itself is ALSO not trusted to never raise (it makes the same
    kind of OS process-query call as ``wait()``): if it does, that is
    ANOTHER "cannot confirm" outcome, not a crash - this function returns
    False rather than letting a second raw ``OSError`` escape. Still
    individually bounded by ``wait_timeout`` either way.
    """

    try:
        signal_fn()
    except OSError:
        pass

    try:
        proc.wait(timeout=wait_timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        try:
            return proc.poll() is not None
        except OSError:
            return False


def _terminate_and_confirm_exit(
    proc: "subprocess.Popen",
    *,
    terminate_wait: float,
    kill_wait: float,
) -> bool:
    """Escalating, individually-bounded hard termination. Returns True ONLY
    if the child's exit was actually CONFIRMED (see
    ``_attempt_termination_step()``) - never merely because ``terminate()``
    /``kill()`` was called, which only REQUESTS termination (
    1 P1: the same "requested, not confirmed" gap that made the previous
    asyncio-cancellation approach insufficient applies just as much to a
    single unconfirmed ``terminate()`` call here - that is why this function
    always confirms via ``.wait()``/``.poll()`` afterward and reports
    failure honestly if even ``kill()`` could not be confirmed).
    """

    if _attempt_termination_step(proc, proc.terminate, terminate_wait):
        return True
    return _attempt_termination_step(proc, proc.kill, kill_wait)


def _sanitize_verdict_payload(parsed: object) -> BleDiagnosticsResult:
    """Strictly allow-lists ``parsed`` (already-``json.loads()``-decoded
    result-file content) into a ``BleDiagnosticsResult`` (
    P2/D). ANY deviation from the exact expected shape - an unknown verdict
    string, an unexpected/missing/extra key, a non-integer or out-of-range
    ``count``, ``count`` present when it should not be (or vice versa) -
    becomes ``ERROR``. Never raises; this is the single place raw,
    untrusted file content is allowed to influence this process's behaviour
    at all, and it is deliberately conservative rather than lenient.
    """

    if not isinstance(parsed, dict):
        return _ERROR_RESULT

    verdict_value = parsed.get("verdict")
    if not isinstance(verdict_value, str):
        return _ERROR_RESULT
    try:
        verdict = BleDiagnosticsVerdict(verdict_value)
    except ValueError:
        return _ERROR_RESULT

    keys = set(parsed.keys())
    if verdict is BleDiagnosticsVerdict.AMBIGUOUS:
        if keys != {"verdict", "count"}:
            return _ERROR_RESULT
        count = parsed.get("count")
        # bool is an int subclass in Python - True/False must never be
        # accepted as a candidate count.
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 2
            or count > _MAX_PLAUSIBLE_AMBIGUOUS_COUNT
        ):
            return _ERROR_RESULT
        return BleDiagnosticsResult(verdict=verdict, count=count)

    if keys != {"verdict"}:
        return _ERROR_RESULT
    return BleDiagnosticsResult(verdict=verdict)


def _read_subprocess_verdict(result_path: str, returncode: Optional[int]) -> BleDiagnosticsResult:
    """Reads and strictly validates the child's result file - ONLY called
    after the child's exit has already been confirmed (see
    ``_run_ble_diagnostics_subprocess()``). Never raises: a nonzero exit, a
    missing file, unreadable/undecodable content, or content that fails
    ``_sanitize_verdict_payload()``'s allow-list all become the same honest
    ``ERROR`` result a real in-child failure would have produced.
    """

    if returncode != 0:
        return _ERROR_RESULT
    try:
        with open(result_path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except (FileNotFoundError, OSError):
        return _ERROR_RESULT
    text = text.strip()
    if not text:
        return _ERROR_RESULT
    try:
        parsed = json.loads(text)
    except ValueError:
        return _ERROR_RESULT
    return _sanitize_verdict_payload(parsed)




def _run_ble_diagnostics_subprocess(
    command: Sequence[str],
    *,
    result_path: str,
    cancel_event: threading.Event,
    timeout: float,
    poll_interval: float = _SUBPROCESS_POLL_SECONDS,
    terminate_wait: float = _SUBPROCESS_TERMINATE_WAIT_SECONDS,
    kill_wait: float = _SUBPROCESS_KILL_WAIT_SECONDS,
    popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
    result_reader: Optional[Callable[[str, int], object]] = None,
) -> object:
    """Spawns ``command`` (the caller owns how that command is built) and
    returns its validated verdict once it has exited ON ITS OWN within
    ``timeout`` (and ``cancel_event`` was never set) - never reading
    ``result_path`` before that. If ``cancel_event`` is set or ``timeout``
    elapses first, forcibly terminates it (see
    ``_terminate_and_confirm_exit()``) and either raises
    ``BleDiscoveryCancelledError`` (termination CONFIRMED) or
    ``BleDiscoverySubprocessShutdownUnconfirmedError`` (termination could
    not be confirmed even after escalating to a forceful kill) - never
    silently returns as if discovery had actually completed, and never
    reads ``result_path`` in either case (a killed child's partially-written
    file, if any, is simply never looked at).

    Owns spawning and waiting only - NOT the temp directory ``result_path``
    lives in, which the caller creates and always cleans up regardless of
    outcome. This function therefore holds no cleanup-worthy resource of its
    own (no stdout pipe is ever opened - see ``_popen_kwargs()``) and needs
    no ``finally`` block.

    A short-circuit before ever spawning: if ``cancel_event`` is already set
    (e.g. the settings window began closing between this worker starting
    and reaching the BLE check), raises ``BleDiscoveryCancelledError``
    immediately rather than paying for a spawn only to kill it right away.
    """

    if cancel_event.is_set():
        raise BleDiscoveryCancelledError("BLE discovery cancelled before it could start")

    proc = popen(list(command), **_popen_kwargs())

    deadline = time.monotonic() + timeout
    returncode = None
    while True:
        try:
            returncode = proc.poll()
        except OSError:
            confirmed_dead = _terminate_and_confirm_exit(
                proc, terminate_wait=terminate_wait, kill_wait=kill_wait
            )
            if not confirmed_dead:
                raise BleDiscoverySubprocessShutdownUnconfirmedError(
                    "BLE diagnostics subprocess status failed and exit could not be confirmed"
                )
            raise RuntimeError(
                "BLE diagnostics subprocess status failed; child was terminated"
            )
        if returncode is not None:
            break
        if cancel_event.is_set():
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)

    if returncode is None:
        confirmed_dead = _terminate_and_confirm_exit(
            proc, terminate_wait=terminate_wait, kill_wait=kill_wait
        )
        if not confirmed_dead:
            raise BleDiscoverySubprocessShutdownUnconfirmedError(
                "BLE diagnostics subprocess could not be confirmed to have exited"
            )
        raise BleDiscoveryCancelledError("BLE discovery cancelled or timed out")

    # The child has been CONFIRMED to have exited on its own - only now is
    # its result file ever read.
    if result_reader is not None:
        return result_reader(result_path, returncode)
    return _read_subprocess_verdict(result_path, returncode)


def _sanitize_vb_cable_loopback_result(parsed: object) -> Optional[CheckResult]:
    if not isinstance(parsed, dict) or set(parsed) != {"status", "detail"}:
        return None
    status_value = parsed.get("status")
    detail = parsed.get("detail")
    if status_value not in {
        CheckStatus.PASS.value,
        CheckStatus.FAIL.value,
        CheckStatus.UNSUPPORTED.value,
    }:
        return None
    if (
        not isinstance(detail, str)
        or not detail.strip()
        or len(detail) > 1024
        or "\x00" in detail
    ):
        return None
    return CheckResult(
        "vb_cable_loopback",
        "VB-CABLE 本地通道",
        CheckGroup.OPTIONAL_DRIVER,
        CheckStatus(status_value),
        detail,
    )


def _read_vb_cable_loopback_result(
    result_path: str, returncode: Optional[int]
) -> Optional[CheckResult]:
    if returncode != 0:
        return None
    try:
        with open(result_path, "r", encoding="utf-8") as handle:
            text = handle.read(4097)
    except (FileNotFoundError, OSError):
        return None
    if not text.strip() or len(text) > 4096:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return _sanitize_vb_cable_loopback_result(parsed)


def _run_vb_cable_loopback_subprocess(
    command: Sequence[str],
    *,
    result_path: str,
    cancel_event: threading.Event,
    timeout: float,
    poll_interval: float = _SUBPROCESS_POLL_SECONDS,
    terminate_wait: float = _SUBPROCESS_TERMINATE_WAIT_SECONDS,
    kill_wait: float = _SUBPROCESS_KILL_WAIT_SECONDS,
    popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
) -> Optional[CheckResult]:
    """Run the PortAudio child with a process-enforced time bound."""

    if cancel_event.is_set():
        raise VbCableLoopbackCancelledError(
            "VB-CABLE loopback cancelled before it could start"
        )

    proc = popen(list(command), **_popen_kwargs())
    deadline = time.monotonic() + timeout
    returncode = None
    while True:
        try:
            returncode = proc.poll()
        except OSError:
            confirmed_dead = _terminate_and_confirm_exit(
                proc, terminate_wait=terminate_wait, kill_wait=kill_wait
            )
            if not confirmed_dead:
                raise VbCableLoopbackSubprocessShutdownUnconfirmedError(
                    "VB-CABLE loopback child status failed and exit was unconfirmed"
                )
            raise RuntimeError(
                "VB-CABLE loopback child status failed; child was terminated"
            )
        if returncode is not None:
            break
        if cancel_event.is_set() or time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)

    if returncode is None:
        confirmed_dead = _terminate_and_confirm_exit(
            proc, terminate_wait=terminate_wait, kill_wait=kill_wait
        )
        if not confirmed_dead:
            raise VbCableLoopbackSubprocessShutdownUnconfirmedError(
                "VB-CABLE loopback child could not be confirmed to have exited"
            )
        raise VbCableLoopbackCancelledError(
            "VB-CABLE loopback cancelled or timed out"
        )

    return _read_vb_cable_loopback_result(result_path, returncode)


def _read_output_endpoint_preflight_result(
    result_path: str, returncode: Optional[int]
) -> Optional[bool]:
    if returncode != 0:
        return None
    try:
        with open(result_path, "r", encoding="utf-8") as handle:
            text = handle.read(257)
    except (FileNotFoundError, OSError):
        return None
    if not text.strip() or len(text) > 256:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or set(parsed) != {"verdict"}:
        return None
    verdict = parsed.get("verdict")
    if verdict == "ok":
        return True
    if verdict == "unavailable":
        return False
    return None


def _run_output_endpoint_preflight_subprocess(
    command: Sequence[str],
    *,
    result_path: str,
    cancel_event: threading.Event,
    timeout: float,
    poll_interval: float = _SUBPROCESS_POLL_SECONDS,
    terminate_wait: float = _SUBPROCESS_TERMINATE_WAIT_SECONDS,
    kill_wait: float = _SUBPROCESS_KILL_WAIT_SECONDS,
    popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
) -> Optional[bool]:
    """Run one endpoint-open preflight with a process-enforced bound."""

    if cancel_event.is_set():
        raise OutputEndpointPreflightCancelledError(
            "output endpoint preflight cancelled before it could start"
        )

    proc = popen(list(command), **_popen_kwargs())
    deadline = time.monotonic() + timeout
    returncode = None
    while True:
        try:
            returncode = proc.poll()
        except OSError:
            confirmed_dead = _terminate_and_confirm_exit(
                proc, terminate_wait=terminate_wait, kill_wait=kill_wait
            )
            if not confirmed_dead:
                raise OutputEndpointPreflightShutdownUnconfirmedError(
                    "output endpoint preflight status failed and exit was unconfirmed"
                )
            raise RuntimeError(
                "output endpoint preflight status failed; child was terminated"
            )
        if returncode is not None:
            break
        if cancel_event.is_set() or time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)

    if returncode is None:
        confirmed_dead = _terminate_and_confirm_exit(
            proc, terminate_wait=terminate_wait, kill_wait=kill_wait
        )
        if not confirmed_dead:
            raise OutputEndpointPreflightShutdownUnconfirmedError(
                "output endpoint preflight child could not be confirmed to have exited"
            )
        raise OutputEndpointPreflightCancelledError(
            "output endpoint preflight cancelled or timed out"
        )

    return _read_output_endpoint_preflight_result(result_path, returncode)


def _vb_cable_bridge_exclusion_guard():
    """Keep bridge startup out while the active audio child owns the route."""

    if sys.platform != "win32":
        return contextlib.nullcontext()
    return single_instance.BridgeInstanceGuard(
        _duplicate_message="the Chromecast bridge is already running"
    )






# -- VB-CABLE endpoints (optional driver) -----------------------------------


def check_vb_cable_endpoints(
    *,
    list_playback: Callable[
        [], Sequence[audio_output.AudioEndpoint]
    ] = audio_output.enumerate_output_endpoints,
    list_recording: Callable[
        [], Sequence[audio_output.AudioEndpoint]
    ] = audio_output.enumerate_input_endpoints,
) -> CheckResult:
    try:
        playback = list(list_playback())
        recording = list(list_recording())
    except audio_output.AudioOutputUnavailableError as exc:
        return CheckResult(
            "vb_cable_endpoints",
            "VB-CABLE 虚拟音频端点（可选）",
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.UNSUPPORTED,
            f"无法检测音频端点：{exc}",
        )

    has_input = any(
        audio_output.is_cable_input_endpoint(e.name)
        and audio_output.is_supported_output_host_api(e.host_api)
        for e in playback
    )
    has_output = any(audio_output.is_cable_output_endpoint(e.name) for e in recording)

    if has_input and has_output:
        return CheckResult(
            "vb_cable_endpoints",
            "VB-CABLE 虚拟音频端点（可选）",
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.PASS,
            "已找到 CABLE Input（播放）和 CABLE Output（录音）",
        )
    missing = []
    if not has_input:
        missing.append("CABLE Input（播放）")
    if not has_output:
        missing.append("CABLE Output（录音）")
    return CheckResult(
        "vb_cable_endpoints",
        "VB-CABLE 虚拟音频端点（可选）",
        CheckGroup.OPTIONAL_DRIVER,
        CheckStatus.FAIL,
        "缺少：" + "、".join(missing) + "；只影响系统麦克风语音，不影响按键",
    )


def check_vb_cable_loopback(
    saved_output_name: str,
    saved_output_host_api: str,
    *,
    list_playback: Callable[
        [], Sequence[audio_output.AudioEndpoint]
    ] = audio_output.enumerate_output_endpoints,
    list_recording: Callable[
        [], Sequence[audio_output.AudioEndpoint]
    ] = audio_output.enumerate_input_endpoints,
    probe: Callable[..., audio_playback.CableLoopbackProbeResult] = (
        audio_playback.probe_virtual_cable_loopback
    ),
    cancel_event: Optional[threading.Event] = None,
) -> CheckResult:
    """Explicit active check; deliberately not part of ``run_diagnostics``.

    It emits a short synthetic sweep into the currently selected CABLE Input
    and checks for the same waveform on the unique CABLE Output view under
    the same host API. It never changes Windows defaults or persists PCM.
    """

    title = "VB-CABLE 本地通道"
    try:
        playback = list(list_playback())
        recording = list(list_recording())
    except audio_output.AudioOutputUnavailableError:
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.UNSUPPORTED,
            "无法检测音频端点；未运行测试",
        )
    except Exception:  # noqa: BLE001 - never expose device details
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "音频端点检测失败；未运行测试",
        )

    try:
        output_endpoint = audio_output.resolve_selected_endpoint(
            playback, saved_output_name, saved_output_host_api
        )
    except audio_output.AudioOutputUnavailableError:
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "输出端点不可用；请重新选择并应用",
        )
    if not audio_output.is_cable_input_endpoint(output_endpoint.name):
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "输出端点不是 CABLE Input；请重新选择并应用",
        )

    input_matches = [
        endpoint
        for endpoint in recording
        if audio_output.is_cable_output_endpoint(endpoint.name)
        and endpoint.host_api == output_endpoint.host_api
    ]
    if not input_matches:
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "未找到同一音频接口的 CABLE Output",
        )
    if len(input_matches) != 1:
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "同一音频接口有多个 CABLE Output；无法确定测试端点",
        )

    try:
        result = probe(
            output_endpoint,
            input_matches[0],
            cancel_event=cancel_event,
        )
    except audio_playback.LoopbackProbeCancelledError:
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.UNSUPPORTED,
            "通道测试已取消",
        )
    except audio_playback.LoopbackProbeUnavailableError:
        status = CheckStatus.UNSUPPORTED if sys.platform != "win32" else CheckStatus.FAIL
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            status,
            "无法完成通道测试；请关闭占用端点的程序后重试",
        )
    except Exception:  # noqa: BLE001 - never expose device details
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "通道测试失败；未得到可信结果",
        )

    if result.input_overflowed or result.output_underflowed:
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "音频溢出或欠载，本次结果无效；请关闭占用端点的程序后重试",
        )
    if not result.detected:
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "CABLE Output 未收到测试信号；请检查端点后重试",
        )

    latency_ms = result.roundtrip_latency_ms or 0.0
    return CheckResult(
        "vb_cable_loopback",
        title,
        CheckGroup.OPTIONAL_DRIVER,
        CheckStatus.PASS,
        f"CABLE Input → CABLE Output 正常（约 {latency_ms:.0f} ms）；"
        "不代表语音识别已通过",
    )


def _run_vb_cable_loopback_in_tempdir(
    saved_output_name: str,
    saved_output_host_api: str,
    *,
    cancel_event: threading.Event,
    timeout: float,
    bridge_guard_factory: Callable[[], object] = _vb_cable_bridge_exclusion_guard,
) -> Optional[CheckResult]:
    with bridge_guard_factory():
        result_dir = tempfile.mkdtemp(prefix="ovb-google-loopback-diag-")
        request_path = os.path.join(result_dir, "request.json")
        result_path = os.path.join(result_dir, "result.json")
        try:
            _write_verdict_atomically(
                request_path,
                {
                    "saved_output_name": saved_output_name,
                    "saved_output_host_api": saved_output_host_api,
                },
            )
            command = build_vb_cable_loopback_subprocess_command(
                request_path, result_path
            )
            return _run_vb_cable_loopback_subprocess(
                command,
                result_path=result_path,
                cancel_event=cancel_event,
                timeout=timeout,
            )
        finally:
            original_exc = sys.exc_info()[1]
            try:
                shutil.rmtree(result_dir)
            except FileNotFoundError:
                pass
            except OSError as cleanup_exc:
                if isinstance(
                    original_exc,
                    VbCableLoopbackSubprocessShutdownUnconfirmedError,
                ):
                    raise original_exc from cleanup_exc
                raise


def check_vb_cable_loopback_isolated(
    saved_output_name: str,
    saved_output_host_api: str,
    *,
    cancel_event: Optional[threading.Event] = None,
    timeout: float = VB_CABLE_LOOPBACK_PROCESS_TIMEOUT_SECONDS,
) -> CheckResult:
    """Run the complete active test in a child that can be forcibly ended."""

    title = "VB-CABLE 本地通道"
    event = cancel_event if cancel_event is not None else threading.Event()
    try:
        result = _run_vb_cable_loopback_in_tempdir(
            saved_output_name,
            saved_output_host_api,
            cancel_event=event,
            timeout=timeout,
        )
    except single_instance.DuplicateInstanceError:
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "遥控器服务正在运行；本次未发送测试信号",
        )
    except (
        single_instance.SingleInstanceUnavailableError,
        single_instance.MutexCleanupError,
    ):
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "无法确认测试与服务互斥；结果无效，请关闭设置后重试",
        )
    except VbCableLoopbackCancelledError:
        if event.is_set():
            return CheckResult(
                "vb_cable_loopback",
                title,
                CheckGroup.OPTIONAL_DRIVER,
                CheckStatus.UNSUPPORTED,
                "通道测试已取消",
            )
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "通道测试超时，已停止测试进程；请关闭占用端点的程序后重试",
        )
    except VbCableLoopbackSubprocessShutdownUnconfirmedError:
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "通道测试超时，且未能确认测试进程已停止；请关闭设置后重试",
        )
    except Exception:  # noqa: BLE001 - never expose paths or device details
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "无法启动或完成通道测试；请稍后重试",
        )
    if result is None:
        return CheckResult(
            "vb_cable_loopback",
            title,
            CheckGroup.OPTIONAL_DRIVER,
            CheckStatus.FAIL,
            "通道测试未返回可信结果；请稍后重试",
        )
    return result


def _run_output_endpoint_preflight_in_tempdir(
    endpoint_name: str,
    endpoint_host_api: str,
    *,
    cancel_event: threading.Event,
    timeout: float,
) -> Optional[bool]:
    result_dir = tempfile.mkdtemp(prefix="ovb-google-endpoint-preflight-")
    request_path = os.path.join(result_dir, "request.json")
    result_path = os.path.join(result_dir, "result.json")
    try:
        _write_verdict_atomically(
            request_path,
            {
                "saved_output_name": endpoint_name,
                "saved_output_host_api": endpoint_host_api,
            },
        )
        command = build_output_endpoint_preflight_subprocess_command(
            request_path, result_path
        )
        return _run_output_endpoint_preflight_subprocess(
            command,
            result_path=result_path,
            cancel_event=cancel_event,
            timeout=timeout,
        )
    finally:
        original_exc = sys.exc_info()[1]
        try:
            shutil.rmtree(result_dir)
        except FileNotFoundError:
            pass
        except OSError as cleanup_exc:
            if isinstance(
                original_exc,
                OutputEndpointPreflightShutdownUnconfirmedError,
            ):
                raise original_exc from cleanup_exc
            raise


def preflight_output_endpoint_isolated(
    endpoint_name: str,
    endpoint_host_api: str,
    *,
    cancel_event: Optional[threading.Event] = None,
    timeout: float = OUTPUT_ENDPOINT_PREFLIGHT_PROCESS_TIMEOUT_SECONDS,
) -> None:
    """Verify one output endpoint without running PortAudio in this process."""

    event = cancel_event if cancel_event is not None else threading.Event()
    try:
        result = _run_output_endpoint_preflight_in_tempdir(
            endpoint_name,
            endpoint_host_api,
            cancel_event=event,
            timeout=timeout,
        )
    except OutputEndpointPreflightCancelledError as exc:
        if event.is_set():
            raise audio_output.AudioOutputUnavailableError(
                "output endpoint preflight was cancelled"
            ) from exc
        raise audio_output.AudioOutputUnavailableError(
            "output endpoint preflight timed out"
        ) from exc
    except OutputEndpointPreflightShutdownUnconfirmedError as exc:
        raise audio_output.AudioOutputUnavailableError(
            "output endpoint preflight process did not stop"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - sanitize the process boundary
        raise audio_output.AudioOutputUnavailableError(
            "output endpoint preflight failed"
        ) from exc
    if result is not True:
        raise audio_output.AudioOutputUnavailableError(
            "output endpoint is unavailable"
        )


# -- Output endpoint resolution (voice bridge) ------------------------------


def check_output_endpoint_resolution(
    saved_name: str,
    saved_host_api: str,
    *,
    list_playback: Callable[
        [], Sequence[audio_output.AudioEndpoint]
    ] = audio_output.enumerate_output_endpoints,
    preflight: Callable[[str, str], None] = audio_playback.preflight_output_endpoint,
) -> CheckResult:
    try:
        endpoints = list(list_playback())
    except audio_output.AudioOutputUnavailableError as exc:
        return CheckResult(
            "output_endpoint",
            "语音输出端点",
            CheckGroup.VOICE_BRIDGE,
            CheckStatus.UNSUPPORTED,
            f"无法检测播放端点：{exc}",
        )

    try:
        endpoint = audio_output.resolve_selected_endpoint(
            endpoints, saved_name, saved_host_api
        )
    except audio_output.AudioOutputUnavailableError as exc:
        return CheckResult(
            "output_endpoint",
            "语音输出端点",
            CheckGroup.VOICE_BRIDGE,
            CheckStatus.FAIL,
            str(exc),
        )

    try:
        preflight(endpoint.name, endpoint.host_api)
    except Exception:  # noqa: BLE001 - never expose PortAudio/device details
        return CheckResult(
            "output_endpoint",
            "语音输出端点",
            CheckGroup.VOICE_BRIDGE,
            CheckStatus.FAIL,
            "端点存在但无法播放；请选择 Windows WASAPI 或 DirectSound",
        )

    if audio_output.is_cable_input_endpoint(endpoint.name):
        return CheckResult(
            "output_endpoint",
            "语音输出端点",
            CheckGroup.VOICE_BRIDGE,
            CheckStatus.PASS,
            f"已选择 CABLE Input：{endpoint.name}",
        )
    # A real, present, non-CABLE endpoint that the preflight above just
    # proved can be opened and played to is a legitimate selection: it is
    # the endpoint the bridge will actually use. This check answers "can
    # audio leave through the saved endpoint", so it must stay green here -
    # the CABLE-Input question belongs to the loopback check, which gates
    # the bundled VB-CABLE workflow separately.
    return CheckResult(
        "output_endpoint",
        "语音输出端点",
        CheckGroup.VOICE_BRIDGE,
        CheckStatus.PASS,
        f"已选择 {endpoint.name}（非 CABLE Input）",
    )


# -- Orchestration -----------------------------------------------------------


def _isolated(
    check_id: str, title: str, group: CheckGroup, run: Callable[[], CheckResult]
) -> CheckResult:
    """Runs one check function, isolated from every other one: each
    ``check_*`` function above already anticipates
    and handles its own documented failure modes (``AudioOutputUnavailableError``,
    ``WinRTUnavailableError``, etc.), but this is the outer safety net for
    whatever it does NOT anticipate - a genuinely unexpected exception type
    must become this ONE check's own honest ``FAIL`` result, with a generic
    detail that never echoes the raw exception text (which could otherwise
    leak a device identifier/path this project's privacy contract forbids
    surfacing), rather than aborting ``run_diagnostics()`` entirely and
    leaving every OTHER check's result missing/stale.
    """

    try:
        return run()
    except Exception:  # noqa: BLE001 - the entire purpose of this wrapper
        return CheckResult(
            check_id,
            title,
            group,
            CheckStatus.FAIL,
            "检测失败；其他项目不受影响，请重新检测",
        )




def run_diagnostics(
    *,
    saved_output_name: str = "",
    saved_output_host_api: str = "",
    cancel_event: Optional[threading.Event] = None,
    selected_key: str | None = None,
) -> DiagnosticsReport:
    """Runs every check and returns a stable report. Pure aside from the
    real OS/WinRT/PortAudio calls each check function makes on Windows;
    intended to be called from a background thread (see
    ``qt_settings_app.DiagnosticsController``), never on the Qt GUI thread.

    ``cancel_event`` , when given, is forwarded to the subprocess-
    isolated output-endpoint preflight check.
    Setting it from another thread cancels either child in bounded time via
    confirmed process termination rather than leaving a native call alive
    during interpreter shutdown.

    Every check is isolated (see ``_isolated()``): one check's unexpected
    failure can never prevent the other four from rendering their own real
    result - UNLESS ``cancel_event`` becomes set 
    once shutdown has been requested, this function stops running any
    FURTHER checks after whichever one just completed and returns
    immediately with only the checks completed so far. This report is about
    to be discarded unemitted anyway once shutdown has begun (see
    ``qt_settings_app.py``'s ``_run_in_background()``, which skips the emit
    entirely once ``_diagnostics_shutdown_event`` is set) - continuing to
    run the remaining checks after that point would only prolong how long
    the background worker thread takes to actually finish, for no
    observable benefit, working directly against the whole point of a
    bounded shutdown. With no ``cancel_event`` (the default), this early
    stop never triggers and all five checks always run.
    """

    endpoint_preflight = functools.partial(
        preflight_output_endpoint_isolated,
        cancel_event=cancel_event,
    )
    check_specs = (
        ("os_version", "Windows 版本与 64 位架构", CheckGroup.ORDINARY_BUTTONS, check_os_version),
        ("raw_input", "Raw Input 按键设备", CheckGroup.ORDINARY_BUTTONS,
         lambda: check_raw_input(selected_key=selected_key)),
        (
            "vb_cable_endpoints",
            "VB-CABLE 虚拟音频端点（可选）",
            CheckGroup.OPTIONAL_DRIVER,
            check_vb_cable_endpoints,
        ),
        (
            "output_endpoint",
            "语音输出端点",
            CheckGroup.VOICE_BRIDGE,
            lambda: check_output_endpoint_resolution(
                saved_output_name,
                saved_output_host_api,
                preflight=endpoint_preflight,
            ),
        ),
    )
    checks: "list[CheckResult]" = []
    for check_id, title, group, run in check_specs:
        checks.append(_isolated(check_id, title, group, run))
        if cancel_event is not None and cancel_event.is_set():
            break
    return DiagnosticsReport(checks=tuple(checks))
