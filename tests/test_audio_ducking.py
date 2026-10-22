import unittest
from unittest import mock

from ovb import config, system_playback_mute as ducking, voice_controller
from ovb import voice_shortcut_session as shortcut


class _Endpoint:
    def __init__(self, device):
        self._device = device

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    @property
    def muted(self):
        return self._device.muted

    def set_mute(self, muted):
        self._device.calls.append(bool(muted))
        self._device.muted = bool(muted)


class _FakeDevice:
    def __init__(self, muted=False):
        self.muted = muted
        self.calls = []

    def factory(self):
        return lambda: _Endpoint(self)


class _FakeTimer:
    def __init__(self, delay, function):
        self.delay = delay
        self.function = function
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class PlaybackDuckerTests(unittest.TestCase):
    def setUp(self):
        self.timers = []

    def make_ducker(self, device, enabled=True, delay=0.3):
        def timer_factory(delay, function):
            timer = _FakeTimer(delay, function)
            self.timers.append(timer)
            return timer

        return ducking.PlaybackDucker(
            logger=mock.Mock(),
            enabled=lambda: enabled,
            endpoint_factory=device.factory(),
            timer_factory=timer_factory,
            restore_delay_seconds=delay,
        )

    def test_disabled_ducking_never_touches_the_endpoint(self):
        device = _FakeDevice()
        ducker = self.make_ducker(device, enabled=False)
        self.assertFalse(ducker.begin())
        ducker.end()
        self.assertEqual(device.calls, [])
        self.assertFalse(ducker.muted)
        self.assertEqual(self.timers, [])

    def test_enabled_ducking_mutes_then_restores_after_release(self):
        device = _FakeDevice()
        ducker = self.make_ducker(device)
        self.assertTrue(ducker.begin())
        self.assertEqual(device.calls, [True])
        self.assertTrue(ducker.muted)
        ducker.end()
        # The restore waits out the key-up tail instead of racing it.
        self.assertEqual(len(self.timers), 1)
        self.assertTrue(self.timers[0].started)
        self.assertEqual(device.calls, [True])
        # Still owned until the tail expires, so a quick re-press re-uses it.
        self.assertTrue(ducker.muted)
        self.timers[0].function()
        self.assertEqual(device.calls, [True, False])
        self.assertFalse(ducker.muted)

    def test_restore_now_skips_the_wait(self):
        device = _FakeDevice()
        ducker = self.make_ducker(device)
        ducker.begin()
        ducker.end()
        self.assertTrue(self.timers[0].started)
        ducker.restore_now()
        self.assertEqual(device.calls, [True, False])
        self.assertTrue(self.timers[0].cancelled)
        self.assertFalse(ducker.muted)

    def test_hold_starting_during_the_tail_keeps_the_device_muted(self):
        device = _FakeDevice()
        ducker = self.make_ducker(device)
        ducker.begin()
        ducker.end()
        self.assertTrue(self.timers[0].started)
        self.assertTrue(ducker.begin())
        self.assertTrue(self.timers[0].cancelled)
        self.assertEqual(device.calls, [True])
        ducker.restore_now()
        self.assertEqual(device.calls, [True, False])

    def test_a_device_the_user_muted_stays_muted(self):
        device = _FakeDevice(muted=True)
        ducker = self.make_ducker(device)
        self.assertFalse(ducker.begin())
        ducker.end()
        ducker.restore_now()
        self.assertEqual(device.calls, [])
        self.assertFalse(ducker.muted)

    def test_repeated_begin_mutes_once_and_restores_once(self):
        device = _FakeDevice()
        ducker = self.make_ducker(device, delay=0)
        self.assertTrue(ducker.begin())
        self.assertTrue(ducker.begin())
        self.assertEqual(device.calls, [True])
        ducker.end()
        self.assertEqual(device.calls, [True, False])
        ducker.end()
        self.assertEqual(device.calls, [True, False])

    def test_mute_failure_is_reported_and_never_restored_as_ours(self):
        class _Broken:
            def __enter__(self):
                raise OSError("no endpoint")

            def __exit__(self, *_args):
                return False

        ducker = ducking.PlaybackDucker(
            logger=mock.Mock(),
            enabled=lambda: True,
            endpoint_factory=_Broken,
            timer_factory=lambda delay, function: _FakeTimer(delay, function),
        )
        self.assertFalse(ducker.begin())
        self.assertFalse(ducker.muted)
        ducker.end()
        self.assertFalse(ducker.muted)

    def test_module_switch_is_process_wide(self):
        previous = ducking.is_enabled()
        try:
            ducking.set_enabled(True)
            self.assertTrue(ducking.is_enabled())
            ducking.set_enabled(False)
            self.assertFalse(ducking.is_enabled())
        finally:
            ducking.set_enabled(previous)


class ShortcutDuckingTests(unittest.TestCase):
    def make_session(self, delivered=True):
        services = mock.Mock()
        services.configured_backend.return_value = (
            shortcut._VOICE_HOTKEY_BACKEND_WETYPE
        )
        services.duck_playback.return_value = True
        trace = mock.Mock()
        trace.current_context.return_value = {}
        session = shortcut.VoiceShortcutSession(
            hotkey_text="ctrl+win",
            logger=mock.Mock(),
            trace=trace,
            services=services,
        )
        control = mock.Mock()
        control.start.return_value = delivered
        control.stop.return_value = True
        control.current_generation = 3
        control.cleanup_pending = False
        control.completion_pending = False
        session.wetype_control = control
        return session, services

    def test_key_down_ducks_and_key_up_restores(self):
        session, services = self.make_session()
        self.assertTrue(
            session.apply(voice_controller.VoiceHostAction.KEY_DOWN)
        )
        services.duck_playback.assert_called_once_with()
        services.restore_playback.assert_not_called()
        self.assertTrue(session.apply(voice_controller.VoiceHostAction.KEY_UP))
        services.restore_playback.assert_called_once_with()

    def test_undelivered_key_down_does_not_leave_the_device_muted(self):
        session, services = self.make_session(delivered=False)
        self.assertFalse(
            session.apply(voice_controller.VoiceHostAction.KEY_DOWN)
        )
        services.duck_playback.assert_called_once_with()
        services.restore_playback.assert_called_once_with()

    def test_safety_release_restores_the_device(self):
        session, services = self.make_session(delivered=False)
        session.pending_tokens = ("ctrl", "win")
        session.pending_backend = shortcut._VOICE_HOTKEY_BACKEND_WETYPE
        self.assertTrue(session.release_pending())
        services.restore_playback.assert_called_once_with()


class ConfigDefaultTests(unittest.TestCase):
    def test_ducking_defaults_to_off(self):
        self.assertFalse(config.default_config()["audio_ducking_enabled"])


if __name__ == "__main__":
    unittest.main()
