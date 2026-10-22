"""Unit tests for the physical-key tri-state observer.

The controller is driven through its edge handlers with fake timing, so no
Windows hook is installed and no key is ever sent to the real desktop.
"""
import time
import unittest
from unittest import mock

from ovb import physical_key_tristate_windows as tristate
from ovb import win32_keys


class FakeSender:
    def __init__(self, fail_down=False):
        self.down = []
        self.up = []
        self.fail_down = fail_down

    def record(self, tokens, key_up):
        if key_up:
            self.up.append(tuple(tokens))
        else:
            self.down.append(tuple(tokens))
            if self.fail_down:
                raise OSError("boom")
        return True


def make_controller(sender, tokens=("lctrl", "lwin", "lalt"), vk=None, **kwargs):
    controller = tristate.PhysicalKeyTriStateController(
        vk if vk is not None else win32_keys.VK_CODES["lwin"],
        tokens,
        logger=mock.Mock(),
        **kwargs,
    )
    return controller


def patch_sender(controller, sender):
    controller._send_down = lambda tokens: (
        False if sender.fail_down else (sender.record(tokens, False) and True)
    )
    controller._send_up = lambda tokens: sender.record(tokens, True)


class TriStateTests(unittest.TestCase):
    def test_the_grace_window_ships_disabled(self):
        self.assertEqual(tristate.GRACE_PERIOD_SECONDS, 0)

    def test_short_tap_never_sends_anything(self):
        sender = FakeSender()
        controller = make_controller(sender)
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        self.assertTrue(controller.pending)
        controller._handle_key_up(win32_keys.VK_CODES["lwin"])
        self.assertFalse(controller.pending)
        self.assertFalse(controller.active)
        self.assertEqual(sender.down, [])
        self.assertEqual(sender.up, [])

    def test_hold_presses_shortcut_without_the_watched_key(self):
        sender = FakeSender()
        controller = make_controller(
            sender, hold_threshold=0.01, grace_period=0.01
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        time.sleep(0.12)
        self.assertTrue(controller.active, "hold should be applied after grace")
        # The operator's own finger holds lwin; only the rest is injected.
        self.assertEqual(sender.down, [("lctrl", "lalt")])

    def test_release_after_hold_releases_exactly_what_was_pressed(self):
        sender = FakeSender()
        controller = make_controller(
            sender, hold_threshold=0.01, grace_period=0.01
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        time.sleep(0.12)
        controller._handle_key_up(win32_keys.VK_CODES["lwin"])
        self.assertFalse(controller.active)
        self.assertEqual(sender.up, [("lctrl", "lalt")])

    def test_other_key_during_grace_cancels_before_any_injection(self):
        sender = FakeSender()
        controller = make_controller(
            sender, hold_threshold=0.01, grace_period=0.5
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        time.sleep(0.08)  # past hold threshold, still inside the grace window
        controller._handle_key_down(win32_keys.VK_CODES["e"])
        self.assertFalse(controller.active)
        self.assertEqual(sender.down, [], "nothing may be injected")
        time.sleep(0.6)  # grace expires later
        self.assertEqual(sender.down, [], "a cancelled hold stays cancelled")
        self.assertFalse(controller.active)

    def test_other_key_before_the_threshold_cancels_without_any_injection(self):
        sender = FakeSender()
        controller = make_controller(
            sender, hold_threshold=0.5, grace_period=0
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        # Still inside the recognition window: the operator changed their mind
        # before the program ever decided it was a hold.
        controller._handle_key_down(win32_keys.VK_CODES["e"])
        self.assertFalse(controller.pending)
        time.sleep(0.6)
        self.assertEqual(sender.down, [], "nothing may be injected")
        self.assertFalse(controller.active)

    def test_zero_grace_injects_without_a_second_timer(self):
        sender = FakeSender()
        controller = make_controller(
            sender, hold_threshold=0.01, grace_period=0
        )
        patch_sender(controller, sender)
        with mock.patch.object(
            tristate.threading, "Timer", wraps=tristate.threading.Timer
        ) as timer_cls:
            controller._handle_key_down(win32_keys.VK_CODES["lwin"])
            time.sleep(0.08)
            self.assertEqual(
                timer_cls.call_count,
                1,
                "with the grace window disabled only the threshold timer runs",
            )
        self.assertTrue(controller.active)
        self.assertEqual(sender.down, [("lctrl", "lalt")])

    def test_other_key_after_hold_releases_injected_keys(self):
        sender = FakeSender()
        controller = make_controller(
            sender, hold_threshold=0.01, grace_period=0.01
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        time.sleep(0.12)
        self.assertTrue(controller.active)
        controller._handle_key_down(win32_keys.VK_CODES["e"])
        self.assertFalse(controller.active)
        self.assertEqual(sender.up, [("lctrl", "lalt")])

    def test_unrelated_key_while_idle_is_ignored(self):
        sender = FakeSender()
        controller = make_controller(sender)
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["e"])
        self.assertFalse(controller.pending)
        self.assertEqual(sender.down, [])

    def test_watched_key_is_never_injected_even_when_only_key(self):
        sender = FakeSender()
        controller = make_controller(
            sender, tokens=("lwin",), hold_threshold=0.01, grace_period=0.01
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        time.sleep(0.12)
        self.assertFalse(controller.active)
        self.assertEqual(sender.down, [])

    def test_shortcut_change_is_picked_up(self):
        sender = FakeSender()
        controller = make_controller(
            sender, hold_threshold=0.01, grace_period=0.01
        )
        patch_sender(controller, sender)
        controller.update_shortcut(("lctrl", "lwin", "lalt", "space"))
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        time.sleep(0.12)
        self.assertEqual(sender.down, [("lctrl", "lalt", "space")])

    def test_failed_injection_does_not_mark_the_hold_active(self):
        sender = FakeSender(fail_down=True)
        controller = make_controller(
            sender, hold_threshold=0.01, grace_period=0.01
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        time.sleep(0.12)
        self.assertFalse(controller.active)
        # A failed press must not leave a later release trying to lift keys
        # that never went down.
        controller._handle_key_up(win32_keys.VK_CODES["lwin"])
        self.assertEqual(sender.up, [])


    def test_without_the_marked_hook_the_hold_sends_nothing(self):
        """Regression: an uninstalled physicalizer used to fail silently.

        ``send_voice_key_combo_down`` raises when the hook is missing, and the
        old ``_send_down`` swallowed it, so the operator saw a dead key.
        """

        sender = FakeSender()
        calls = []

        def refuse():
            calls.append(True)
            return False

        controller = make_controller(
            sender,
            hold_threshold=0.01,
            grace_period=0.01,
            ensure_physicalizer=refuse,
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not controller.active:
            if controller.phase == tristate.PHASE_IDLE:
                break
            time.sleep(0.005)
        self.assertTrue(calls, "the hook readiness probe must run before sending")
        self.assertFalse(controller.active)
        self.assertEqual(sender.down, [])

    def test_hold_reuses_the_remote_voice_path_instead_of_injecting(self):
        """A hold must drive the remote's own voice pipeline.

        Injecting the leftover keys by hand bypasses the input-profile switch
        and Doubao's in-process Frida hook, which is why the provider ignored
        them. When a voice sender is supplied it wins and no keys are
        hand-injected.
        """

        sender = FakeSender()
        pressed = []
        controller = make_controller(
            sender,
            hold_threshold=0.01,
            grace_period=0.01,
            voice_press=lambda tokens: pressed.append(tuple(tokens)) or True,
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        time.sleep(0.12)
        self.assertEqual(pressed, [("lctrl", "lalt")], "voice press must be used")
        self.assertEqual(sender.down, [], "no key may be hand-injected")
        self.assertTrue(controller.active)

    def test_release_reuses_the_remote_voice_path(self):
        sender = FakeSender()
        released = []
        controller = make_controller(
            sender,
            hold_threshold=0.01,
            grace_period=0.01,
            voice_press=lambda tokens: True,
            voice_release=lambda tokens: released.append(tuple(tokens)) or True,
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        time.sleep(0.12)
        self.assertTrue(controller.active)
        controller._handle_key_up(win32_keys.VK_CODES["lwin"])
        self.assertEqual(released, [("lctrl", "lalt")], "voice release must be used")
        self.assertEqual(sender.up, [], "no key may be hand-released")

    def test_hold_omits_the_watched_key_from_the_voice_press(self):
        """The key the operator holds must never be re-injected.

        The watched key is already physically down under the operator's
        finger. Sending it again trips the preflight guard
        (``PhysicalKeyInUseError``) and the whole combo is dropped before a
        single key reaches the provider.
        """

        sender = FakeSender()
        seen = []
        # Watched key "lwin" is also part of the configured shortcut.
        controller = make_controller(
            sender,
            tokens=("lctrl", "lwin", "lalt"),
            vk=win32_keys.VK_CODES["lwin"],
            hold_threshold=0.01,
            grace_period=0.01,
            voice_press=lambda tokens: seen.append(tuple(tokens)) or True,
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        time.sleep(0.12)
        self.assertEqual(seen, [("lctrl", "lalt")])
        self.assertNotIn("lwin", seen[0], "held key must not be re-injected")

    def test_marked_hook_ready_allows_the_hold(self):
        sender = FakeSender()
        controller = make_controller(
            sender,
            hold_threshold=0.01,
            grace_period=0.01,
            ensure_physicalizer=lambda: True,
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not controller.active:
            time.sleep(0.005)
        self.assertTrue(controller.active)
        self.assertEqual(sender.down, [("lctrl", "lalt")])

    def test_release_lifts_the_tokens_that_were_pressed(self):
        """A shortcut change during the hold must not strand keys.

        The release has to lift exactly what went down. Re-reading the
        current shortcut would lift keys that were never pressed and leave
        the pressed ones stuck.
        """

        sender = FakeSender()
        released = []
        controller = make_controller(
            sender,
            hold_threshold=0.01,
            grace_period=0.01,
            voice_press=lambda tokens: True,
            voice_release=lambda tokens: released.append(tuple(tokens)) or True,
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        time.sleep(0.12)
        self.assertTrue(controller.active)
        # The operator re-configures the shortcut while still holding.
        controller.update_shortcut(("lctrl", "lalt", "space"))
        controller._handle_key_up(win32_keys.VK_CODES["lwin"])
        self.assertEqual(released, [("lctrl", "lalt")])

    def test_a_shortcut_of_only_the_watched_key_sends_nothing(self):
        """Nothing is left to send, so the voice path must not be driven."""

        sender = FakeSender()
        pressed = []
        controller = make_controller(
            sender,
            tokens=("lwin",),
            hold_threshold=0.01,
            grace_period=0.01,
            voice_press=lambda tokens: pressed.append(tuple(tokens)) or True,
        )
        patch_sender(controller, sender)
        controller._handle_key_down(win32_keys.VK_CODES["lwin"])
        time.sleep(0.12)
        self.assertFalse(controller.active)
        self.assertEqual(pressed, [], "no key is left to send")
        self.assertEqual(sender.down, [])


class ConfigNormalizationTests(unittest.TestCase):
    def test_single_modifiers_accepted(self):
        from ovb import config

        for token in ("lwin", "rwin", "lctrl", "rctrl", "lalt", "ralt",
                      "lshift", "rshift"):
            with self.subTest(token=token):
                self.assertEqual(
                    config.normalize_voice_physical_key(token), token
                )

    def test_rejected_values_fall_back_to_disabled(self):
        from ovb import config

        for value in ("", "lctrl+lwin", "a", "f1", "space", "vk_ff",
                      "notakey", None, 5, ["lwin"]):
            with self.subTest(value=value):
                self.assertEqual(config.normalize_voice_physical_key(value), "")

    def test_default_config_disables_the_feature(self):
        from ovb import config

        self.assertEqual(config.default_config()["voice_physical_key"], "")

    def test_load_config_normalizes_a_bad_saved_value(self):
        from ovb import config
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            stored = config.default_config()
            stored["voice_physical_key"] = "ctrl+alt"
            config.save_config(path, stored)
            loaded = config.load_config(path)
            self.assertEqual(loaded["voice_physical_key"], "")

            stored["voice_physical_key"] = "lwin"
            config.save_config(path, stored)
            self.assertEqual(config.load_config(path)["voice_physical_key"], "lwin")

    def test_saved_value_survives_a_round_trip(self):
        from ovb import config
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            stored = config.default_config()
            stored["voice_physical_key"] = "rctrl"
            saved = config.save_config_and_load(path, stored)
            self.assertEqual(saved["voice_physical_key"], "rctrl")


if __name__ == "__main__":
    unittest.main()
