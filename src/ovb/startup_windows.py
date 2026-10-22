"""User-controlled Windows login startup for the Remote Mic desktop shell."""

from __future__ import annotations

import html
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable, Optional, Sequence

from . import dev_session, product_identity


RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "RemoteMicChromecast"
BACKGROUND_START_FLAG = "--background"
# Name of the pre-authorized (HighestAvailable) login task used by the source
# build. The packaged build keeps its own helper task and is untouched here.
# The leading backslash is mandatory: hid_elevation_windows._task_path_parts
# rejects any task name that is not fully qualified, and every query / delete
# goes through it (registration alone does not). Without the prefix the task
# could be created but never read back or removed.
AUTOSTART_TASK_NAME = r"\RemoteMicChromecastAutostart"
# Hidden child-process flag. The parent (medium integrity) re-launches itself
# through the ``runas`` verb with this flag so the registration runs elevated.
AUTOSTART_TASK_MAINTENANCE_FLAG = "--manage-autostart-task"
# Bounded wait for the elevated child to finish Task Scheduler work.
AUTOSTART_TASK_MAINTENANCE_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class StartupState:
    enabled: bool
    error: str = ""


CommandLineParser = Callable[[str], Sequence[str]]


def build_startup_command(
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    source_launcher: Optional[str] = None,
) -> list[str]:
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if executable is None:
        executable = sys.executable
    if not executable:
        raise ValueError("sys.executable is empty")
    if frozen:
        return dev_session.mark_command([executable, BACKGROUND_START_FLAG])
    launcher = source_launcher or str(
        Path(__file__).resolve().parents[1] / "launcher.py"
    )
    return dev_session.mark_command(
        [executable, launcher, BACKGROUND_START_FLAG]
    )


def command_line(command: Optional[Sequence[str]] = None) -> str:
    resolved = list(command) if command is not None else build_startup_command()
    if not resolved or not str(resolved[0]).strip():
        raise ValueError("startup command is empty")
    return subprocess.list2cmdline([str(part) for part in resolved])


def _load_winreg():
    import winreg

    return winreg


def _parse_windows_command_line(command: str) -> list[str]:
    """Parse one Windows command line with the system's argv rules."""

    import ctypes

    if not command or "\0" in command:
        raise ValueError("startup command is invalid")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = (
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_int),
    )
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    argument_count = ctypes.c_int(0)
    arguments = shell32.CommandLineToArgvW(
        command,
        ctypes.byref(argument_count),
    )
    if not arguments:
        raise OSError(ctypes.get_last_error(), "CommandLineToArgvW failed")
    try:
        return [str(arguments[index]) for index in range(argument_count.value)]
    finally:
        kernel32.LocalFree(arguments)


def _owned_frozen_startup_command(
    value: object,
    *,
    parser: CommandLineParser,
) -> bool:
    if not isinstance(value, str):
        return False
    try:
        arguments = [str(argument) for argument in parser(value)]
    except (OSError, ValueError, TypeError):
        return False
    if len(arguments) != 2 or arguments[1] != BACKGROUND_START_FLAG:
        return False
    executable = PureWindowsPath(arguments[0])
    if not executable.is_absolute() or not product_identity.is_recognized_windows_executable_name(
        executable.name
    ):
        return False
    return command_line(arguments) == value


def rebind_owned_frozen_startup(
    *,
    platform: Optional[str] = None,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    current_command: Optional[str] = None,
    winreg_module=None,
    command_parser: Optional[CommandLineParser] = None,
) -> StartupState:
    """Move this product's existing login entry to the current frozen copy."""

    if dev_session.is_isolated():
        return StartupState(False, "隔离测试不修改随系统启动。")
    current_platform = sys.platform if platform is None else platform
    current_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if current_platform != "win32" or not current_frozen:
        return StartupState(False)
    current_executable = PureWindowsPath(executable or sys.executable)
    if not current_executable.is_absolute() or not product_identity.is_recognized_windows_executable_name(
        current_executable.name
    ):
        return StartupState(False, "ValueError")
    expected = current_command or command_line(
        [str(current_executable), BACKGROUND_START_FLAG]
    )
    parser = command_parser or _parse_windows_command_line
    try:
        winreg = winreg_module or _load_winreg()
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            winreg.KEY_QUERY_VALUE,
        ) as key:
            value, value_type = winreg.QueryValueEx(key, RUN_VALUE_NAME)
        if value_type != winreg.REG_SZ:
            return StartupState(False)
        if str(value) == expected:
            return StartupState(True)
        if not _owned_frozen_startup_command(value, parser=parser):
            return StartupState(False)
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(
                key,
                RUN_VALUE_NAME,
                0,
                winreg.REG_SZ,
                expected,
            )
        return StartupState(True)
    except FileNotFoundError:
        return StartupState(False)
    except (OSError, ValueError, TypeError) as exc:
        return StartupState(False, type(exc).__name__)


def read_startup_state(
    *,
    platform: Optional[str] = None,
    expected_command: Optional[str] = None,
    winreg_module=None,
) -> StartupState:
    if dev_session.is_isolated():
        return StartupState(False, "隔离测试不使用随系统启动。")
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return StartupState(False, "仅 Windows 支持随系统启动。")
    current_frozen = bool(getattr(sys, "frozen", False))
    try:
        winreg = winreg_module or _load_winreg()
        expected = expected_command or command_line()
        if not current_frozen and autostart_task_registered():
            # Source build: the pre-authorized HighestAvailable task is the
            # real mechanism now. Report enabled regardless of the legacy Run
            # entry so the switch reflects the task, not the fallback.
            return StartupState(True)
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            winreg.KEY_QUERY_VALUE,
        ) as key:
            value, value_type = winreg.QueryValueEx(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        return StartupState(False)
    except (OSError, ValueError, TypeError) as exc:
        return StartupState(False, type(exc).__name__)
    enabled = value_type == winreg.REG_SZ and str(value) == expected
    return StartupState(enabled)


def set_startup_enabled(
    enabled: bool,
    *,
    platform: Optional[str] = None,
    startup_command: Optional[str] = None,
    winreg_module=None,
    elevation_runner: Optional[Callable[[str], tuple[bool, str]]] = None,
) -> StartupState:
    """Enable or disable login startup.

    The source build always uses the pre-authorized ``HighestAvailable`` login
    task, so every logon starts the app elevated with no UAC prompt. When the
    current process is not elevated it re-launches itself through ``runas``
    (one UAC prompt at the moment the switch is flipped) and lets that elevated
    child do the registration.

    There is deliberately no silent fallback to the medium-integrity Run key:
    falling back would quietly reintroduce a UAC prompt on every boot while
    the UI still reported "enabled".
    """
    if dev_session.is_isolated():
        return StartupState(False, "隔离测试不修改随系统启动。")
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32":
        return StartupState(False, "仅 Windows 支持随系统启动。")
    current_frozen = bool(getattr(sys, "frozen", False))
    try:
        winreg = winreg_module or _load_winreg()
        if current_frozen:
            # Packaged build: keep the original Run-key behavior (it ships its
            # own HighestAvailable helper for elevation).
            if enabled:
                value = startup_command or command_line()
                with winreg.CreateKeyEx(
                    winreg.HKEY_CURRENT_USER,
                    RUN_KEY_PATH,
                    0,
                    winreg.KEY_SET_VALUE,
                ) as key:
                    winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, value)
                return StartupState(True)
            _delete_run_value(winreg)
            return StartupState(False)

        if enabled:
            runner = elevation_runner or _run_autostart_task_maintenance_elevated
            succeeded, detail = runner("install")
            if not succeeded:
                # Never silently write the plain Run entry: that would restore
                # a UAC prompt on every boot while claiming success.
                return StartupState(False, detail)
            # Registration done; drop any legacy Run entry so the app is not
            # launched twice per logon.
            _delete_run_value(winreg)
            return StartupState(True)

        # Disabled: remove both the task and the legacy Run entry.
        runner = elevation_runner or _run_autostart_task_maintenance_elevated
        succeeded, detail = runner("remove")
        _delete_run_value(winreg)
        if not succeeded:
            return StartupState(False, detail)
        return StartupState(False)
    except (OSError, ValueError, TypeError) as exc:
        return StartupState(not bool(enabled), type(exc).__name__)


def _is_app_elevated() -> bool:
    try:
        from .hid_elevation_windows import is_process_elevated

        return bool(is_process_elevated())
    except Exception:
        return False


def autostart_task_registered() -> bool:
    """True when the pre-authorized login task exists."""
    try:
        from .hid_elevation_windows import _read_registered_task

        return _read_registered_task(AUTOSTART_TASK_NAME) is not None
    except Exception:
        return False


def run_autostart_task_maintenance(action: str) -> int:
    """Perform the actual Task Scheduler work.

    Runs inside the elevated child process launched through ``runas``. Returns
    a process exit code: 0 on success, non-zero otherwise.
    """
    if action == "install":
        return 0 if _register_autostart_task() else 1
    if action == "remove":
        return 0 if _unregister_autostart_task() else 1
    return 1


def _run_autostart_task_maintenance_elevated(action: str) -> tuple[bool, str]:
    """Register/remove the login task, elevating through UAC when needed."""
    if _is_app_elevated():
        if action == "install":
            if _register_autostart_task():
                return True, ""
            return False, "无法注册提权登录任务。"
        if _unregister_autostart_task():
            return True, ""
        return False, "无法删除提权登录任务。"
    if not _can_self_elevate():
        return False, "当前账户无法自我提权，不能注册提权登录任务。"
    try:
        exit_code = _launch_elevated_maintenance(action)
    except _StartupElevationCancelled:
        return False, "已取消提权，未修改随 Windows 启动。"
    except Exception:
        return False, "提权进程启动失败，未修改随 Windows 启动。"
    if exit_code != 0:
        return False, "提权任务维护失败，未修改随 Windows 启动。"
    return True, ""


class _StartupElevationCancelled(Exception):
    """Raised when the user dismisses the UAC prompt."""


def _can_self_elevate() -> bool:
    try:
        from .hid_elevation_windows import can_current_user_self_elevate

        return bool(can_current_user_self_elevate())
    except Exception:
        return True


def _launch_elevated_maintenance(action: str) -> int:
    """Re-launch this launcher via ``runas`` to run maintenance elevated."""
    from . import hid_elevation_windows as hid

    launcher = str(Path(__file__).resolve().parents[1] / "launcher.py")
    arguments = subprocess.list2cmdline(
        [launcher, AUTOSTART_TASK_MAINTENANCE_FLAG, action]
    )
    if dev_session.is_active():
        arguments += " " + dev_session.DEV_SESSION_FLAG
    try:
        return hid._run_elevated_and_wait(
            Path(sys.executable),
            arguments,
            AUTOSTART_TASK_MAINTENANCE_TIMEOUT_SECONDS,
        )
    except hid.HidElevationError as exc:
        if str(exc) == "uac_cancelled":
            raise _StartupElevationCancelled() from exc
        raise


def _register_autostart_task() -> bool:
    try:
        from . import hid_elevation_windows as hid

        command = build_startup_command()
        # Repository root is two levels up (src/ovb/startup_windows.py ->
        # parents[2]); parents[1] would be ``src`` itself and would make the
        # task's working directory the package folder.
        repo_root = str(Path(__file__).resolve().parents[2])
        sid = hid.current_user_sid()
        xml = _build_autostart_task_xml(command, repo_root, sid)
        sddl = hid.task_security_sddl(sid)
        hid._register_task(AUTOSTART_TASK_NAME, xml, sddl)
        return True
    except Exception:
        return False


def _unregister_autostart_task() -> bool:
    try:
        from . import hid_elevation_windows as hid

        hid._delete_task(AUTOSTART_TASK_NAME, missing_ok=True)
        return True
    except Exception:
        return False


def _delete_run_value(winreg) -> None:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        pass


def _build_autostart_task_xml(
    command_parts: Sequence[str],
    working_dir: str,
    user_sid: str,
) -> str:
    """Build a login (LogonTrigger) scheduled-task XML that runs the app with
    RunLevel=HighestAvailable, so it starts elevated on every logon without a
    UAC prompt. Layout mirrors the project's existing HID-helper task."""
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    sid = str(user_sid).strip()
    executable = str(command_parts[0])
    arguments = subprocess.list2cmdline([str(p) for p in command_parts[1:]])
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="{namespace}">
  <RegistrationInfo>
    <Description>Remote Mic Chromecast login autostart (elevated).</Description>
    <URI>{AUTOSTART_TASK_NAME}</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>{html.escape(sid)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{html.escape(sid)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{html.escape(executable)}</Command>
      <Arguments>{html.escape(arguments)}</Arguments>
      <WorkingDirectory>{html.escape(working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
