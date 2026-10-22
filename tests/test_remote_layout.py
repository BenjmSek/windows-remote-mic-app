"""Pure data/logic tests for remote_layout.py - no Tk/Qt import
at all, so these run identically whether or not PySide6 is installed.
"""

import unittest

from ovb import device_profile, remote_layout


class ButtonHotspotTableTests(unittest.TestCase):
    def test_exactly_thirteen_hotspots(self):
        # 12 ordinary HID buttons + the fixed mic - never "volume_mute"
        # (see module docstring).
        self.assertEqual(len(remote_layout.BUTTON_HOTSPOTS), 13)

    def test_button_ids_match_the_user_facing_set_exactly(self):
        hotspot_ids = {hotspot.button_id for hotspot in remote_layout.BUTTON_HOTSPOTS}
        expected = device_profile.ALL_BUTTON_IDS - {"volume_mute"}
        self.assertEqual(hotspot_ids, expected)

    def test_no_duplicate_button_ids(self):
        ids = [hotspot.button_id for hotspot in remote_layout.BUTTON_HOTSPOTS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_hotspot_is_within_the_unit_square(self):
        for hotspot in remote_layout.BUTTON_HOTSPOTS:
            self.assertGreaterEqual(hotspot.x, 0.0)
            self.assertGreaterEqual(hotspot.y, 0.0)
            self.assertLessEqual(hotspot.x + hotspot.width, 1.0)
            self.assertLessEqual(hotspot.y + hotspot.height, 1.0)

    def test_exactly_one_hotspot_is_marked_is_voice(self):
        voice_hotspots = [h for h in remote_layout.BUTTON_HOTSPOTS if h.is_voice]
        self.assertEqual(len(voice_hotspots), 1)
        self.assertEqual(voice_hotspots[0].button_id, "mic")

    def test_mic_hotspot_uses_the_calibrated_voice_coordinates(self):
        # The fixed microphone hotspot is part of the Windows layout contract.
        mic = remote_layout.hotspot_for("mic")
        self.assertEqual((mic.x, mic.y, mic.width, mic.height), (0.73, 0.355, 0.28, 0.090))

    def test_hotspot_for_unknown_button_returns_none(self):
        self.assertIsNone(remote_layout.hotspot_for("does_not_exist"))

    def test_button_order_matches_hotspot_table_order(self):
        self.assertEqual(
            remote_layout.BUTTON_ORDER,
            tuple(hotspot.button_id for hotspot in remote_layout.BUTTON_HOTSPOTS),
        )


class ButtonDisplayNameTests(unittest.TestCase):
    def test_every_hotspot_button_has_a_chinese_display_name(self):
        for hotspot in remote_layout.BUTTON_HOTSPOTS:
            self.assertIn(hotspot.button_id, remote_layout.BUTTON_DISPLAY_NAMES)
            name = remote_layout.BUTTON_DISPLAY_NAMES[hotspot.button_id]
            self.assertTrue(name)

    def test_mic_display_name_says_mic_not_a_generic_button(self):
        self.assertIn("麦克风", remote_layout.BUTTON_DISPLAY_NAMES["mic"])


from ovb import remote_layout

# The shipped Chromecast remote never sends keyboard-page usages: its buttons
# arrive as one-byte vendor report codes, so that is what the column shows.
# The retired profile's 0xXXXX keyboard-page table was removed with it.


class HidUsageDisplayTests(unittest.TestCase):
    def test_ordinary_button_shows_its_vendor_report_code(self):
        code = remote_layout.CHROMECAST_REPORT_CODES["ok"]
        self.assertEqual(
            remote_layout.hid_usage_display("ok"), f"普通报告 0x{code:02X}"
        )

    def test_power_button_shows_its_vendor_report_code(self):
        code = remote_layout.CHROMECAST_REPORT_CODES["power"]
        self.assertEqual(
            remote_layout.hid_usage_display("power"), f"普通报告 0x{code:02X}"
        )

    def test_mic_has_no_vendor_report_and_says_so(self):
        self.assertNotIn("mic", remote_layout.CHROMECAST_REPORT_CODES)
        self.assertEqual(
            remote_layout.hid_usage_display("mic"), "ATVV 语音控制（非普通报告）"
        )

    def test_every_button_with_a_report_code_shows_it(self):
        for button_id, code in remote_layout.CHROMECAST_REPORT_CODES.items():
            self.assertEqual(
                remote_layout.hid_usage_display(button_id), f"普通报告 0x{code:02X}"
            )


if __name__ == "__main__":
    unittest.main()
