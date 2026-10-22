import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ovb import qt_settings_app


_OVB_ROOT = Path(__file__).resolve().parents[1]
_ICON_DIR = _OVB_ROOT / "src" / "ovb" / "assets" / "icons"
_GENERATOR = _OVB_ROOT / "build" / "generate-app-icon.py"


class ApplicationIconTests(unittest.TestCase):
    def test_state_icons_keep_the_existing_brand_blue(self):
        for name in ("remote-mic-connected.svg", "remote-mic-unavailable.svg"):
            content = (_ICON_DIR / name).read_text(encoding="utf-8")
            self.assertIn('stroke="#4AA8FF"', content)
            self.assertNotIn("currentColor", content)

    def test_qt_icon_is_applied_to_the_application_and_native_window(self):
        calls = []

        class FakeIcon:
            def __init__(self, path):
                self.path = path

        class FakeApplication:
            def setWindowIcon(self, icon):
                calls.append(("application", icon.path))

        class FakeWindow:
            def setIcon(self, icon):
                calls.append(("window", icon.path))

        icon_path = _ICON_DIR / "remote-mic-connected.svg"
        qt_settings_app._apply_application_icon(
            FakeApplication(), FakeWindow(), FakeIcon, icon_path
        )

        self.assertEqual(
            calls,
            [
                ("application", str(icon_path)),
                ("window", str(icon_path)),
            ],
        )





if __name__ == "__main__":
    unittest.main()
