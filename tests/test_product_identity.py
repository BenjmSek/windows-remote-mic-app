import inspect
import json
import subprocess
import sys
import unittest
from pathlib import Path

from ovb import (
    bridge_tray_windows,
    key_mapping,
    product_identity,
    qt_settings_app,
    settings_ui,
    voice_hotkey_sync_windows,
    voice_program_manager,
)


_OVB_ROOT = Path(__file__).resolve().parents[1]


class ProductIdentityTests(unittest.TestCase):
    def test_windows_portable_presentation_uses_full_version(self):
        version = "1.0.51-candidate.51"
        self.assertEqual(
            product_identity.windows_presentation_label(version),
            "无线麦Mod win版 1.0.51-candidate.51",
        )
        self.assertEqual(
            product_identity.windows_executable_name(version),
            "无线麦Mod win版 1.0.51-candidate.51.exe",
        )
        self.assertEqual(
            product_identity.windows_fixed_file_version(version),
            (1, 0, 51, 0),
        )
        self.assertTrue(product_identity.windows_version_is_prerelease(version))

    def test_windows_executable_recognition_is_exact_and_layout_aware(self):
        cases = {
            "无线麦Mod win版 1.0.51-candidate.51.exe": "current",
            "无线麦Mod WIN版 1.0.51-candidate.51.EXE": "current",
            "无线麦Mod win版 1.0.51.exe": "current",
        }
        for name, layout in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    product_identity.recognized_windows_executable_layout(name),
                    layout,
                )

        # The pre-versioned "RemoteMicRC003.exe" copy is no longer a shipped
        # layout: recognizing it would only matter to an uninstaller or
        # updater that has to find a helper under this build's old "_internal"
        # directory, and no such copy is produced any more.
        for name in (
            "RemoteMicRC003.exe",
            "REMOTEMICChromecast.EXE",
            "RemoteMicChromecast-old.exe",
            "无线麦Mod win版.exe",
            "无线麦Mod win版 1.0.exe",
            "无线麦Mod win版 01.0.51.exe",
            "无线麦Mod win版 1.0.51-.exe",
            "C:\\Apps\\无线麦Mod win版 1.0.51.exe",
            "..\\无线麦Mod win版 1.0.51.exe",
            "无线麦Mod win版 1.0.51.exe.bak",
        ):
            with self.subTest(name=name):
                self.assertIsNone(
                    product_identity.recognized_windows_executable_layout(name)
                )

        with self.assertRaises(ValueError):
            product_identity.windows_runtime_relative_directory_for_executable(
                "RemoteMicRC003.exe"
            )
        self.assertEqual(
            product_identity.windows_runtime_relative_directory_for_executable(
                "无线麦Mod win版 1.0.51-candidate.51.exe"
            ),
            Path("程序文件"),
        )

    def test_windows_version_rejects_unsafe_or_unrepresentable_values(self):
        for version in (
            " 1.0.51",
            "1.0",
            "1.0.51-",
            "1.0.51/other",
            "1.0.51\\other",
            "01.0.51",
            "1.0.51-candidate..51",
        ):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    product_identity.validate_version(version)
        with self.assertRaises(ValueError):
            product_identity.windows_fixed_file_version("65536.0.0")


    def test_windows_version_components_reject_unrepresentable_values(self):
        with self.assertRaises(ValueError):
            product_identity.windows_fixed_file_version("65536.0.0")
        self.assertEqual(
            product_identity.windows_fixed_file_version("1.0.51"), (1, 0, 51, 0)
        )
        self.assertFalse(product_identity.windows_version_is_prerelease("1.0.51"))

    def test_user_visible_name_has_one_runtime_source(self):
        self.assertEqual(product_identity.DISPLAY_NAME, "无线麦Mod")
        self.assertEqual(
            settings_ui._action_to_display(
                key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_REMOTE_MIC)
            ),
            "打开无线麦Mod",
        )

        tooltip_default = inspect.signature(
            bridge_tray_windows.BridgeTray
        ).parameters["tooltip"].default
        self.assertEqual(tooltip_default, "无线麦Mod · 谷歌 Chromecast 遥控器")

    def test_voice_statuses_use_the_display_name(self):
        read_result = voice_hotkey_sync_windows.read_provider_hotkey(
            voice_program_manager.VOICE_PROGRAM_CUSTOM,
            platform="win32",
        )
        save_result = voice_hotkey_sync_windows.sync_provider_hotkey(
            voice_program_manager.VOICE_PROGRAM_WETYPE,
            "lctrl+lshift+f9",
            platform="win32",
        )
        disabled_status = voice_program_manager.VoiceProgramStatus(
            provider_id=voice_program_manager.VOICE_PROGRAM_NONE,
            display_name="不管理",
            available=False,
            running=False,
            elevated=None,
            executable=None,
            code="disabled",
        )

        for text in (
            read_result.message,
            save_result.message,
        ):
            self.assertIn(product_identity.DISPLAY_NAME, text)
            self.assertNotIn("Remote Mic", text)
        self.assertEqual(voice_program_manager.status_text(disabled_status),
                         "请选择语音程序；未配置时仅普通按键可用。")

    def test_qml_reads_the_controller_identity_instead_of_copying_the_name(self):
        qml_dir = _OVB_ROOT / "src" / "ovb" / "qml"
        main_qml = (qml_dir / "main.qml").read_text(encoding="utf-8")
        self.assertIn("SettingsController.applicationDisplayName", main_qml)
        self.assertIn("SettingsController.applicationPresentationLabel", main_qml)

        voice_qml = (qml_dir / "VoicePage.qml").read_text(encoding="utf-8")
        self.assertNotIn("Remote Mic", voice_qml)

        device_qml = (qml_dir / "DevicePage.qml").read_text(encoding="utf-8")
        self.assertNotIn("Remote Mic", device_qml)

        self.assertIn(
            "title: SettingsController.applicationPresentationLabel", main_qml
        )
        self.assertNotIn('title: "%1 · %2"', main_qml)
        self.assertNotIn(qt_settings_app.__version__, main_qml)
        self.assertNotIn('title: qsTr("%1 设置")', main_qml)

    def test_qt_process_identity_uses_the_shared_display_name(self):
        calls = []

        class FakeApplication:
            def setApplicationName(self, value):
                calls.append(value)

        qt_settings_app._apply_application_identity(FakeApplication())
        self.assertEqual(calls, [product_identity.DISPLAY_NAME])

    def test_element_navigation_keeps_its_standalone_process_identity(self):
        navigation_host = (
            _OVB_ROOT / "src" / "ovb" / "script" / "element_navigation_windows_host.py"
        ).read_text(encoding="utf-8")

        self.assertIn('app.setApplicationName("元素导航")', navigation_host)
        self.assertNotIn("from ovb import product_identity", navigation_host)
        self.assertNotIn("product_identity.DISPLAY_NAME", navigation_host)
        self.assertNotIn("Remote Mic Element Navigation", navigation_host)




if __name__ == "__main__":
    unittest.main()
