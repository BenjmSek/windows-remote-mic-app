"""Volume/mute keys must bypass the physical-key preflight guard.

The guard in ``send_key_combo_tap`` refuses to inject a key that the OS
already reports as physically held.  That protects ordinary keys from
double-firing, but it mislabels the remote's own Consumer HID volume event
as a held key and would block the very injection that makes the system
volume change.  ``send_volume_up/down/mute`` therefore skip the guard.
"""

import unittest
from unittest import mock

from ovb import win32_input


class _FakeSender:
    """Captures the events handed to SendInput without touching the system."""

    def __init__(self):
        self.events = []

    def __call__(self, events):
        self.events.extend(events)
        return len(events)


class VolumeGuardTests(unittest.TestCase):
    def test_held_volume_key_is_blocked_without_skip(self):
        sender = _FakeSender()
        query = lambda vk: True  # every key reported as physically held
        with self.assertRaises(win32_input.PhysicalKeyInUseError):
            win32_input.send_key_combo_tap(
                ("volume_up",), _sender=sender, _key_down_query=query
            )
        self.assertFalse(sender.events)  # nothing was sent

    def test_skip_guard_delivers_volume_while_held(self):
        sender = _FakeSender()
        query = lambda vk: True
        win32_input.send_key_combo_tap(
            ("volume_up",),
            _sender=sender,
            _key_down_query=query,
            _skip_physical_guard=True,
        )
        self.assertTrue(sender.events)
        vk = win32_input.win32_keys.VK_CODES["volume_up"]
        self.assertEqual([v for v, _up in sender.events], [vk, vk])

    def test_send_volume_up_forwards_skip_flag(self):
        captured = {}
        with mock.patch.object(
            win32_input, "send_key_combo_tap",
            side_effect=lambda *a, **k: captured.update(k),
        ):
            win32_input.send_volume_up(_sender=_FakeSender())
        self.assertTrue(captured.get("_skip_physical_guard"))

    def test_send_volume_down_forwards_skip_flag(self):
        captured = {}
        with mock.patch.object(
            win32_input, "send_key_combo_tap",
            side_effect=lambda *a, **k: captured.update(k),
        ):
            win32_input.send_volume_down(_sender=_FakeSender())
        self.assertTrue(captured.get("_skip_physical_guard"))

    def test_send_volume_mute_forwards_skip_flag(self):
        captured = {}
        with mock.patch.object(
            win32_input, "send_key_combo_tap",
            side_effect=lambda *a, **k: captured.update(k),
        ):
            win32_input.send_volume_mute(_sender=_FakeSender())
        self.assertTrue(captured.get("_skip_physical_guard"))

    def test_ordinary_key_still_blocked_when_held(self):
        # Regression guard: a non-volume key must keep the protection.
        sender = _FakeSender()
        query = lambda vk: True
        with self.assertRaises(win32_input.PhysicalKeyInUseError):
            win32_input.send_key_combo_tap(
                ("enter",), _sender=sender, _key_down_query=query
            )


if __name__ == "__main__":
    unittest.main()
