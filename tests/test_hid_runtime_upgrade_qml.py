"""Exercise the real device/voice pages with isolated HID upgrade states."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests.test_voice_program_qml import _PROBE as _VOICE_PAGE_PROBE


# Reuse the existing offscreen application setup and its hardware/host stubs.
# Run only setup, before that probe begins its voice-program interactions.
_SETUP = _VOICE_PAGE_PROBE.split('tab_bar = find(window, "tabBar")', 1)[0]
_SETUP = _SETUP.replace(
    "engine = QQmlApplicationEngine()",
    "m.windows_diagnostics.run_diagnostics = lambda **_kwargs: None\n"
    "from pathlib import Path\n"
    "from PySide6.QtGui import QFontDatabase\n"
    "font_path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / 'msyh.ttc'\n"
    "if font_path.is_file():\n"
    "    QFontDatabase.addApplicationFont(str(font_path))\n"
    "engine = QQmlApplicationEngine()",
)
_PROBE = _SETUP + r'''
from ovb import bridge_runtime_status, frida_compat, hid_elevation_windows

find(window, "bridgeStatusRefreshTimer").setProperty("running", False)
tab_bar = find(window, "tabBar")
tab_bar.setProperty("currentIndex", 2)
render(window, app)
wait_for_voice_hotkey_idle(controller, app)
deadline = time.monotonic() + 3.0
while diagnostics.isRefreshing and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(0.01)
assert not diagnostics.isRefreshing
# Only the shipped profile exists, so its HID-upgrade UI is the only one
# DevicePage.qml renders. Stub the bridge as running and drive those states.
controller._config["remote_selection"] = {"schema": 1, "active": "a" * 64,
    "devices": [{"key": "a" * 64, "profile": "chromecast-remote"}]}
controller.remoteSelectionChanged.emit()
assert controller.activeRemoteProfile == "chromecast-remote"
controller._bridge_running = True
controller.bridgeRunningChanged.emit()
controller._set_bridge_connected(True)
controller._set_bridge_connection_state("connected")
device_page = find(window, "deviceScroll").parent()
root_path = m.config.config_root()


def chromecast_state(raw_state):
    bridge_runtime_status.publish_status(
        root_path, bridge_runtime_status.BridgeConnectionState.CONNECTED,
        pid=os.getpid(), raw_input_state=raw_state,
        hid_tap_state=frida_compat.HidTapState.UNAVAILABLE.value,
    )
    controller._set_bridge_input_states(bridge_runtime_status.read_status(root_path))


# The shipped profile ignores the retired profile's tap states entirely: an
# unusable HID tap must not change its receiver verdict.
for tap_state in (frida_compat.HidTapState.RESTART_REQUIRED.value,
                  frida_compat.HidTapState.SHARED_HOST.value,
                  frida_compat.HidTapState.UNHEALTHY.value):
    bridge_runtime_status.publish_status(
        root_path, bridge_runtime_status.BridgeConnectionState.CONNECTED,
        pid=os.getpid(), raw_input_state="chromecast_ready", hid_tap_state=tap_state,
    )
    controller._set_bridge_input_states(bridge_runtime_status.read_status(root_path))
    assert device_page.buttonReceiverStateCode() == "ready", (
        tap_state, device_page.buttonReceiverStateCode())
    assert device_page.buttonReceiverStateText() == "正常"
    assert not device_page.bridgeNeedsRestartAction()

# Setup failures stay actionable and never claim a working receiver.
for raw_state, code in (
    ("chromecast_sensitive_logging_disabled", "setup_failed"),
    ("chromecast_input_payload_unavailable", "input_incomplete"),
    ("chromecast_permission_cancelled", "authorization_cancelled"),
):
    chromecast_state(raw_state)
    assert device_page.buttonReceiverStateCode() == code, (
        raw_state, device_page.buttonReceiverStateCode())

# A confirmed receiver with the service stopped is reported, not silently ready.
chromecast_state("chromecast_ready")
controller._set_bridge_running(False)
assert device_page.buttonReceiverStateCode() == "service_stopped"
controller._set_bridge_running(True)
assert device_page.buttonReceiverStateCode() == "ready"

screenshot_dir = os.environ.get("HID_UPGRADE_SCREENSHOT_DIR")
if screenshot_dir:
    tab_bar.setProperty("currentIndex", 0)
    render(window, app)
    window.grabWindow().save(os.path.join(screenshot_dir, "hid-upgrade-device.png"))
    find(window, "tabBar").setProperty("currentIndex", 2)
    render(window, app)
    window.grabWindow().save(os.path.join(screenshot_dir, "hid-upgrade-voice.png"))
print(json.dumps({"warnings": [str(warning) for warning in warnings]}))
window.hide()
m._shutdown_diagnostics_workers()
'''


class HidRuntimeUpgradeQmlTests(unittest.TestCase):
    def test_upgrade_and_host_reload_states_in_real_pages(self):
        with tempfile.TemporaryDirectory() as local_app_data:
            env = dict(os.environ)
            env["QT_QPA_PLATFORM"] = "offscreen"
            env["LOCALAPPDATA"] = local_app_data
            result = subprocess.run(
                [sys.executable, "-B", "-c", _PROBE],
                env=env, capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(data["warnings"], [])


if __name__ == "__main__":
    unittest.main()
