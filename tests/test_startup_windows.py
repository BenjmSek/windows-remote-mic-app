import sys
import unittest
from unittest import mock

from ovb import startup_windows


class _Key:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, value=None, *, value_type=None, set_error=None):
        self.value = value
        self.value_type = self.REG_SZ if value_type is None else value_type
        self.set_error = set_error
        self.set_calls = 0

    def OpenKey(self, root, path, reserved, access):
        if self.value is None:
            raise FileNotFoundError(path)
        return _Key()

    def CreateKeyEx(self, root, path, reserved, access):
        return _Key()

    def QueryValueEx(self, key, name):
        if self.value is None:
            raise FileNotFoundError(name)
        return self.value, self.value_type

    def SetValueEx(self, key, name, reserved, value_type, value):
        self.set_calls += 1
        if self.set_error is not None:
            raise self.set_error
        self.value = value

    def DeleteValue(self, key, name):
        if self.value is None:
            raise FileNotFoundError(name)
        self.value = None


class StartupWindowsTests(unittest.TestCase):
    def test_new_versioned_unicode_executable_builds_startup_command(self):
        command = startup_windows.command_line(
            startup_windows.build_startup_command(
                frozen=True,
                executable=(
                    r"C:\无线麦Mod 测试\无线麦Mod win版 "
                    r"1.0.51-candidate.51.exe"
                ),
            )
        )
        self.assertEqual(
            command,
            '"C:\\无线麦Mod 测试\\无线麦Mod win版 1.0.51-candidate.51.exe" --background',
        )

    def test_frozen_command_uses_background_shell(self):
        self.assertEqual(
            startup_windows.build_startup_command(
                frozen=True, executable=r"C:\Apps\Remote Mic\无线麦Mod win版 1.0.51-candidate.51.exe"
            ),
            [r"C:\Apps\Remote Mic\无线麦Mod win版 1.0.51-candidate.51.exe", "--background"],
        )

    def test_source_command_uses_standalone_launcher(self):
        self.assertEqual(
            startup_windows.build_startup_command(
                frozen=False,
                executable=r"C:\Python312\python.exe",
                source_launcher=r"D:\Remote Mic\src\launcher.py",
            ),
            [
                r"C:\Python312\python.exe",
                r"D:\Remote Mic\src\launcher.py",
                "--background",
            ],
        )

    def test_default_source_launcher_exists(self):
        command = startup_windows.build_startup_command(
            frozen=False, executable=r"C:\Python312\python.exe"
        )
        self.assertTrue(startup_windows.Path(command[1]).is_file())

    def test_read_requires_the_exact_owned_command(self):
        registry = _FakeWinreg('"C:\\Apps\\无线麦Mod win版 1.0.51-candidate.51.exe" --settings')
        # The task probe must be stubbed out: on a developer machine that has
        # actually flipped the switch the login task exists for real, and a
        # live task would short-circuit the Run-value comparison under test.
        with mock.patch.object(
            startup_windows, "autostart_task_registered", return_value=False
        ):
            state = startup_windows.read_startup_state(
                platform="win32",
                expected_command='"C:\\Apps\\无线麦Mod win版 1.0.51-candidate.51.exe" --background',
                winreg_module=registry,
            )
        self.assertFalse(state.enabled)
        self.assertEqual(state.error, "")

    def test_enable_and_disable_round_trip(self):
        # The packaged build still ships its own elevated helper, so it keeps
        # the plain Run-key behavior. (The source build now uses the
        # pre-authorized login task - see the tests below.)
        registry = _FakeWinreg()
        command = '"C:\\Apps\\无线麦Mod win版 1.0.51-candidate.51.exe" --background'
        with unittest.mock.patch.object(sys, "frozen", True, create=True):
            enabled = startup_windows.set_startup_enabled(
                True,
                platform="win32",
                startup_command=command,
                winreg_module=registry,
            )
            self.assertTrue(enabled.enabled)
            self.assertEqual(registry.value, command)
            disabled = startup_windows.set_startup_enabled(
                False,
                platform="win32",
                winreg_module=registry,
            )
        self.assertFalse(disabled.enabled)
        self.assertIsNone(registry.value)

    def test_source_build_delegates_to_the_elevated_task_runner(self):
        # The source build must never write a medium-integrity Run entry:
        # that would silently restore a UAC prompt on every boot. It asks the
        # elevation runner to register the HighestAvailable login task.
        registry = _FakeWinreg()
        calls = []

        def runner(action):
            calls.append(action)
            return True, ""

        enabled = startup_windows.set_startup_enabled(
            True,
            platform="win32",
            winreg_module=registry,
            elevation_runner=runner,
        )
        self.assertTrue(enabled.enabled)
        self.assertEqual(enabled.error, "")
        self.assertEqual(calls, ["install"])
        # No silent Run-key fallback.
        self.assertIsNone(registry.value)

    def test_source_build_reports_failure_without_writing_the_run_entry(self):
        registry = _FakeWinreg()
        state = startup_windows.set_startup_enabled(
            True,
            platform="win32",
            winreg_module=registry,
            elevation_runner=lambda action: (False, "已取消提权，未修改随 Windows 启动。"),
        )
        self.assertFalse(state.enabled)
        self.assertIn("取消提权", state.error)
        self.assertIsNone(registry.value)

    def test_source_build_disable_delegates_removal_and_clears_run_entry(self):
        registry = _FakeWinreg("stale-command")
        calls = []

        def runner(action):
            calls.append(action)
            return True, ""

        state = startup_windows.set_startup_enabled(
            False,
            platform="win32",
            winreg_module=registry,
            elevation_runner=runner,
        )
        self.assertFalse(state.enabled)
        self.assertEqual(calls, ["remove"])
        self.assertIsNone(registry.value)

    def test_maintenance_flag_register_or_remove_actions(self):
        self.assertEqual(startup_windows.run_autostart_task_maintenance("install"), 1)
        self.assertEqual(startup_windows.run_autostart_task_maintenance("remove"), 1)
        self.assertEqual(startup_windows.run_autostart_task_maintenance("bogus"), 1)

    def test_non_windows_fails_without_touching_registry(self):
        state = startup_windows.set_startup_enabled(
            True, platform="linux", winreg_module=_FakeWinreg()
        )
        self.assertFalse(state.enabled)
        self.assertIn("Windows", state.error)

    def test_rebind_owned_frozen_startup_moves_old_portable_command_to_current_path(self):
        old_command = startup_windows.command_line(
            [r"D:\旧版 无线麦Mod\无线麦Mod win版 1.0.51-candidate.51.exe", "--background"]
        )
        current_command = startup_windows.command_line(
            [r"E:\新版 无线麦Mod\无线麦Mod win版 1.0.51-candidate.51.exe", "--background"]
        )
        registry = _FakeWinreg(old_command)

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\新版 无线麦Mod\无线麦Mod win版 1.0.51-candidate.51.exe",
            current_command=current_command,
            winreg_module=registry,
            command_parser=lambda _value: [
                r"D:\旧版 无线麦Mod\无线麦Mod win版 1.0.51-candidate.51.exe",
                "--background",
            ],
        )

        self.assertTrue(state.enabled)
        self.assertEqual(state.error, "")
        self.assertEqual(registry.value, current_command)
        self.assertEqual(registry.set_calls, 1)

    def test_rebind_owned_frozen_startup_leaves_a_retired_entry_alone(self):
        # "RemoteMicRC003.exe" was the pre-versioned portable copy. It is not
        # a shipped name any more, so a login entry still pointing at one is
        # no longer recognized as ours and must be left untouched rather than
        # rewritten to the current copy.
        old_executable = r"D:\旧版\RemoteMicRC003.exe"
        old_command = startup_windows.command_line(
            [old_executable, "--background"]
        )
        registry = _FakeWinreg(old_command)

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\新版 无线麦Mod\无线麦Mod win版 1.0.51-candidate.51.exe",
            winreg_module=registry,
            command_parser=lambda _value: [old_executable, "--background"],
        )

        self.assertFalse(state.enabled)
        self.assertEqual(state.error, "")
        self.assertEqual(registry.value, old_command)
        self.assertEqual(registry.set_calls, 0)

    def test_rebind_owned_frozen_startup_migrates_between_versioned_names(self):
        old_executable = r"D:\旧版\无线麦Mod win版 1.0.50-candidate.50.exe"
        new_executable = r"E:\新版\无线麦Mod win版 1.0.51-candidate.51.exe"
        old_command = startup_windows.command_line(
            [old_executable, "--background"]
        )
        registry = _FakeWinreg(old_command)

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=new_executable,
            winreg_module=registry,
            command_parser=lambda _value: [old_executable, "--background"],
        )

        self.assertTrue(state.enabled)
        self.assertEqual(
            registry.value,
            startup_windows.command_line([new_executable, "--background"]),
        )

    def test_rebind_owned_frozen_startup_rejects_unrecognized_current_name(self):
        registry = _FakeWinreg(
            startup_windows.command_line(
                [r"D:\旧版\无线麦Mod win版 1.0.51-candidate.51.exe", "--background"]
            )
        )
        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\新版\无线麦Mod.exe",
            winreg_module=registry,
            command_parser=lambda _value: self.fail("parser should not run"),
        )
        self.assertFalse(state.enabled)
        self.assertEqual(state.error, "ValueError")
        self.assertEqual(registry.set_calls, 0)

    def test_rebind_owned_frozen_startup_works_when_old_exe_is_missing(self):
        old_command = startup_windows.command_line(
            [r"D:\已删除\无线麦Mod win版 1.0.51-candidate.51.exe", "--background"]
        )
        registry = _FakeWinreg(old_command)

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\当前\无线麦Mod win版 1.0.51-candidate.51.exe",
            winreg_module=registry,
            command_parser=lambda _value: [
                r"D:\已删除\无线麦Mod win版 1.0.51-candidate.51.exe",
                "--background",
            ],
        )

        self.assertTrue(state.enabled)
        self.assertEqual(
            registry.value,
            startup_windows.command_line(
                [r"E:\当前\无线麦Mod win版 1.0.51-candidate.51.exe", "--background"]
            ),
        )

    def test_rebind_owned_frozen_startup_same_command_does_not_write(self):
        current_command = startup_windows.command_line(
            [r"E:\当前\无线麦Mod win版 1.0.51-candidate.51.exe", "--background"]
        )
        registry = _FakeWinreg(current_command)

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\当前\无线麦Mod win版 1.0.51-candidate.51.exe",
            current_command=current_command,
            winreg_module=registry,
            command_parser=lambda _value: self.fail("parser should not run"),
        )

        self.assertTrue(state.enabled)
        self.assertEqual(registry.set_calls, 0)

    def test_rebind_owned_frozen_startup_rejects_unknown_or_extra_arguments(self):
        current_executable = r"E:\当前\无线麦Mod win版 1.0.51-candidate.51.exe"
        cases = (
            ([r"D:\旧版\无线麦Mod win版 1.0.51-candidate.51.exe", "--settings"],),
            ([r"D:\旧版\无线麦Mod win版 1.0.51-candidate.51.exe", "--background", "--bridge"],),
            ([r"D:\旧版\Other.exe", "--background"],),
            ([r"python.exe", r"D:\launcher.py", "--background"],),
        )
        for (arguments,) in cases:
            with self.subTest(arguments=arguments):
                old_command = startup_windows.command_line(arguments)
                registry = _FakeWinreg(old_command)
                state = startup_windows.rebind_owned_frozen_startup(
                    platform="win32",
                    frozen=True,
                    executable=current_executable,
                    winreg_module=registry,
                    command_parser=lambda _value, arguments=arguments: arguments,
                )
                self.assertFalse(state.enabled)
                self.assertEqual(state.error, "")
                self.assertEqual(registry.value, old_command)
                self.assertEqual(registry.set_calls, 0)

    def test_rebind_owned_frozen_startup_rejects_non_string_registry_value(self):
        registry = _FakeWinreg(
            [r"D:\旧版\无线麦Mod win版 1.0.51-candidate.51.exe", "--background"]
        )

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\当前\无线麦Mod win版 1.0.51-candidate.51.exe",
            winreg_module=registry,
            command_parser=lambda _value: self.fail("parser should not run"),
        )

        self.assertFalse(state.enabled)
        self.assertEqual(registry.set_calls, 0)

    def test_rebind_owned_frozen_startup_write_failure_preserves_old_value(self):
        old_command = startup_windows.command_line(
            [r"D:\旧版\无线麦Mod win版 1.0.51-candidate.51.exe", "--background"]
        )
        registry = _FakeWinreg(old_command, set_error=PermissionError("denied"))

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\当前\无线麦Mod win版 1.0.51-candidate.51.exe",
            winreg_module=registry,
            command_parser=lambda _value: [
                r"D:\旧版\无线麦Mod win版 1.0.51-candidate.51.exe",
                "--background",
            ],
        )

        self.assertFalse(state.enabled)
        self.assertEqual(state.error, "PermissionError")
        self.assertEqual(registry.value, old_command)
        self.assertEqual(registry.set_calls, 1)

    def test_rebind_owned_frozen_startup_ignores_non_string_registry_type(self):
        old_command = startup_windows.command_line(
            [r"D:\旧版\无线麦Mod win版 1.0.51-candidate.51.exe", "--background"]
        )
        registry = _FakeWinreg(old_command, value_type=7)

        state = startup_windows.rebind_owned_frozen_startup(
            platform="win32",
            frozen=True,
            executable=r"E:\当前\无线麦Mod win版 1.0.51-candidate.51.exe",
            winreg_module=registry,
            command_parser=lambda _value: self.fail("parser should not run"),
        )

        self.assertFalse(state.enabled)
        self.assertEqual(registry.value, old_command)
        self.assertEqual(registry.set_calls, 0)

    def test_autostart_task_xml_pins_the_logon_trigger_to_one_user(self):
        # A bare <LogonTrigger/> fires for every account on the machine while
        # the task itself still runs as the registered user; a second account
        # logging on would therefore produce a failed start. Pin the trigger
        # to the same SID as the principal.
        sid = "S-1-5-21-1-2-3-1001"
        xml = startup_windows._build_autostart_task_xml(
            ["C:\\Python\\pythonw.exe", r"D:\repo\src\launcher.py", "--background"],
            r"D:\repo",
            sid,
        )

        # One occurrence for the principal, one for the logon trigger.
        self.assertEqual(xml.count(f"<UserId>{sid}</UserId>"), 2)
        self.assertIn(
            f"<WorkingDirectory>D:\\repo</WorkingDirectory>",
            xml,
        )

    def test_autostart_task_name_is_fully_qualified(self):
        # Registration reaches COM directly, but every read / delete goes
        # through hid_elevation_windows._task_path_parts, which rejects a bare
        # task name. A bare name therefore produces a task that can be created
        # but never found again -- the switch would report "delete failed" and
        # the state reader would always claim "not enabled".
        from ovb import hid_elevation_windows as hid

        self.assertTrue(
            startup_windows.AUTOSTART_TASK_NAME.startswith("\\"),
            "task name must be fully qualified for _task_path_parts",
        )
        folder_parts, leaf_name = hid._task_path_parts(startup_windows.AUTOSTART_TASK_NAME)
        self.assertEqual(folder_parts, ())
        self.assertEqual(leaf_name, "RemoteMicChromecastAutostart")

    def test_registered_task_uses_the_repository_root_as_working_directory(self):
        # parents[1] of src/ovb/startup_windows.py is "src"; the task must run
        # with the checkout root as its working directory so relative paths
        # never resolve inside the package folder.
        from pathlib import Path

        from ovb import hid_elevation_windows as hid

        captured = {}

        def fake_register(name, xml, sddl):
            captured["name"] = name
            captured["xml"] = xml

        with mock.patch.object(hid, "current_user_sid", return_value="S-1-5-21-9-8-7-1001"), \
                mock.patch.object(hid, "task_security_sddl", return_value="O:BAG:BAD:"), \
                mock.patch.object(hid, "_register_task", side_effect=fake_register):
            succeeded = startup_windows._register_autostart_task()

        self.assertTrue(succeeded)
        self.assertEqual(captured["name"], startup_windows.AUTOSTART_TASK_NAME)
        repo_root = str(Path(startup_windows.__file__).resolve().parents[2])
        self.assertIn(f"<WorkingDirectory>{repo_root}</WorkingDirectory>", captured["xml"])
        self.assertFalse(repo_root.replace("\\", "/").endswith("/src"))


if __name__ == "__main__":
    unittest.main()
