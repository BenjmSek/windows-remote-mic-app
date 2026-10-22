from __future__ import annotations

import sys
import subprocess
import ctypes
from ctypes import wintypes

from ovb.__main__ import main

# Child / worker entry flags. These are launched by the running application
# (often already elevated) through this same launcher; the elevation guard must
# never re-request UAC for them or every injected worker would pop a prompt.
_SUBPROCESS_FLAGS = (
    "--chromecast-worker",
    "--capture-hotkey-worker",
    "--google-hid-injector",
    "--google-source-injector",
    "--doubao-handsfree-finish",
    "--observe-shortcut",
    "--sogou-normal-submit",
    "--list-remote-devices",
    "--diagnose-vb-cable-loopback",
    "--preflight-output-endpoint",
    "--element-navigation",
    # Elevated child that registers/removes the HighestAvailable login task.
    # It is launched through ``runas`` by the parent, so it must never ask for
    # elevation again.
    "--manage-autostart-task",
)

_SEE_MASK_NOCLOSEPROCESS = 0x00000040


class _ShellExecuteInfoW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_uint32),
        ("hIconOrMonitor", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


def _is_subprocess_invocation(argv) -> bool:
    return any(flag in argv for flag in _SUBPROCESS_FLAGS)


def _should_self_elevate(argv) -> bool:
    # Only Windows has UAC / integrity levels to deal with.
    if sys.platform != "win32":
        return False
    # Frozen builds use their own packaged entry point, not this launcher.
    if getattr(sys, "frozen", False):
        return False
    # Workers / child helpers must keep their parent's elevation; never recurse.
    if _is_subprocess_invocation(argv):
        return False
    return True


def _restart_elevated(argv) -> bool:
    """Re-launch THIS launcher via the ``runas`` verb (UAC prompt).

    Returns True when an elevated copy was launched (caller should exit);
    False when the user cancelled UAC or the launch failed (caller should
    fall back to running at the current integrity level).
    """
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = (ctypes.POINTER(_ShellExecuteInfoW),)
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    params = subprocess.list2cmdline([sys.argv[0]] + list(argv))
    info = _ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = params
    info.nShow = 0
    ctypes.set_last_error(0)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return False
    if info.hProcess:
        kernel32.CloseHandle(info.hProcess)
    return True


if __name__ == "__main__":
    argv = sys.argv[1:]
    if _should_self_elevate(argv):
        try:
            from ovb.hid_elevation_windows import is_process_elevated
            if not is_process_elevated() and _restart_elevated(sys.argv):
                sys.exit(0)
        except Exception:
            # Elevation probe or relaunch failed; run as-is (medium IL).
            # High-integrity windows simply won't receive injected input.
            pass
    main()
