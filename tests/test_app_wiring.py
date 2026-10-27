"""App-wiring/thread-safety tests for app.py's ChromecastApp.

``ChromecastApp.__init__`` is safe to construct off Windows: config/hotkey/
voice-controller/supervisor setup is pure Python, and the real Win32/WinRT
calls only happen inside ``_connect_once()``/the HID listener, which these
tests never call. Constructing a real ``ChromecastApp`` and substituting its
BLE-session/playback collaborators with lightweight recorders lets these
tests exercise the actual wiring DECISIONS app.py makes - host hotkey
failure suppresses MIC_OPEN, playback write failure fails closed and
requests a reconnect, and that request happens correctly from a real
worker thread - without any Windows API, matching this project's existing
"test contracts, not implementation-mirroring fakes" approach.

The host-hotkey-unavailable case doesn't even need mocking: off Windows,
win32_input.py's ``_require_windows()`` genuinely raises
``Win32InputUnavailableError`` on every call, so it exercises the exact
"hotkey failed to deliver" branch app.py must fail closed on - not a stand-
in for it.
"""

import asyncio
from contextlib import ExitStack
import json
from dataclasses import replace
import logging
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ovb import app as app_module
from ovb import (
    bridge_runtime_status,
    config,
    frida_compat,
    hid_identity,
    key_detection_bridge,
    key_mapping,
    logging_setup,
    raw_input_windows,
    settings_ui,
    voice_program_manager,
    win32_input,
    win32_keys,
)
from ovb.atvv_session import AudioStarted, AudioStopped, MicButtonPressed

DEFAULT_VOICE_TOKENS = ("ralt",)
WETYPE_VOICE_TOKENS = ("lctrl", "lshift", "f9")


def _run(coro):
    # Explicitly closing the loop (a ResourceWarning for an unclosed
    # test event loop) rather than letting
    # it be garbage-collected.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeBleSession:
    def __init__(self, close_raises=False):
        self.mic_open_calls = 0
        self.mic_close_calls = 0
        self.close_raises = close_raises
        self.close_calls = 0
        self.audio_arrival_watermark = -1

    def send_mic_open_threadsafe(self):
        self.mic_open_calls += 1

    def send_mic_close_threadsafe(self):
        self.mic_close_calls += 1

    async def close(self):
        self.close_calls += 1
        if self.close_raises:
            raise RuntimeError("simulated BLE worker thread that did not stop")


class _FakeHidListener:
    def __init__(self, stop_raises=False):
        self.stop_raises = stop_raises
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        if self.stop_raises:
            raise RuntimeError("simulated Raw Input listener thread that did not stop")


class _FakeInputOwner:
    def __init__(self, stop_raises=False):
        self.stop_raises = stop_raises
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1
        if self.stop_raises:
            raise RuntimeError("simulated input owner that did not stop")


class _FakeVoicePhysicalizer:
    def __init__(
        self,
        *,
        start_error=None,
        immediate_exit=False,
        accepts_new_down=True,
        stop_error=None,
    ):
        self.start_error = start_error
        self.immediate_exit = immediate_exit
        self.is_running = False
        self.accepts_new_down = accepts_new_down
        self.stop_error = stop_error
        self.start_calls = 0
        self.stop_calls = 0
        self.callback = None
        self.health_callback = None
        self.tracker_generation = 1
        self.installation_epoch = 1

    def set_tracking_lost_callback(self, callback):
        self.callback = callback

    def set_health_failure_callback(self, callback):
        self.health_callback = callback

    def start(self):
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        self.is_running = not self.immediate_exit
        self.accepts_new_down = self.is_running

    def stop(self):
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        self.is_running = False
        self.accepts_new_down = False

    def emit_loss(self):
        if self.callback is not None:
            self.callback()


class _FakeChromecastVoiceHost:
    """Minimal receiver-side voice owner.

    Only ``cancel_recording`` is defined, so the caller's preferred
    ``cancel_recording_and_wait`` seam is absent and it must fall back.
    """

    def __init__(self):
        self.closed = False
        self.cancel_calls = 0

    def cancel_recording(self):
        self.cancel_calls += 1


class _ManualTimer:
    def __init__(self, callback):
        self.callback = callback
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


class _FakePlaybackSink:
    def __init__(self, fail_write=False, close_raises=False):
        self.fail_write = fail_write
        self.close_raises = close_raises
        self.write_calls = []
        self.closed = False
        self.close_calls = 0

    def write(self, samples):
        self.write_calls.append(samples)
        if self.fail_write:
            raise OSError("simulated PortAudio write failure")

    def close(self):
        self.close_calls += 1
        if self.close_raises:
            raise RuntimeError("simulated PortAudio stream that did not close")
        self.closed = True


class _FakeHidListenerForFailedStart:
    """A fake standing in for
    RawInputButtonListener itself (not just its ``start()`` outcome), so
    ``_start_hid_listener()`` can be exercised end-to-end off Windows -
    ``is_running`` is the source of truth a failed ``start()`` must consult
    before deciding whether to keep or discard the owner reference.
    """

    def __init__(self, is_running_after_failed_start):
        self._is_running_after_failed_start = is_running_after_failed_start
        self.start_calls = 0

    @property
    def is_running(self):
        return self._is_running_after_failed_start

    def start(self, device_path):
        self.start_calls += 1
        raise app_module.raw_input_windows.RawInputUnavailableError("simulated failed start")

    def stop(self):
        pass


class _FakeRecoveringRawListener:
    def __init__(
        self,
        *,
        start_error=None,
        running_after_start=True,
        running_after_error=False,
        stop_error=None,
    ):
        self.start_error = start_error
        self.running_after_start = running_after_start
        self.running_after_error = running_after_error
        self.stop_error = stop_error
        self.is_running = False
        self.start_calls = 0
        self.stop_calls = 0
        self.sourced_callback = None
        self.raw_callback = None
        self.removed_callback = None
        self.corruption_callback = None
        self.tracking_lost_callback = None
        self.physical_bindings = None

    def set_sourced_button_event_callback(self, callback):
        self.sourced_callback = callback

    def set_raw_event_callback(self, callback):
        self.raw_callback = callback

    def set_device_removed_callback(self, callback):
        self.removed_callback = callback

    def set_input_corruption_callback(self, callback):
        self.corruption_callback = callback

    def set_physical_keyboard_tracking_lost_callback(self, callback):
        self.tracking_lost_callback = callback

    def set_physical_bindings(self, bindings):
        self.physical_bindings = dict(bindings)

    def start(self, _device_path):
        self.start_calls += 1
        if self.start_error is not None:
            self.is_running = self.running_after_error
            raise self.start_error
        self.is_running = self.running_after_start

    def stop(self):
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        self.is_running = False


def _build_app(tmp_root: Path) -> "app_module.ChromecastApp":
    # Redirect config_root (and therefore logging_setup's log directory) at
    # a throwaway temp directory instead of the real machine's config/log
    # location - ChromecastApp.__init__ always calls config.config_root()/
    # logging_setup.get_logger(), neither of which touch any Windows API.
    original = config.config_root
    config.config_root = lambda: tmp_root
    try:
        return app_module.ChromecastApp()
    finally:
        config.config_root = original


def _build_app_with_owned_loop(tmp_root: Path):
    """Like _build_app(), but explicitly creates a fresh event loop and sets
    it as this thread's current loop before constructing the app.

    ChromecastApp.__init__ builds a ChromecastRuntime, whose __init__ captures
    ``loop or asyncio.get_event_loop()`` (google_runtime.py) - called
    here synchronously, off any running loop. Without an owned loop already
    set, that would silently create-and-cache this thread's implicit default
    loop the first time any test builds an ChromecastApp - a loop nothing then
    ever closes (see EventLoopOwnershipRegressionTests for the exact red
    evidence this reproduces and fixes). Returns ``(app, loop)``; the caller
    owns ``loop`` and must ``asyncio.set_event_loop(None)`` then
    ``loop.close()`` it when done - exactly mirroring the real app's own
    ``asyncio.run(_run())`` construction, which owns and closes its loop too.
    """

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = _build_app(tmp_root)
    return app, loop


class _AppWiringTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Red evidence (real Windows run 29644660267): 425 tests
        # passed, then the process printed an ignored "unclosed event loop"
        # ResourceWarning for a ProactorEventLoop plus two unclosed self-pipe
        # sockets - AFTER unittest's own summary, so -W error::ResourceWarning
        # never sees it and the step still exits 0 (a ResourceWarning-turned-
        # exception raised inside a __del__/finalizer is unraisable; Python
        # can only print it via sys.unraisablehook, never let it change an
        # already-computed exit code - see EventLoopOwnershipRegressionTests
        # below for a deterministic, isolated-subprocess reproduction).
        # _build_app_with_owned_loop() above threads a per-test owned loop
        # into ChromecastRuntime instead - never the ambient, never-closed
        # default the old bare _build_app() call left behind.
        self.app, self._loop = _build_app_with_owned_loop(Path(self._tmp.name))
        # Voice wiring tests need an explicitly configured provider, not the
        # retired none mode. This empty fixture is never launched.
        voice_exe = Path(self._tmp.name) / "test-voice.exe"
        voice_exe.touch()
        self.app._config["voice_program"] = voice_program_manager.normalize_voice_program_settings(
            {"provider": "custom", "custom_executable": str(voice_exe),
             "launch_on_bridge_start": False})
        # This wiring harness represents an explicitly selected device. PnP
        # identity isolation is tested separately in test_remote_selection.
        self.app._selected_remote_key = "a" * 64
        self.app._remote_profile = app_module.remote_selection.CHROMECAST_PROFILE
        selection_patch = mock.patch(
            "ovb.remote_selection.selected_raw_path",
            side_effect=lambda paths, _key, **_kwargs: hid_identity.select_single_device_path(paths),
        )
        selection_patch.start()
        self.addCleanup(selection_patch.stop)
        self.app._voice_audio.sink = _FakePlaybackSink()
        self.app._ble_session = _FakeBleSession()
        self.app._accept_ble_events = True
        # Most wiring tests exercise the product's mapped-button path. That
        # path is available only after the privileged HID endpoint confirms
        # that it intercepted a real report before Windows saw it.
        self.app._direct_hid_interception_ready = True
        # The real application starts both physical-input trackers before it
        # accepts mapped button events. These unit tests construct ChromecastApp
        # directly, so establish the same ready state explicitly.
        raw_input_windows._set_physical_keyboard_tracker_active(True)
        self.app._voice_key_physicalizer_ready = True
        self.app._voice_shortcut.doubao_physicalizer = mock.Mock()
        self.app._voice_shortcut.doubao_physicalizer.start.return_value = True
        self.app._voice_shortcut.doubao_physicalizer.status = "active"
        self.app._voice_shortcut.doubao_physicalizer.error = None
        self.app._voice_shortcut.doubao_physicalizer.generation = 1
        self.app._voice_shortcut.doubao_physicalizer.target_pid = 123
        self.app._voice_shortcut.doubao_physicalizer.is_active_generation.return_value = True
        class ReadyDoubaoCapture:
            identity = None
            status = "waiting"

            def begin(inner_self):
                return None

            def poll(inner_self, _now):
                inner_self.identity = "test-capture"
                inner_self.status = "tracking"
                return False

        self.production_doubao_capture_watch_factory = self.app._doubao_capture_watch_factory
        self.app._doubao_capture_watch_factory = ReadyDoubaoCapture
        self.app._raw_windows_key_down_query = lambda _vk_code: False
        self._button_input_release_timers = []

        def button_input_release_timer_factory(delay, callback):
            timer = _ManualTimer(callback)
            timer.delay = delay
            self._button_input_release_timers.append(timer)
            return timer

        self.app._button_input_release_timer_factory = (
            button_input_release_timer_factory
        )
        self._voice_hotkey_release_timers = []

        def voice_hotkey_release_timer_factory(delay, callback):
            timer = _ManualTimer(callback)
            timer.delay = delay
            self._voice_hotkey_release_timers.append(timer)
            return timer

        self.app._voice_shortcut.timer_factory = (
            voice_hotkey_release_timer_factory
        )

    def tearDown(self):
        with self.app._button_action_lock:
            self.app._button_input_release_retry_stopping = True
            self.app._cancel_button_input_release_retry_locked(reset_delay=False)
        with self.app._voice_shortcut.lock:
            self.app._cancel_voice_hold_watchdog_locked()
            self.app._voice_shortcut.retry_stopping = True
            self.app._voice_shortcut.cancel_release_retry(reset_delay=False)
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._voice_key_physicalizer_stopping = True
            self.app._cancel_voice_key_physicalizer_retry_locked()
        with self.app._raw_input_lifecycle_lock:
            self.app._raw_input_stopping = True
            self.app._cancel_raw_input_retry_locked()
        app_module.voice_key_physicalizer_windows._set_physical_tracker_active(
            False
        )
        raw_input_windows._set_physical_keyboard_tracker_active(False)
        playback_writer = self.app._voice_audio.writer
        if playback_writer is not None:
            playback_writer.flush(1.0)
            playback_writer.stop(1.0)
            self.app._voice_audio.writer = None
        # logging_setup.get_logger() configures its FileHandler
        # exactly once per process (module-global ``_configured``) and never
        # closes it - correct for a real long-running app, but in this suite
        # it leaves an open handle inside THIS test's temp directory. Windows
        # (unlike POSIX, where you can unlink a file while a handle is still
        # open on it) refuses to delete a directory containing an open file
        # handle, so ``self._tmp.cleanup()`` below would raise on Windows
        # once any prior test in this class had already configured the
        # logger. Close/remove the handler and reset the one-time-config
        # flag first so every test starts and ends with no logging state
        # leaked into the next one.
        logger = logging.getLogger(logging_setup.LOGGER_NAME)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        logging_setup._configured = False
        self._tmp.cleanup()
        # Close the loop this test owns (see setUp()) and detach
        # it as the thread's current loop, so its own eventual __del__ finds
        # is_closed() already True and stays silent - and so the NEXT test's
        # setUp() cannot mistake this now-closed loop for a live ambient one.
        asyncio.set_event_loop(None)
        self._loop.close()

    def _drain_event_loop(self):
        self._loop.run_until_complete(asyncio.sleep(0))

    def _flush_playback(self):
        writer = self.app._voice_audio.writer
        self.assertIsNotNone(writer)
        return writer.flush(1.0)

    def _save_voice_settings(self, *, mode: str, hotkey_text: str) -> None:
        refreshed = config.load_config(self.app._config_path)
        refreshed["voice_program"] = dict(self.app._config["voice_program"])
        refreshed["voice_trigger_mode"] = mode
        refreshed["voice_hotkey"] = hotkey_text
        config.save_config(self.app._config_path, refreshed)

    def _set_voice_mapping(
        self,
        button_id: str,
        mode: key_mapping.VoiceTriggerMode,
    ) -> None:
        bindings = self.app._bindings["bindings"]
        for existing_button, raw_action in list(bindings.items()):
            try:
                action = key_mapping.ButtonAction.from_dict(raw_action)
            except (KeyError, TypeError, ValueError):
                continue
            if key_mapping.is_voice_action(action):
                bindings[existing_button] = key_mapping.ButtonAction(
                    key_mapping.ActionKind.DISABLED
                ).to_dict()
        bindings[button_id] = key_mapping.voice_action_for_trigger_mode(mode).to_dict()


class StartupIdentityLoggingTests(unittest.TestCase):
    def test_frozen_startup_logs_version_runtime_and_package_directory(self):
        logger = mock.Mock(spec=logging.Logger)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                package_dir = Path(tmp) / "RemoteMicChromecast-localtest"
                executable = package_dir / "无线麦Mod win版 1.0.51-candidate.51.exe"
                with mock.patch.object(
                    logging_setup, "get_logger", return_value=logger
                ), mock.patch.object(
                    bridge_runtime_status.sys, "frozen", True, create=True
                ), mock.patch.object(
                    bridge_runtime_status.sys, "executable", str(executable)
                ):
                    _build_app(Path(tmp))
        finally:
            asyncio.set_event_loop(None)
            loop.close()

        logger.info.assert_any_call(
            "startup: app identity: version=%s runtime=%s package=%s",
            app_module.__version__,
            "frozen",
            package_dir.name,
        )


class LiveSettingsReloadTests(_AppWiringTestCase):
    def test_chromecast_attempt_claim_defers_provider_and_hotkey_reload(self):
        host = mock.Mock(_settings_claimed=True)
        self.app._chromecast_runtime.voice_host = host
        refreshed = config.load_config(self.app._config_path)
        refreshed["voice_program"] = (
            voice_program_manager.normalize_voice_program_settings(
                {"provider": voice_program_manager.VOICE_PROGRAM_DOUBAO_IME}
            )
        )
        config.set_voice_hotkey_for_provider(
            refreshed,
            voice_program_manager.VOICE_PROGRAM_DOUBAO_IME,
            "ralt+space",
        )
        config.save_config(self.app._config_path, refreshed)

        worker = threading.Thread(target=self.app._reload_settings_if_changed)
        worker.start()
        worker.join(1.0)

        self.assertFalse(worker.is_alive())
        self.assertNotEqual(
            self.app._config["voice_program"]["provider"],
            voice_program_manager.VOICE_PROGRAM_DOUBAO_IME,
        )
        self.assertIsNotNone(self.app._pending_config)

        host._settings_claimed = False
        with self.app._voice_shortcut.lock:
            self.app._apply_pending_voice_settings_if_idle_locked()

        self.assertEqual(
            self.app._config["voice_program"]["provider"],
            voice_program_manager.VOICE_PROGRAM_DOUBAO_IME,
        )
        self.assertEqual(self.app._voice_shortcut.hotkey.serialize(), "ralt+space")

    def test_reload_never_applies_another_entity_settings(self):
        from ovb import remote_selection
        original = {"schema": 1, "active": "a" * 64, "devices": [
            {"key": "a" * 64, "profile": "chromecast-remote"},
            {"key": "b" * 64, "profile": "chromecast-remote"}]}
        self.app._config[remote_selection.KEY] = original
        incoming = dict(self.app._config)
        incoming[remote_selection.KEY] = dict(original, active="b" * 64)
        config.save_config(self.app._config_path, incoming)
        old_mapping = self.app._bindings
        self.app._reload_settings_if_changed()
        self.assertEqual(remote_selection.active_key(self.app._config), "a" * 64)
        self.assertIs(self.app._bindings, old_mapping)
        self.assertIsNone(self.app._pending_config)

    def _use_manual_gesture_timers(self):
        timers = []

        def timer_factory(_delay, callback):
            timer = _ManualTimer(callback)
            timers.append(timer)
            return timer

        self.app._button_gestures._timer_factory = timer_factory
        return timers

    def _save_button_bindings(self, bindings):
        config.save_key_bindings(self.app._bindings_path, bindings)
        self.app._reload_settings_if_changed()

    def test_zero_voice_blank_shortcuts_construct_without_crashing(self):
        stored_config = config.default_config()
        stored_config["voice_hotkey"] = ""
        stored_config["voice_hotkeys"] = {"toggle": "", "hold": ""}
        config.save_config(self.app._config_path, stored_config)
        stored_bindings = config.default_key_bindings()
        stored_bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        config.save_key_bindings(self.app._bindings_path, stored_bindings)

        reconstructed = _build_app(self.app._config_root)

        self.assertEqual(reconstructed._config["voice_hotkey"], "ralt")
        self.assertEqual(reconstructed._configured_voice_buttons(), [])
        self.assertEqual(reconstructed._voice_shortcut.hotkey.serialize(), "ralt")

    def test_voice_mode_and_hotkey_reload_while_idle(self):
        self._save_voice_settings(mode="hold", hotkey_text="ctrl+l")

        self.app._reload_settings_if_changed()

        self.assertEqual(self.app._voice_shortcut.controller.trigger_mode, key_mapping.VoiceTriggerMode.HOLD)
        self.assertEqual(self.app._voice_shortcut.hotkey.serialize(), "ctrl+l")
        self.assertIsNone(self.app._pending_voice_settings)

    def test_doubao_provider_refresh_is_used_by_the_next_voice_action(self):
        refreshed = config.load_config(self.app._config_path)
        refreshed["voice_program"] = (
            voice_program_manager.normalize_voice_program_settings(
                {"provider": voice_program_manager.VOICE_PROGRAM_DOUBAO_IME}
            )
        )
        config.set_voice_hotkey_for_provider(refreshed, "doubao_ime", "ralt")
        config.save_config(self.app._config_path, refreshed)
        control = mock.Mock()
        control.start.return_value = True
        control.prepare.return_value = object()
        control.dispatch_prepared.return_value = True
        control.stop.return_value = True
        self.app._voice_shortcut.doubao_control = control

        self.app._reload_settings_if_changed()
        delivered = self.app._voice_shortcut.apply(
            app_module.voice_controller.VoiceHostAction.KEY_DOWN
        )

        self.assertTrue(delivered)
        self.assertEqual(
            self.app._configured_voice_hotkey_backend(),
            app_module._VOICE_HOTKEY_BACKEND_DOUBAO,
        )
        control.start.assert_called_once_with(("ralt",), activate_profile=True)
        self.app._voice_shortcut.doubao_physicalizer.start.assert_called_once_with((0xA5,))
        self.assertTrue(
            self.app._voice_shortcut.apply(
                app_module.voice_controller.VoiceHostAction.KEY_UP
            )
        )
        self.app._voice_shortcut.doubao_physicalizer.expect_markers.assert_has_calls(
            [mock.call("down", 1), mock.call("up", 1)]
        )

    def test_voice_settings_reload_is_deferred_until_active_hold_releases(self):
        self.app._voice_shortcut.controller.on_mic_button_pressed()
        self._save_voice_settings(mode="hold", hotkey_text="ctrl+l")

        self.app._reload_settings_if_changed()

        self.assertEqual(self.app._voice_shortcut.controller.trigger_mode, key_mapping.VoiceTriggerMode.HOLD)
        self.assertEqual(self.app._voice_shortcut.hotkey.serialize(), "ralt")
        self.assertIsNotNone(self.app._pending_voice_settings)

        self.app._voice_shortcut.controller.on_mic_button_released()
        with self.app._voice_shortcut.lock:
            self.app._apply_pending_voice_settings_if_idle_locked()

        self.assertEqual(self.app._voice_shortcut.controller.trigger_mode, key_mapping.VoiceTriggerMode.HOLD)
        self.assertEqual(self.app._voice_shortcut.hotkey.serialize(), "ctrl+l")
        self.assertIsNone(self.app._pending_voice_settings)

    def test_invalid_voice_settings_keep_the_last_valid_runtime_values(self):
        refreshed = config.load_config(self.app._config_path)
        refreshed["voice_hotkey"] = "ctrl"
        config.save_config(self.app._config_path, refreshed)

        self.app._reload_settings_if_changed()

        self.assertEqual(self.app._voice_shortcut.controller.trigger_mode, key_mapping.VoiceTriggerMode.HOLD)
        self.assertEqual(self.app._voice_shortcut.hotkey.serialize(), "ralt")
        self.assertIsNone(self.app._pending_voice_settings)

    def test_delayed_single_click_keeps_the_mapping_owned_at_first_press(self):
        timers = self._use_manual_gesture_timers()
        original = config.default_key_bindings()
        original["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        original["secondary_bindings"]["up"] = {
            "double_click": key_mapping.ButtonAction(
                key_mapping.ActionKind.RETURN
            ).to_dict()
        }
        self._save_button_bindings(original)

        self.app._on_button_event("up", True, event_source="hid")
        self.app._on_button_event("up", False, event_source="hid")
        changed = config.load_key_bindings(self.app._bindings_path)
        changed["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.MOUSE_LEFT_CLICK
        ).to_dict()
        self._save_button_bindings(changed)
        self.assertIsNotNone(self.app._pending_bindings)

        with mock.patch.object(win32_input, "send_escape") as old_action, mock.patch.object(
            win32_input, "send_mouse_button_click"
        ) as new_action:
            timers[0].fire()

        old_action.assert_called_once_with()
        new_action.assert_not_called()
        self.assertIsNone(self.app._pending_bindings)
        self.assertEqual(
            self.app._bindings["bindings"]["up"]["kind"],
            key_mapping.ActionKind.MOUSE_LEFT_CLICK.value,
        )

    def test_delayed_callback_reservation_blocks_a_concurrent_mapping_swap(self):
        timers = self._use_manual_gesture_timers()
        original = config.default_key_bindings()
        original["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        original["secondary_bindings"]["up"] = {
            "double_click": key_mapping.ButtonAction(
                key_mapping.ActionKind.RETURN
            ).to_dict()
        }
        self._save_button_bindings(original)
        self.app._on_button_event("up", True, event_source="hid")
        self.app._on_button_event("up", False, event_source="hid")

        changed = config.load_key_bindings(self.app._bindings_path)
        changed["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.MOUSE_LEFT_CLICK
        ).to_dict()
        config.save_key_bindings(self.app._bindings_path, changed)

        entered = threading.Event()
        release = threading.Event()
        original_trigger = self.app._button_gestures._on_trigger

        def blocking_trigger(button_id, trigger):
            entered.set()
            release.wait(1.0)
            original_trigger(button_id, trigger)

        self.app._button_gestures._on_trigger = blocking_trigger
        with mock.patch.object(win32_input, "send_escape") as old_action, mock.patch.object(
            win32_input, "send_mouse_button_click"
        ) as new_action:
            worker = threading.Thread(target=timers[0].fire)
            worker.start()
            self.assertTrue(entered.wait(1.0))

            self.app._reload_settings_if_changed()
            self.assertIsNotNone(self.app._pending_bindings)
            release.set()
            worker.join(1.0)

        self.assertFalse(worker.is_alive())
        old_action.assert_called_once_with()
        new_action.assert_not_called()
        self.assertIsNone(self.app._pending_bindings)

    def test_mapping_swap_serializes_with_a_new_physical_gesture(self):
        timers = self._use_manual_gesture_timers()
        original = config.default_key_bindings()
        original["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        original["secondary_bindings"]["up"] = {
            "double_click": key_mapping.ButtonAction(
                key_mapping.ActionKind.RETURN
            ).to_dict()
        }
        self._save_button_bindings(original)

        changed = config.load_key_bindings(self.app._bindings_path)
        changed["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.MOUSE_LEFT_CLICK
        ).to_dict()
        config.save_key_bindings(self.app._bindings_path, changed)

        original_reload = self.app._reload_settings_if_changed
        input_passed_reload = threading.Event()
        allow_input_dispatch = threading.Event()
        input_dispatching = threading.Event()
        reload_checked_idle = threading.Event()
        allow_mapping_swap = threading.Event()

        def routed_reload():
            if threading.current_thread().name == "mapping-input":
                input_passed_reload.set()
                allow_input_dispatch.wait(1.0)
                input_dispatching.set()
                return
            original_reload()

        original_idle = self.app._ordinary_button_mappings_idle

        def pause_after_idle_check():
            result = original_idle()
            reload_checked_idle.set()
            allow_mapping_swap.wait(1.0)
            return result

        with mock.patch.object(
            self.app, "_reload_settings_if_changed", side_effect=routed_reload
        ), mock.patch.object(
            self.app,
            "_ordinary_button_mappings_idle",
            side_effect=pause_after_idle_check,
        ):
            input_worker = threading.Thread(
                target=self.app._on_button_event,
                args=("up", True),
                kwargs={"event_source": "hid"},
                name="mapping-input",
            )
            input_worker.start()
            self.assertTrue(input_passed_reload.wait(1.0))

            reload_worker = threading.Thread(target=original_reload)
            reload_worker.start()
            self.assertTrue(reload_checked_idle.wait(1.0))
            allow_input_dispatch.set()
            self.assertTrue(input_dispatching.wait(1.0))
            self.assertTrue(input_worker.is_alive())

            allow_mapping_swap.set()
            reload_worker.join(1.0)
            input_worker.join(1.0)

        self.assertFalse(reload_worker.is_alive())
        self.assertFalse(input_worker.is_alive())
        self.app._on_button_event("up", False, event_source="hid")
        with mock.patch.object(win32_input, "send_mouse_button_click") as new_action:
            timers[0].fire()
        new_action.assert_called_once_with("left")

    def test_long_press_keeps_old_mapping_until_physical_release(self):
        timers = self._use_manual_gesture_timers()
        original = config.default_key_bindings()
        original["secondary_bindings"]["ok"] = {
            "long_press": key_mapping.ButtonAction(
                key_mapping.ActionKind.ESCAPE
            ).to_dict()
        }
        self._save_button_bindings(original)

        self.app._on_button_event("ok", True, event_source="hid")
        changed = config.load_key_bindings(self.app._bindings_path)
        changed["secondary_bindings"]["ok"]["long_press"] = (
            key_mapping.ButtonAction(
                key_mapping.ActionKind.MOUSE_LEFT_CLICK
            ).to_dict()
        )
        self._save_button_bindings(changed)

        with mock.patch.object(win32_input, "send_escape") as old_action, mock.patch.object(
            win32_input, "send_mouse_button_click"
        ) as new_action:
            timers[0].fire()
            self.assertIsNotNone(self.app._pending_bindings)
            self.app._on_button_event("ok", False, event_source="hid")

        old_action.assert_called_once_with()
        new_action.assert_not_called()
        self.assertIsNone(self.app._pending_bindings)

    def test_repeat_keeps_old_mapping_for_the_whole_hold(self):
        timers = self._use_manual_gesture_timers()
        original = config.default_key_bindings()
        self._save_button_bindings(original)

        with mock.patch.object(win32_input, "send_arrow_up") as old_action, mock.patch.object(
            win32_input, "send_key_combo_tap"
        ) as new_action:
            self.app._on_button_event("up", True, event_source="hid")
            changed = config.load_key_bindings(self.app._bindings_path)
            changed["bindings"]["up"] = {"kind": "mouse_wheel_up", "keys": []}
            self._save_button_bindings(changed)
            timers[0].fire()
            self.app._on_button_event("up", False, event_source="hid")

        self.assertEqual(old_action.call_count, 2)
        new_action.assert_not_called()
        self.assertIsNone(self.app._pending_bindings)

    def test_legacy_combo_change_never_creates_an_active_combo(self):
        original = config.default_key_bindings()
        original["combo_bindings"] = {
            "modifier": "tv",
            "bindings": {
                "up": key_mapping.ButtonAction(
                    key_mapping.ActionKind.ESCAPE
                ).to_dict()
            },
            "display_notes": {},
        }
        self._save_button_bindings(original)

        self.app._on_button_event("tv", True, event_source="hid")
        self.assertFalse(self.app._button_combos.has_active_combo())
        changed = config.load_key_bindings(self.app._bindings_path)
        changed["combo_bindings"]["bindings"]["up"] = (
            key_mapping.ButtonAction(
                key_mapping.ActionKind.MOUSE_LEFT_CLICK
            ).to_dict()
        )
        self._save_button_bindings(changed)

        with mock.patch.object(win32_input, "send_mouse_button_click") as combo_action:
            self.app._on_button_event("up", True, event_source="hid")
            self.app._on_button_event("up", False, event_source="hid")
            self.app._on_button_event("tv", False, event_source="hid")

        combo_action.assert_not_called()
        self.assertFalse(self.app._button_combos.has_active_combo())
        self.assertIsNone(self.app._pending_bindings)


class CandidateResolutionWiringTests(_AppWiringTestCase):

    def test_runtime_status_cleanup_only_removes_the_current_process_file(self):
        other_pid = app_module.os.getpid() + 1
        bridge_runtime_status.publish_status(
            self.app._config_root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=other_pid,
        )

        self.app.clear_runtime_status()

        self.assertEqual(
            bridge_runtime_status.read_status(self.app._config_root).pid,
            other_pid,
        )
        bridge_runtime_status.publish_status(
            self.app._config_root,
            bridge_runtime_status.BridgeConnectionState.CONNECTED,
            pid=app_module.os.getpid(),
        )
        self.app.clear_runtime_status()
        self.assertIsNone(bridge_runtime_status.read_status(self.app._config_root))



class DoubaoAsyncStartupTests(_AppWiringTestCase):
    def test_production_capture_watch_checks_promptly_and_keeps_verified_pid(self):
        from ovb.voice_playback_session_windows import CaptureSession
        reader_path = "ovb.chromecast_host_activity.read_doubao_capture_for_pid"
        with mock.patch(reader_path, side_effect=[(), (), (CaptureSession("endpoint", "capture", 123, 1),)]) as reader:
            watch = self.production_doubao_capture_watch_factory(123)
            watch.begin()
            watch.poll(1)
            self.assertIsNone(watch.identity)
            watch.poll(1.06)
            self.assertEqual(watch.status, "tracking")
            self.assertEqual(reader.call_args_list, [mock.call(123)] * 3)

    def _cold_process(self):
        physicalizer = self.app._voice_shortcut.doubao_physicalizer
        physicalizer.status = "unavailable"
        physicalizer.error = "ImeService.exe is not running"
        return physicalizer

    def test_non_process_failures_are_not_retried(self):
        physicalizer = self.app._voice_shortcut.doubao_physicalizer
        physicalizer.start.return_value = False
        for status, error in (("unsupported_version", "unsupported"),
                              ("unavailable", "Python frida package is not installed"),
                              ("cleanup_required", "retained resources")):
            with self.subTest(status=status, error=error):
                physicalizer.start.reset_mock()
                physicalizer.status, physicalizer.error = status, error
                self.assertFalse(self.app._prepare_doubao_voice_physicalizer(
                    ("ralt",), cancel_event=threading.Event()
                ))
                physicalizer.start.assert_called_once()

    def test_synchronous_caller_does_not_wait_for_missing_process(self):
        physicalizer = self._cold_process()
        physicalizer.start.return_value = False
        self.assertFalse(self.app._prepare_doubao_voice_physicalizer(("ralt",)))
        physicalizer.start.assert_called_once()


class HostHotkeyFailureSuppressesMicOpenTests(_AppWiringTestCase):

    def test_focus_trace_keeps_unconfirmed_voice_ui_explicitly_unknown(self):
        trace = mock.Mock()
        trace.current_context.return_value = {
            "gesture_id": "gesture-2",
            "attempt_id": "attempt-2",
        }
        self.app._diagnostic_trace = trace
        before = app_module.voice_interaction_diagnostics_windows.FocusSnapshot(
            supported=True,
            foreground_pid=100,
            foreground_class="ChatWnd",
            focus_handle=200,
            focus_class="Edit",
            text_length=3,
        )
        after = replace(before, text_length=8)
        with mock.patch.object(
            app_module.voice_interaction_diagnostics_windows,
            "capture_focus_snapshot",
            side_effect=[before, after],
        ):
            self.app._capture_voice_focus_before()
            observation = self.app._log_voice_submission_observation()

        self.assertEqual(observation.text_delta, 5)
        focus_events = [
            call
            for call in trace.emit.call_args_list
            if call.args[0]
            in {"voice_focus_snapshot", "voice_submission_observation"}
        ]
        self.assertEqual(len(focus_events), 2)
        self.assertEqual(focus_events[0].kwargs["voice_ui"], "unknown")
        self.assertEqual(focus_events[1].kwargs["voice_ui"], "unknown")
        self.assertEqual(focus_events[1].kwargs["text_delta"], 5)

    """Hold-to-talk must fail closed and never leave a host key held."""

    @unittest.skipIf(
        sys.platform == "win32",
        "only exercises the off-Windows input-backend gate",
    )

    def test_voice_hotkey_release_debt_keeps_only_one_retry_timer(self):
        self.app._voice_shortcut.pending_tokens = DEFAULT_VOICE_TOKENS

        with self.app._voice_shortcut.lock:
            self.app._voice_shortcut.schedule_release_retry()
            self.app._voice_shortcut.schedule_release_retry()

        self.assertEqual(len(self._voice_hotkey_release_timers), 1)
        self.assertIs(
            self.app._voice_shortcut.retry_timer,
            self._voice_hotkey_release_timers[0],
        )

    def test_safety_release_uses_the_original_shortcut_after_settings_change(self):
        self.app._voice_shortcut.pending_tokens = ("ralt",)
        self.app._voice_shortcut.hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+l")
        calls = []

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(tokens),
        ):
            self.assertTrue(self.app._voice_shortcut.release_pending())

        self.assertEqual(calls, [("ralt",)])

    def test_playback_mute_guard_is_wetype_only_and_uses_open_sink_identity(self):
        self.assertEqual(
            self.app._voice_shortcut.wetype_control._prepare_playback_mute_guard,
            self.app._prepare_wetype_playback_mute_guard,
        )
        self.assertIsNone(self.app._voice_shortcut.doubao_control._prepare_playback_mute_guard)
        with mock.patch.object(
            app_module.voice_playback_session_windows, "prepare_playback_mute_guard"
        ) as prepare:
            self.app._voice_audio.sink = None
            self.assertIsNone(self.app._prepare_wetype_playback_mute_guard())
            self.app._voice_audio.sink = mock.Mock(ready=False, endpoint_name="not-open")
            self.assertIsNone(self.app._prepare_wetype_playback_mute_guard())
            prepare.assert_not_called()
            self.app._voice_audio.sink = mock.Mock(ready=True, endpoint_name="opened-endpoint")
            self.app._config["output_endpoint_name"] = "different-config"
            self.assertIs(self.app._prepare_wetype_playback_mute_guard(), prepare.return_value)
            prepare.assert_called_once_with("opened-endpoint")

    def test_wetype_provider_uses_configured_control_for_start_and_stop(self):
        self.app._config["voice_program"] = (
            voice_program_manager.normalize_voice_program_settings(
                {"provider": voice_program_manager.VOICE_PROGRAM_WETYPE}
            )
        )
        self.app._voice_shortcut.hotkey = app_module.hotkey.HotkeySpec.parse(
            "+".join(WETYPE_VOICE_TOKENS)
        )
        wetype_control = mock.Mock()
        wetype_control.start.return_value = True
        wetype_control.stop.return_value = True
        self.app._voice_shortcut.wetype_control = wetype_control
        with mock.patch.object(
            win32_input, "send_voice_key_combo_down"
        ) as marked_down, mock.patch.object(
            win32_input, "send_voice_key_combo_up"
        ) as marked_up:
            self.assertTrue(
                self.app._voice_shortcut.apply(
                    app_module.voice_controller.VoiceHostAction.KEY_DOWN
                )
            )
            self.assertTrue(
                self.app._voice_shortcut.apply(
                    app_module.voice_controller.VoiceHostAction.KEY_UP
                )
            )

        wetype_control.start.assert_called_once_with(
            WETYPE_VOICE_TOKENS, activate_profile=True
        )
        wetype_control.stop.assert_called_once_with()
        marked_down.assert_not_called()
        marked_up.assert_not_called()
        self.assertIsNone(self.app._voice_shortcut.pending_tokens)

    def test_wetype_fast_confirmation_uses_registered_generation(self):
        from ovb.voice_playback_session_windows import CaptureSession
        self.app._config["voice_program"] = voice_program_manager.normalize_voice_program_settings(
            {"provider": voice_program_manager.VOICE_PROGRAM_WETYPE})
        control = self.app._voice_shortcut.wetype_control
        workers = []
        def spawn(**kwargs):
            worker = threading.Thread(**kwargs)
            workers.append(worker)
            return worker
        schedule = control._schedule_confirmation
        def confirm_before_return(observation):
            schedule(observation)
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())
        with (
            mock.patch.object(control, "_activate_profile", return_value=True),
            mock.patch.object(control, "_run_sta", side_effect=lambda callback: callback()),
            mock.patch.object(control, "_press_keys"),
            mock.patch.object(control, "_release_keys"),
            mock.patch.object(control, "_prepare_playback_mute_guard", None),
            mock.patch.object(control, "_capture_reader", side_effect=[(), (CaptureSession("mic", "new", 42, 1),)]),
            mock.patch.object(control, "_wait_for_cancel", return_value=False),
            mock.patch.object(control, "_thread_factory", spawn),
            mock.patch.object(control, "_schedule_confirmation", side_effect=confirm_before_return),
        ):
            self.assertTrue(self.app._voice_shortcut.apply(app_module.voice_controller.VoiceHostAction.KEY_DOWN))
            self.assertTrue(self.app._voice_shortcut.runtime_mic_confirmed)
            self.assertEqual(self.app._voice_shortcut.runtime_generation, control.current_generation)
            self.assertTrue(self.app._voice_shortcut.apply(app_module.voice_controller.VoiceHostAction.KEY_UP))
            self.assertFalse(control.cleanup_pending)

    def test_doubao_provider_activates_its_profile_control_for_start_and_stop(self):
        self.app._config["voice_program"] = (
            voice_program_manager.normalize_voice_program_settings(
                {"provider": voice_program_manager.VOICE_PROGRAM_DOUBAO_IME}
            )
        )
        doubao_control = mock.Mock()
        doubao_control.start.return_value = True
        doubao_control.stop.return_value = True
        self.app._voice_shortcut.doubao_control = doubao_control
        with mock.patch.object(
            win32_input, "send_voice_key_combo_down"
        ) as direct_down, mock.patch.object(
            win32_input, "send_voice_key_combo_up"
        ) as direct_up, mock.patch.object(
            self.app._voice_shortcut.wetype_control, "start"
        ) as wetype_start:
            self.assertTrue(
                self.app._voice_shortcut.apply(
                    app_module.voice_controller.VoiceHostAction.KEY_DOWN
                )
            )
            self.assertTrue(
                self.app._voice_shortcut.apply(
                    app_module.voice_controller.VoiceHostAction.KEY_UP
                )
            )

        doubao_control.start.assert_called_once_with(
            DEFAULT_VOICE_TOKENS, activate_profile=True
        )
        doubao_control.stop.assert_called_once_with()
        self.app._voice_shortcut.doubao_physicalizer.start.assert_called_once_with((0xA5,))
        wetype_start.assert_not_called()
        direct_down.assert_not_called()
        direct_up.assert_not_called()
        self.assertIsNone(self.app._voice_shortcut.pending_tokens)

    def test_successful_hold_down_is_owned_until_matching_key_up(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
            side_effect=lambda tokens: calls.append(("down", tokens)),
        ), mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
            side_effect=lambda tokens: calls.append(("up", tokens)),
        ):
            self.assertTrue(
                self.app._voice_shortcut.apply(
                    app_module.voice_controller.VoiceHostAction.KEY_DOWN
                )
            )
            self.assertEqual(
                self.app._voice_shortcut.pending_tokens,
                DEFAULT_VOICE_TOKENS,
            )
            self.assertTrue(
                self.app._voice_shortcut.apply(
                    app_module.voice_controller.VoiceHostAction.KEY_UP
                )
            )

        self.assertEqual(
            calls,
            [("down", DEFAULT_VOICE_TOKENS), ("up", DEFAULT_VOICE_TOKENS)],
        )
        self.assertIsNone(self.app._voice_shortcut.pending_tokens)

    def test_non_wetype_providers_keep_the_marked_voice_backend(self):
        providers = (
            voice_program_manager.VOICE_PROGRAM_NONE,
            voice_program_manager.VOICE_PROGRAM_SOGOU,
            voice_program_manager.VOICE_PROGRAM_CUSTOM,
        )
        for provider in providers:
            with self.subTest(provider=provider):
                self.app._config["voice_program"] = (
                    voice_program_manager.normalize_voice_program_settings(
                        {"provider": provider}
                    )
                )
                self.app._voice_shortcut.active_backend = None
                with mock.patch.object(
                    win32_input, "send_voice_key_combo_down"
                ) as marked_down, mock.patch.object(
                    self.app._voice_shortcut.wetype_control, "start"
                ) as wetype_start:
                    self.assertTrue(
                        self.app._voice_shortcut.apply(
                            app_module.voice_controller.VoiceHostAction.KEY_DOWN
                        )
                    )

                marked_down.assert_called_once_with(DEFAULT_VOICE_TOKENS)
                wetype_start.assert_not_called()

    def test_wetype_failed_stop_keeps_the_session_backend_for_retry(self):
        self.app._config["voice_program"] = (
            voice_program_manager.normalize_voice_program_settings(
                {"provider": voice_program_manager.VOICE_PROGRAM_WETYPE}
            )
        )
        wetype_control = mock.Mock()
        wetype_control.start.return_value = True
        wetype_control.stop.side_effect = [False, True]
        self.app._voice_shortcut.wetype_control = wetype_control
        self.assertTrue(
            self.app._voice_shortcut.apply(
                app_module.voice_controller.VoiceHostAction.KEY_DOWN
            )
        )

        self.app._config["voice_program"] = (
            voice_program_manager.normalize_voice_program_settings(
                {"provider": voice_program_manager.VOICE_PROGRAM_SOGOU}
            )
        )
        self.assertFalse(
            self.app._voice_shortcut.apply(
                app_module.voice_controller.VoiceHostAction.KEY_UP
            )
        )
        self.assertEqual(
            self.app._voice_shortcut.active_backend,
            app_module._VOICE_HOTKEY_BACKEND_WETYPE,
        )
        self.assertTrue(
            self.app._voice_shortcut.apply(
                app_module.voice_controller.VoiceHostAction.KEY_UP
            )
        )
        self.assertEqual(wetype_control.stop.call_count, 2)
        self.assertIsNone(self.app._voice_shortcut.active_backend)

    def test_hid_mic_button_is_ignored_until_ble_session_is_connected(self):
        self.app._ble_session = None
        with mock.patch.object(win32_input, "send_voice_key_combo_down") as hotkey:
            self.app._on_button_event("mic", True)

        hotkey.assert_not_called()
        self.assertFalse(self.app._voice_shortcut.controller.active)


class CorruptButtonBindingFailsClosedTests(_AppWiringTestCase):
    def test_unknown_or_malformed_binding_does_not_escape_raw_input_callback(self):
        for malformed in (
            {"kind": "unknown", "keys": []},
            {"kind": "key_combo", "keys": ["not_a_real_key"]},
            {"kind": "key_combo", "keys": "ctrl+a"},
            {},
            "not-a-mapping",
        ):
            self.app._bindings = {"bindings": {"back": malformed}}
            self.app._on_button_event("back", True)

        self.assertFalse(self.app._voice_shortcut.controller.active)


class OrdinaryButtonGestureWiringTests(_AppWiringTestCase):
    def _use_recorded_repeat_timers(self):
        timers = []

        def timer_factory(delay, callback):
            timer = _ManualTimer(callback)
            timer.delay = delay
            timers.append(timer)
            return timer

        self.app._button_gestures._timer_factory = timer_factory
        self.app._button_gestures._hold_timer_factory = (
            lambda _delay, callback: _ManualTimer(callback)
        )
        self.addCleanup(self.app._button_gestures.reset)
        return timers

    def test_direction_hold_repeats_faster_and_release_stops_both_sources(self):
        timers = self._use_recorded_repeat_timers()
        for source in ("hid_tap", "chromecast"):
            for button in ("up", "down", "left", "right"):
                with self.subTest(source=source, button=button), mock.patch.object(
                    win32_input, "send_arrow_" + button
                ) as send, mock.patch.object(
                    app_module.element_navigation_control_windows,
                    "route_mapped_navigation_key", return_value=False,
                ):
                    self.app._on_button_event(button, True, event_source=source)
                    send.assert_called_once_with()
                    self.assertEqual(timers[-1].delay, 0.350)
                    # Repeated device DOWN reports must not add extra steps.
                    self.app._on_button_event(button, True, event_source=source)
                    send.assert_called_once_with()
                    for _ in range(3):
                        timers[-1].fire()
                        self.assertEqual(timers[-1].delay, 0.065)
                    self.assertEqual(send.call_count, 4)
                    stale_timer = timers[-1]
                    self.app._on_button_event(button, False, event_source=source)
                    # A timer already dispatched before cancellation is stale too.
                    stale_timer.callback()
                    self.assertEqual(send.call_count, 4)
                    self.app._on_button_event(button, True, event_source=source)
                    self.assertEqual(send.call_count, 5)
                    self.assertEqual(timers[-1].delay, 0.350)
                    stale_timer.callback()
                    self.assertEqual(send.call_count, 5)
                    self.app._on_button_event(button, False, event_source=source)

    def test_direction_speed_does_not_change_other_repeatable_actions(self):
        timers = self._use_recorded_repeat_timers()
        cases = (
            ("up", key_mapping.ActionKind.SYSTEM_VOLUME_UP, 0.100),
            ("up", key_mapping.ActionKind.DELETE_BACKWARD, 0.100),
            ("volume_up", key_mapping.ActionKind.SYSTEM_VOLUME_UP, 0.100),
            ("volume_down", key_mapping.ActionKind.ARROW_DOWN, 0.100),
            ("back", key_mapping.ActionKind.DELETE_BACKWARD, 0.050),
        )
        for button, kind, interval in cases:
            with self.subTest(button=button, kind=kind), mock.patch.object(
                self.app, "_apply_button_action"
            ) as apply:
                self.app._bindings["bindings"][button] = key_mapping.ButtonAction(kind).to_dict()
                self.app._on_button_event(button, True, event_source="hid_tap")
                self.assertEqual(timers[-1].delay, 0.350)
                timers[-1].fire()
                self.assertEqual(timers[-1].delay, interval)
                self.assertEqual(apply.call_count, 2)
                self.app._on_button_event(button, False, event_source="hid_tap")

    def test_faster_direction_repeat_routes_navigation_without_duplicate_input(self):
        timers = self._use_recorded_repeat_timers()
        with mock.patch.object(
            app_module.element_navigation_control_windows,
            "route_mapped_navigation_key", return_value=True,
        ) as route, mock.patch.object(win32_input, "send_arrow_up") as send:
            self.app._on_button_event("up", True, event_source="hid_tap")
            timers[-1].fire()
            self.assertEqual(timers[-1].delay, 0.065)
            self.app._on_button_event("up", False, event_source="hid_tap")
            timers[-1].callback()
            self.assertEqual(route.call_args_list, [mock.call(0x26), mock.call(0x26)])
            send.assert_not_called()

    def test_dispatch_callback_does_not_reacquire_the_mapping_lock(self):
        lock_held = threading.Event()
        release_lock = threading.Event()
        callback_finished = threading.Event()

        def hold_mapping_lock():
            with self.app._button_mapping_lock:
                lock_held.set()
                release_lock.wait(1.0)

        holder = threading.Thread(target=hold_mapping_lock)
        callback = threading.Thread(
            target=lambda: (
                self.app._on_button_trigger(
                    "up",
                    app_module.button_gesture.ButtonTrigger.SINGLE_CLICK,
                ),
                callback_finished.set(),
            )
        )
        holder.start()
        try:
            self.assertTrue(lock_held.wait(1.0))
            with mock.patch.object(win32_input, "send_arrow_up"):
                callback.start()
                self.assertTrue(callback_finished.wait(0.5))
        finally:
            release_lock.set()
            holder.join(1.0)
            callback.join(1.0)

        self.assertFalse(holder.is_alive())
        self.assertFalse(callback.is_alive())

    def test_saved_mapping_is_reloaded_before_the_next_button_event(self):
        updated = config.default_key_bindings()
        updated["bindings"]["back"] = {"kind": "key_combo", "keys": ["f8"]}
        config.save_key_bindings(self.app._bindings_path, updated)

        calls = []
        original = win32_input.send_key_combo_tap
        win32_input.send_key_combo_tap = lambda keys: calls.append(tuple(keys))
        try:
            self.app._on_button_event("back", True)
            self.app._on_button_event("back", False)
        finally:
            win32_input.send_key_combo_tap = original

        self.assertEqual(calls, [("f8",)])

    def test_semantic_arrow_action_uses_its_function_executor(self):
        calls = []
        original = getattr(win32_input, "send_arrow_up", None)
        win32_input.send_arrow_up = lambda: calls.append("arrow_up")
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_UP)
            )
        finally:
            if original is None:
                delattr(win32_input, "send_arrow_up")
            else:
                win32_input.send_arrow_up = original

        self.assertEqual(calls, ["arrow_up"])

    def test_navigation_owned_mapping_is_not_also_injected(self):
        control = app_module.element_navigation_control_windows
        for accepted in (True, False):
            with self.subTest(accepted=accepted), mock.patch.object(
                control, "route_mapped_navigation_key", return_value=accepted
            ) as route, mock.patch.object(win32_input, "send_arrow_up") as inject:
                self.app._apply_button_action(key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_UP))
                route.assert_called_once_with(0x26)
                self.assertEqual(inject.call_count, 0 if accepted else 1)

    def test_custom_combo_bypasses_semantic_navigation_route(self):
        with mock.patch.object(app_module.element_navigation_control_windows,
                               "route_mapped_navigation_key", return_value=True) as route, mock.patch.object(
            win32_input, "send_key_combo_tap"
        ) as inject:
            self.app._apply_button_action(key_mapping.ButtonAction(key_mapping.ActionKind.KEY_COMBO, keys=("up",)))
            route.assert_not_called()
            inject.assert_called_once_with(("up",))

    def test_remote_page_key_controls_navigation_only_when_claimed(self):
        control = app_module.element_navigation_control_windows
        for token, vk in (
            ("pageup", 0x21), ("page_up", 0x21),
            ("pagedown", 0x22), ("page_down", 0x22),
        ):
            action = settings_ui._display_to_action(token)
            for accepted in (True, False):
                with self.subTest(token=token, accepted=accepted), mock.patch.object(
                    control, "route_mapped_navigation_key", return_value=accepted
                ) as route, mock.patch.object(win32_input, "send_key_combo_tap") as inject:
                    self.app._apply_button_action(action)
                    route.assert_called_once_with(vk)
                    self.assertEqual(inject.call_count, 0 if accepted else 1)

    def test_app_switcher_cleanup_tracks_all_persistent_switcher_keys(self):
        with mock.patch.object(
            win32_input,
            "send_app_switcher",
            side_effect=win32_input.InputCleanupIncompleteError("stuck"),
        ):
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.APP_SWITCHER)
            )

        self.assertEqual(
            self.app._button_key_release_pending,
            ("ctrl", "alt", "tab"),
        )
        self.assertEqual(len(self._button_input_release_timers), 1)

    def test_element_navigation_action_uses_the_isolated_controller(self):
        result = app_module.element_navigation_control_windows.ToggleResult(
            app_module.element_navigation_control_windows.ToggleResultKind.DELIVERED,
            321,
        )
        with mock.patch.object(
            app_module.element_navigation_control_windows,
            "toggle_element_navigation",
            return_value=result,
        ) as toggle:
            self.app._apply_button_action(
                key_mapping.ButtonAction(
                    key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE
                )
            )

        toggle.assert_called_once_with()

    def test_element_navigation_failure_does_not_stop_button_processing(self):
        with mock.patch.object(
            app_module.element_navigation_control_windows,
            "toggle_element_navigation",
            side_effect=RuntimeError("simulated companion failure"),
        ), self.assertLogs(level="ERROR") as captured:
            self.app._apply_button_action(
                key_mapping.ButtonAction(
                    key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE
                )
            )

        self.assertTrue(
            any("element navigation toggle failed" in line for line in captured.output)
        )

    def test_element_navigation_diagnostic_matches_the_dispatch_result(self):
        control = app_module.element_navigation_control_windows
        cases = [
            (control.ToggleResult(kind, 321, error="unavailable"),
             kind != control.ToggleResultKind.FAILED, "")
            for kind in control.ToggleResultKind
        ]
        cases.append((RuntimeError("simulated companion failure"), False, "RuntimeError"))
        for result, success, error_type in cases:
            with self.subTest(result=result), mock.patch.object(
                control, "toggle_element_navigation",
                side_effect=result if isinstance(result, Exception) else None,
                return_value=result,
            ), mock.patch.object(self.app._diagnostic_trace, "emit") as emit:
                self.app._apply_button_action(key_mapping.ButtonAction(
                    key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE))
                outcomes = [call.kwargs for call in emit.call_args_list
                            if call.args[0] == "action_result"]
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0]["success"], success)
                self.assertEqual(outcomes[0]["error"],
                                 "" if success else "element_navigation_failed")
                self.assertEqual(outcomes[0]["error_type"], error_type)

    def test_application_diagnostic_matches_launch_availability(self):
        for available in (False, True):
            with self.subTest(available=available), mock.patch.object(
                app_module, "open_configured_application", return_value=available,
            ) as launch, mock.patch.object(self.app._diagnostic_trace, "emit") as emit:
                action = key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_CODEX)
                self.app._apply_button_action(action)
                launch.assert_called_once_with(action)
                outcomes = [call.kwargs for call in emit.call_args_list
                            if call.args[0] == "action_result"]
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0]["success"], available)
                self.assertEqual(outcomes[0]["error"],
                                 "" if available else "application_unavailable")

    def test_incomplete_button_rollback_is_released_before_the_next_action(self):
        original_up = win32_input.send_arrow_up
        original_down = win32_input.send_arrow_down
        original_release = win32_input.send_key_combo_up
        releases = []
        actions = []
        win32_input.send_arrow_up = lambda: (_ for _ in ()).throw(
            win32_input.InputCleanupIncompleteError("simulated stuck arrow key")
        )
        win32_input.send_arrow_down = lambda: actions.append("arrow_down")
        win32_input.send_key_combo_up = lambda keys: releases.append(tuple(keys))
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_UP)
            )
            self.assertEqual(self.app._button_key_release_pending, ("up",))

            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_DOWN)
            )
        finally:
            win32_input.send_arrow_up = original_up
            win32_input.send_arrow_down = original_down
            win32_input.send_key_combo_up = original_release

        self.assertEqual(releases, [("up",)])
        self.assertEqual(actions, ["arrow_down"])
        self.assertIsNone(self.app._button_key_release_pending)

    def test_pending_button_release_blocks_new_actions_when_retry_is_incomplete(self):
        original_down = win32_input.send_arrow_down
        original_release = win32_input.send_key_combo_up
        actions = []
        self.app._button_key_release_pending = ("ctrl", "l")
        win32_input.send_arrow_down = lambda: actions.append("arrow_down")
        win32_input.send_key_combo_up = lambda _keys: (_ for _ in ()).throw(
            win32_input.InputCleanupIncompleteError("still stuck")
        )
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_DOWN)
            )
        finally:
            win32_input.send_arrow_down = original_down
            win32_input.send_key_combo_up = original_release

        self.assertEqual(actions, [])
        self.assertEqual(self.app._button_key_release_pending, ("ctrl", "l"))

    def test_incomplete_button_release_retries_without_another_action(self):
        with mock.patch.object(
            win32_input,
            "send_arrow_up",
            side_effect=win32_input.InputCleanupIncompleteError("stuck"),
        ), mock.patch.object(
            win32_input,
            "send_key_combo_up",
        ) as send_up:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_UP)
            )
            self.assertEqual(len(self._button_input_release_timers), 1)
            self._button_input_release_timers[0].fire()

        send_up.assert_called_once_with(("up",))
        self.assertIsNone(self.app._button_key_release_pending)
        self.assertIsNone(self.app._button_input_release_retry_timer)
        self.assertEqual(
            self.app._button_input_release_retry_delay,
            app_module._BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS,
        )

    def test_repeated_release_failures_keep_only_one_live_retry(self):
        self.app._button_key_release_pending = ("up",)
        with mock.patch.object(
            win32_input,
            "send_key_combo_up",
            side_effect=win32_input.InputCleanupIncompleteError("still stuck"),
        ):
            self.assertFalse(self.app._release_pending_button_keys())
            first_timer = self.app._button_input_release_retry_timer
            self.assertFalse(self.app._release_pending_button_keys())
            self.assertIs(self.app._button_input_release_retry_timer, first_timer)
            self.assertEqual(len(self._button_input_release_timers), 1)

            self._button_input_release_timers[0].fire()
            second_timer = self.app._button_input_release_retry_timer
            self.assertIsNot(second_timer, first_timer)
            self.assertEqual(len(self._button_input_release_timers), 2)

            self._button_input_release_timers[0].fire()
            self.assertIs(self.app._button_input_release_retry_timer, second_timer)
            self.assertEqual(len(self._button_input_release_timers), 2)

    def test_key_and_mouse_release_debt_share_one_retry_timer(self):
        self.app._button_key_release_pending = ("up",)
        self.app._button_mouse_release_pending = "left"
        with mock.patch.object(
            win32_input,
            "send_key_combo_up",
            side_effect=win32_input.InputCleanupIncompleteError("key stuck"),
        ), mock.patch.object(
            win32_input,
            "send_mouse_button_up",
            side_effect=win32_input.InputCleanupIncompleteError("mouse stuck"),
        ):
            self.assertFalse(self.app._release_pending_button_inputs())

        self.assertEqual(len(self._button_input_release_timers), 1)
        self.assertIs(
            self.app._button_input_release_retry_timer,
            self._button_input_release_timers[0],
        )

    def test_mouse_actions_dispatch_physical_buttons(self):
        button_calls = []
        with mock.patch.object(
            win32_input,
            "send_mouse_button_click",
            side_effect=lambda button: button_calls.append(button),
        ):
            for action_kind in (
                key_mapping.ActionKind.MOUSE_LEFT_CLICK,
                key_mapping.ActionKind.MOUSE_RIGHT_CLICK,
                key_mapping.ActionKind.MOUSE_MIDDLE_CLICK,
                key_mapping.ActionKind.MOUSE_X1_CLICK,
                key_mapping.ActionKind.MOUSE_X2_CLICK,
            ):
                self.app._apply_button_action(key_mapping.ButtonAction(action_kind))

        self.assertEqual(button_calls, ["left", "right", "middle", "x1", "x2"])

    def test_old_wheel_mapping_becomes_nonrepeating_page_key(self):
        for button_id in ("mic", "power", "up", "down", "left", "right", "ok", "back",
                          "volume_up", "volume_down", "home", "menu", "tv"):
            for kind in (key_mapping.ActionKind.MOUSE_WHEEL_UP,
                         key_mapping.ActionKind.MOUSE_WHEEL_DOWN):
                with self.subTest(button_id=button_id, kind=kind):
                    self.app._bindings["bindings"][button_id] = {"kind": kind.value, "keys": []}
                    self.assertFalse(self.app._is_button_repeatable(button_id))
                    action = key_mapping.button_action_for(
                        self.app._bindings, button_id, key_mapping.ButtonTrigger.SINGLE_CLICK
                    )
                    self.assertEqual(action.kind, key_mapping.ActionKind.KEY_COMBO)

    def test_every_visible_action_option_saves_and_has_an_execution_route(self):
        senders = (
            "send_key_combo_tap", "send_escape", "send_return", "send_arrow_up",
            "send_arrow_down", "send_arrow_left", "send_arrow_right",
            "send_delete_backward", "send_show_desktop", "send_context_menu",
            "send_app_switcher", "send_volume_up", "send_volume_down",
            "send_volume_mute", "send_play_pause", "send_mouse_button_click",
        )
        with ExitStack() as stack:
            effects = [stack.enter_context(mock.patch.object(win32_input, name)) for name in senders]
            effects.append(stack.enter_context(mock.patch.object(
                app_module, "open_configured_application", return_value=True
            )))
            effects.append(stack.enter_context(mock.patch.object(
                app_module.element_navigation_control_windows,
                "toggle_element_navigation",
                return_value=app_module.element_navigation_control_windows.ToggleResult(
                    app_module.element_navigation_control_windows.ToggleResultKind.DELIVERED, 321
                ),
            )))
            stack.enter_context(mock.patch.object(
                app_module.element_navigation_control_windows,
                "route_mapped_navigation_key", return_value=False
            ))
            for option in settings_ui._PRESET_KEY_COMBOS:
                with self.subTest(option=option):
                    self.assertEqual(settings_ui.button_action_validation_message(
                        "up", "single_click", option
                    ), "")
                    self.assertEqual(settings_ui.button_action_validation_message(
                        "up", "double_click", option
                    ), "")
                    action = settings_ui._display_to_action(option)
                    if action.kind == key_mapping.ActionKind.DISABLED:
                        continue
                    before = sum(effect.call_count for effect in effects)
                    self.app._apply_button_action(action)
                    self.assertEqual(sum(effect.call_count for effect in effects), before + 1)

    def test_physical_mouse_hold_skips_click_without_recording_cleanup_debt(self):
        with mock.patch.object(
            win32_input,
            "send_mouse_button_click",
            side_effect=win32_input.MouseButtonInUseError("physical hold"),
        ):
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.MOUSE_LEFT_CLICK)
            )

        self.assertIsNone(self.app._button_mouse_release_pending)

    def test_incomplete_mouse_click_is_retained_and_released_before_next_action(self):
        actions = []
        releases = []
        with mock.patch.object(
            win32_input,
            "send_mouse_button_click",
            side_effect=win32_input.InputCleanupIncompleteError("still down"),
        ), mock.patch.object(
            win32_input,
            "send_mouse_button_up",
            side_effect=lambda button: releases.append(button),
        ), mock.patch.object(
            win32_input,
            "send_arrow_down",
            side_effect=lambda: actions.append("arrow_down"),
        ):
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.MOUSE_X2_CLICK)
            )
            self.assertEqual(self.app._button_mouse_release_pending, "x2")

            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_DOWN)
            )

        self.assertEqual(releases, ["x2"])
        self.assertEqual(actions, ["arrow_down"])
        self.assertIsNone(self.app._button_mouse_release_pending)

    def test_pending_mouse_release_blocks_new_actions_when_retry_is_incomplete(self):
        actions = []
        self.app._button_mouse_release_pending = "left"
        with mock.patch.object(
            win32_input,
            "send_mouse_button_up",
            side_effect=win32_input.InputCleanupIncompleteError("still down"),
        ), mock.patch.object(
            win32_input,
            "send_arrow_down",
            side_effect=lambda: actions.append("arrow_down"),
        ):
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_DOWN)
            )

        self.assertEqual(actions, [])
        self.assertEqual(self.app._button_mouse_release_pending, "left")

    def test_fallback_safety_release_cannot_erase_a_new_pending_action_release(self):
        first_release_started = threading.Event()
        allow_first_release = threading.Event()
        action_attempted = threading.Event()
        release_calls = []
        release_calls_lock = threading.Lock()

        def release_keys(keys):
            with release_calls_lock:
                release_calls.append(tuple(keys))
                call_number = len(release_calls)
            if call_number == 1:
                first_release_started.set()
                allow_first_release.wait(1.0)

        def fail_action():
            action_attempted.set()
            raise win32_input.InputCleanupIncompleteError("simulated stuck down key")

        with mock.patch.object(
            win32_input,
            "send_key_combo_up",
            side_effect=release_keys,
        ), mock.patch.object(
            win32_input,
            "send_arrow_down",
            side_effect=fail_action,
        ):
            fallback_thread = threading.Thread(
                target=lambda: self.app._release_raw_fallback_keyups(
                    {"up"},
                    reason="test_handover",
                )
            )
            fallback_thread.start()
            self.assertTrue(first_release_started.wait(1.0))

            action_thread = threading.Thread(
                target=lambda: self.app._apply_button_action(
                    key_mapping.ButtonAction(key_mapping.ActionKind.ARROW_DOWN)
                )
            )
            action_thread.start()
            self.assertFalse(action_attempted.wait(0.1))

            allow_first_release.set()
            fallback_thread.join(1.0)
            action_thread.join(1.0)

        self.assertFalse(fallback_thread.is_alive())
        self.assertFalse(action_thread.is_alive())
        self.assertEqual(release_calls, [("up",)])
        self.assertEqual(self.app._button_key_release_pending, ("down",))

    def test_open_app_action_uses_application_executor(self):
        calls = []
        original = getattr(app_module, "open_configured_application", None)
        app_module.open_configured_application = lambda action: calls.append(action.kind)
        try:
            self.app._apply_button_action(
                key_mapping.ButtonAction(key_mapping.ActionKind.OPEN_CODEX)
            )
        finally:
            if original is None:
                delattr(app_module, "open_configured_application")
            else:
                app_module.open_configured_application = original

        self.assertEqual(calls, [key_mapping.ActionKind.OPEN_CODEX])

    def test_one_physical_direction_press_emits_one_tap_mapping_action(self):
        calls = []
        with mock.patch.object(
            win32_input,
            "send_arrow_up",
            side_effect=lambda: calls.append("up"),
        ):
            self.app._on_button_event("up", True, event_source="hid")
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_button_event("up", True, event_source="hid")
            self.app._on_button_event("up", False, event_source="hid_tap")
            self.app._on_button_event("up", False, event_source="hid")

        self.assertEqual(calls, ["up"])

    def test_custom_combo_on_direction_button_does_not_start_hold_repeat(self):
        self.app._bindings["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.KEY_COMBO,
            ("shift", "3"),
        ).to_dict()

        with mock.patch.object(win32_input, "send_key_combo_tap") as action:
            self.app._on_button_event("up", True, event_source="hid_tap")

        action.assert_called_once_with(("shift", "3"))
        self.assertNotIn("up", self.app._button_gestures._repeat_timers)

        self.app._on_button_event("up", False, event_source="hid_tap")

    def test_legacy_remote_combo_configuration_keeps_single_actions(self):
        self.app._bindings["bindings"]["tv"] = {
            "kind": "escape",
            "keys": [],
        }
        self.app._bindings["bindings"]["up"] = {
            "kind": "arrow_up",
            "keys": [],
        }
        self.app._bindings["combo_bindings"] = {
            "modifier": "tv",
            "bindings": {"up": {"kind": "return", "keys": []}},
            "display_notes": {},
        }

        with mock.patch.object(win32_input, "send_escape") as tv_action, mock.patch.object(
            win32_input, "send_arrow_up"
        ) as up_action, mock.patch.object(win32_input, "send_return") as combo_action:
            self.app._on_button_event("tv", True, event_source="hid_tap")
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_button_event("up", False, event_source="hid_tap")
            self.app._on_button_event("tv", False, event_source="hid_tap")

        combo_action.assert_not_called()
        tv_action.assert_called_once_with()
        up_action.assert_called_once_with()

    def test_unused_remote_combo_modifier_keeps_its_single_click(self):
        self.app._bindings["bindings"]["tv"] = {
            "kind": "escape",
            "keys": [],
        }
        self.app._bindings["combo_bindings"] = {
            "modifier": "tv",
            "bindings": {"up": {"kind": "return", "keys": []}},
            "display_notes": {},
        }

        with mock.patch.object(win32_input, "send_escape") as tv_action:
            self.app._on_button_event("tv", True)
            self.app._on_button_event("tv", False)

        tv_action.assert_called_once_with()

    def test_quicker_uri_action_uses_the_protocol_executor(self):
        action = key_mapping.ButtonAction(
            key_mapping.ActionKind.QUICKER_URI,
            uri="quicker:runaction:pin-window",
        )

        with mock.patch.object(
            app_module.action_executor, "open_quicker_uri"
        ) as launcher:
            self.app._apply_button_action(action)

        launcher.assert_called_once_with(action)

    def test_raw_keyboard_fallback_never_executes_a_mapping(self):
        self.app._direct_hid_interception_ready = False

        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_raw_button_event("up", True, "keyboard")
            self.app._on_raw_button_event("up", False, "keyboard")

        press.assert_not_called()
        release.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())

    def test_raw_keyboard_fallback_tracks_both_edges_without_sticking(self):
        self.app._direct_hid_interception_ready = False

        self.app._on_raw_button_event("up", True, "keyboard")
        self.assertEqual(self.app._raw_fallback_buttons_down, {"up"})
        self.app._on_raw_button_event("up", False, "keyboard")

        self.assertEqual(self.app._raw_fallback_buttons_down, set())

    def test_direction_fallback_handles_cross_source_edges_without_mapping(self):
        self.app._direct_hid_interception_ready = False

        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_raw_button_event("up", True, "hid")
            self.app._on_raw_button_event("up", False, "keyboard")
            self.app._on_raw_button_event("down", True, "keyboard")
            self.app._on_raw_button_event("down", False, "hid")

        press.assert_not_called()
        release.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())

    def test_raw_hid_fallback_never_executes_a_mapping(self):
        self.app._direct_hid_interception_ready = False
        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_raw_button_event("ok", True, "hid")
            self.app._on_raw_button_event("ok", False, "hid")

        press.assert_not_called()
        release.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())

        self.app._direct_hid_interception_ready = True
        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_raw_button_event("ok", True, "hid")
            self.app._on_raw_button_event("ok", False, "hid")

        press.assert_not_called()
        release.assert_not_called()

    def test_raw_hid_fallback_clears_when_keyboard_edge_arrives_last(self):
        self.app._direct_hid_interception_ready = False

        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_raw_button_event("ok", True, "hid")
            self.app._on_raw_button_event("ok", False, "keyboard")

        press.assert_not_called()
        release.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())

    def test_every_raw_button_preserves_only_the_windows_original(self):
        self.app._direct_hid_interception_ready = False

        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release, mock.patch.object(
            win32_input, "send_voice_key_combo_down"
        ) as voice_down:
            for button_id in app_module._RAW_FALLBACK_KEY_TOKENS:
                with self.subTest(button_id=button_id):
                    self.app._on_raw_button_event(button_id, True, "hid")
                    self.app._on_raw_button_event(button_id, False, "keyboard")

        press.assert_not_called()
        release.assert_not_called()
        voice_down.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())

    def test_raw_fallback_hold_timeout_releases_windows_key_and_waits_for_up(self):
        self.app._direct_hid_interception_ready = False
        timers = []

        def timer_factory(_delay, callback):
            timer = _ManualTimer(callback)
            timers.append(timer)
            return timer

        self.app._raw_fallback_timer_factory = timer_factory
        with mock.patch.object(win32_input, "send_key_combo_up") as release_keys, mock.patch.object(
            self.app._button_gestures, "press"
        ) as press:
            self.app._on_raw_button_event("up", True, "keyboard")
            timers[0].fire()
            self.app._on_raw_button_event("up", True, "keyboard")
            self.app._on_raw_button_event("up", False, "keyboard")

        release_keys.assert_called_once_with(("up",))
        press.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())

    def test_rearm_guard_never_treats_elapsed_time_as_a_release(self):
        self.app._input_rearm_blocked_buttons = {"ok"}

        with mock.patch.object(app_module.time, "monotonic", return_value=101.0), mock.patch.object(
            self.app._button_gestures, "press"
        ) as press:
            self.app._on_button_event("ok", True, event_source="hid_tap")

        press.assert_not_called()
        self.assertEqual(self.app._input_rearm_blocked_buttons, {"ok"})
        self.app._on_button_event("ok", False, event_source="hid_tap")
        self.assertEqual(self.app._input_rearm_blocked_buttons, set())

        with mock.patch.object(self.app._button_gestures, "press") as press:
            self.app._on_button_event("ok", True, event_source="hid_tap")
        press.assert_called_once_with("ok")


class VoiceMappingProductBoundaryTests(_AppWiringTestCase):
    def _save_bindings(self, bindings):
        config.save_key_bindings(self.app._bindings_path, bindings)
        self.app._reload_settings_if_changed()

    def test_non_mic_voice_mapping_is_disabled_as_a_whole_button(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD
        ).to_dict()
        bindings["secondary_bindings"]["up"] = {
            "double_click": key_mapping.ButtonAction(
                key_mapping.ActionKind.ARROW_DOWN
            ).to_dict()
        }
        self._save_bindings(bindings)

        with mock.patch.object(win32_input, "send_voice_key_combo_down") as voice, mock.patch.object(
            win32_input, "send_arrow_down"
        ) as secondary:
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_button_event("up", False, event_source="hid_tap")
            self.app._on_button_trigger(
                "up", app_module.button_gesture.ButtonTrigger.DOUBLE_CLICK
            )

        self.assertEqual(self.app._removed_voice_bindings, {"up": "voice_hold"})
        voice.assert_not_called()
        secondary.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_old_toggle_mapping_disables_mic_and_its_secondary_gestures(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_TOGGLE
        ).to_dict()
        bindings["secondary_bindings"]["mic"] = {
            "long_press": key_mapping.ButtonAction(
                key_mapping.ActionKind.ARROW_UP
            ).to_dict()
        }
        self._save_bindings(bindings)

        with mock.patch.object(win32_input, "send_voice_key_combo_down") as voice, mock.patch.object(
            win32_input, "send_arrow_up"
        ) as secondary:
            self.app._on_button_event("mic", True)
            self.app._on_button_event("mic", False)
            self.app._on_button_trigger(
                "mic", app_module.button_gesture.ButtonTrigger.LONG_PRESS
            )

        self.assertEqual(self.app._removed_voice_bindings, {"mic": "voice_toggle"})
        voice.assert_not_called()
        secondary.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)

    def test_secondary_voice_mapping_disables_the_whole_button(self):
        bindings = config.default_key_bindings()
        bindings["secondary_bindings"]["left"] = {
            "double_click": key_mapping.ButtonAction(
                key_mapping.ActionKind.VOICE_HOLD
            ).to_dict()
        }
        self._save_bindings(bindings)

        with mock.patch.object(win32_input, "send_arrow_left") as primary:
            self.app._on_button_event("left", True, event_source="hid_tap")
            self.app._on_button_event("left", False, event_source="hid_tap")

        self.assertEqual(self.app._removed_voice_bindings, {"left": "voice_hold"})
        primary.assert_not_called()
        self.assertFalse(
            self.app._is_button_action_configured(
                "left", app_module.button_gesture.ButtonTrigger.SINGLE_CLICK
            )
        )

    def test_reselecting_an_ordinary_action_clears_the_removed_marker(self):
        legacy = config.default_key_bindings()
        legacy["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD
        ).to_dict()
        self._save_bindings(legacy)
        self.assertIn("up", self.app._removed_voice_bindings)

        refreshed = config.default_key_bindings()
        refreshed["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()
        self._save_bindings(refreshed)

        with mock.patch.object(win32_input, "send_arrow_up") as action:
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_button_event("up", False, event_source="hid_tap")

        self.assertNotIn("up", self.app._removed_voice_bindings)
        action.assert_called_once_with()

    def test_ordinary_mic_same_source_next_press_is_not_suppressed(self):
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()
        actions = []
        with mock.patch.object(
            win32_input,
            "send_arrow_up",
            side_effect=lambda: actions.append("up"),
        ):
            self.app._on_button_event("mic", True, event_source="hid_tap")
            self.app._on_button_event("mic", False, event_source="hid_tap")
            self.app._on_button_event("mic", True, event_source="hid_tap")
            self.app._on_button_event("mic", False, event_source="hid_tap")

        self.assertEqual(actions, ["up", "up"])

    def test_stale_ordinary_mic_source_cannot_block_the_next_press(self):
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()
        clock = [100.0]
        actions = []

        with mock.patch.object(
            app_module.time,
            "monotonic",
            side_effect=lambda: clock[0],
        ), mock.patch.object(
            win32_input,
            "send_arrow_up",
            side_effect=lambda: actions.append("up"),
        ):
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_button_event("mic", True, event_source="hid_tap")
            clock[0] += app_module._ORDINARY_MIC_SOURCE_STALE_SECONDS + 0.01
            self.app._on_button_event("mic", True, event_source="hid")
            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(actions, ["up", "up"])
        self.assertEqual(self.app._ordinary_mic_sources_down, set())
        self.assertFalse(self.app._ordinary_mic_gesture_active)

    def test_non_mic_legacy_voice_data_is_removed_from_runtime_bindings(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ESCAPE
        ).to_dict()
        bindings["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD
        ).to_dict()
        self._save_bindings(bindings)

        self.assertIn("up", self.app._removed_voice_bindings)

    def test_ordinary_mic_release_finishes_before_voice_mapping_applies(self):
        self.app._bindings["bindings"]["mic"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.ARROW_UP
        ).to_dict()

        with mock.patch.object(win32_input, "send_arrow_up"):
            self.app._on_button_event("mic", True, event_source="hid")

            refreshed = config.default_key_bindings()
            config.save_key_bindings(self.app._bindings_path, refreshed)
            self.app._reload_settings_if_changed()
            self.assertIsNotNone(self.app._pending_bindings)

            self.app._on_button_event("mic", False, event_source="hid")

        self.assertEqual(self.app._ordinary_mic_sources_down, set())
        self.assertFalse(self.app._ordinary_mic_gesture_active)
        self.assertIsNone(self.app._pending_bindings)
        self.assertEqual(
            self.app._bindings["bindings"]["mic"]["kind"],
            key_mapping.ActionKind.VOICE_HOLD.value,
        )

    def test_detected_removed_voice_button_reports_but_does_not_execute(self):
        bindings = config.default_key_bindings()
        bindings["bindings"]["up"] = key_mapping.ButtonAction(
            key_mapping.ActionKind.VOICE_HOLD
        ).to_dict()
        self._save_bindings(bindings)
        request = key_detection_bridge.request_detection(self.app._config_root)

        with mock.patch.object(win32_input, "send_voice_key_combo_down") as voice:
            self.app._on_button_event("up", True, event_source="hid_tap")
            self.app._on_button_event("up", False, event_source="hid_tap")

        self.assertEqual(key_detection_bridge.poll_detection(request), "up")
        voice.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)


class LiveBridgeKeyDetectionTests(_AppWiringTestCase):
    def test_next_ordinary_button_is_reported_and_its_mapping_is_suppressed(self):
        request = key_detection_bridge.request_detection(self.app._config_root)
        with mock.patch.object(self.app._button_gestures, "press") as press, mock.patch.object(
            self.app._button_gestures, "release"
        ) as release:
            self.app._on_button_event("back", True)
            self.app._on_button_event("back", False)

        self.assertEqual(key_detection_bridge.poll_detection(request), "back")
        press.assert_not_called()
        release.assert_not_called()
        self.assertEqual(self.app._key_detection_suppressed_buttons, set())

    def test_raw_keyboard_fallback_can_feed_detection_before_mapping_bypass(self):
        self.app._direct_hid_interception_ready = False
        request = key_detection_bridge.request_detection(self.app._config_root)

        with mock.patch.object(self.app._button_gestures, "press") as press:
            self.app._on_raw_button_event("up", True, "keyboard")
            self.app._on_raw_button_event("up", False, "keyboard")

        self.assertEqual(key_detection_bridge.poll_detection(request), "up")
        press.assert_not_called()
        self.assertEqual(self.app._raw_fallback_buttons_down, set())

    def test_detected_mic_button_never_triggers_host_or_device_voice(self):
        request = key_detection_bridge.request_detection(self.app._config_root)
        with mock.patch.object(win32_input, "send_voice_key_combo_down") as hotkey:
            self.app._on_button_event("mic", True)
            self.app._on_button_event("mic", False)

        self.assertEqual(key_detection_bridge.poll_detection(request), "mic")
        hotkey.assert_not_called()
        self.assertEqual(self.app._ble_session.mic_open_calls, 0)
        self.assertEqual(self.app._ble_session.mic_close_calls, 0)





    def test_detection_suppression_timeout_allows_a_later_real_press(self):
        self.app._key_detection_suppressed_buttons = {"back"}
        self.app._key_detection_suppression_deadlines = {"back": 100.0}

        with mock.patch.object(app_module.time, "monotonic", return_value=101.0), mock.patch.object(
            key_detection_bridge,
            "publish_next_button",
            return_value=False,
        ), mock.patch.object(self.app._button_gestures, "press") as press:
            self.app._on_button_event("back", True, event_source="hid_tap")

        press.assert_called_once_with("back")
        self.assertEqual(self.app._key_detection_suppressed_buttons, set())
        self.app._on_button_event("back", False, event_source="hid_tap")




class InputLifecycleTests(_AppWiringTestCase):

    def test_unavailable_voice_physicalizer_degrades_without_losing_owner_state(self):
        timers = []

        class UnavailablePhysicalizer:
            is_running = False

            def start(self):
                raise (
                    app_module.voice_key_physicalizer_windows.
                    VoiceKeyPhysicalizerUnavailableError("unavailable")
                )

        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            UnavailablePhysicalizer,
        ):
            self.app._voice_key_physicalizer_timer_factory = (
                lambda _delay, callback: timers.append(_ManualTimer(callback))
                or timers[-1]
            )
            self.app._start_voice_key_physicalizer()

        self.assertIsNone(self.app._voice_key_physicalizer)
        self.assertFalse(self.app._voice_key_physicalizer_ready)
        self.assertEqual(self.app._runtime_voice_key_physicalizer_state, "failed")
        self.assertEqual(len(timers), 1)

    def test_still_running_voice_physicalizer_is_retained_for_shutdown(self):
        class StuckPhysicalizer:
            is_running = True

            def start(self):
                raise (
                    app_module.voice_key_physicalizer_windows.
                    VoiceKeyPhysicalizerUnavailableError("stuck")
                )

        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            StuckPhysicalizer,
        ), self.assertRaises(
            app_module.voice_key_physicalizer_windows.
            VoiceKeyPhysicalizerUnavailableError
        ):
            self.app._start_voice_key_physicalizer()

        self.assertIsInstance(
            self.app._voice_key_physicalizer,
            StuckPhysicalizer,
        )

    def test_ready_then_immediate_exit_never_publishes_ready(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer(immediate_exit=True)
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            return_value=physicalizer,
        ):
            self.app._start_voice_key_physicalizer()

        self.assertFalse(self.app._voice_key_physicalizer_ready)
        self.assertEqual(
            self.app._runtime_voice_key_physicalizer_state,
            "recovering",
        )
        self.assertEqual(len(timers), 1)

    def test_repeated_start_failures_keep_one_recovery_timer(self):
        timers = []
        instances = []

        def build_physicalizer():
            instance = _FakeVoicePhysicalizer(
                start_error=(
                    app_module.voice_key_physicalizer_windows.
                    VoiceKeyPhysicalizerUnavailableError("failed")
                )
            )
            instances.append(instance)
            return instance

        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            side_effect=build_physicalizer,
        ):
            self.app._start_voice_key_physicalizer()
            with self.app._voice_key_physicalizer_lifecycle_lock:
                self.app._schedule_voice_key_physicalizer_recovery_locked()
            self.assertEqual(len(timers), 1)
            timers[0].fire()

        self.assertEqual(len(instances), 2)
        self.assertEqual(len(timers), 2)
        self.assertIsNone(self.app._voice_key_physicalizer)

    def test_normal_shutdown_cancels_recovery_and_never_restarts(self):
        timers = []
        instances = []

        def build_physicalizer():
            instance = _FakeVoicePhysicalizer(
                start_error=(
                    app_module.voice_key_physicalizer_windows.
                    VoiceKeyPhysicalizerUnavailableError("failed")
                )
            )
            instances.append(instance)
            return instance

        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            side_effect=build_physicalizer,
        ):
            self.app._start_voice_key_physicalizer()
            self.app._stop_input_channels()
            timers[0].fire()

        self.assertEqual(len(instances), 1)
        self.assertTrue(timers[0].cancelled)
        self.assertEqual(
            self.app._runtime_voice_key_physicalizer_state,
            "stopped",
        )

    def test_stale_recovery_timer_cannot_stop_replacement_generation(self):
        timers = []
        old = _FakeVoicePhysicalizer(accepts_new_down=False)
        old.is_running = True
        self.app._voice_key_physicalizer = old
        self.app._voice_key_physicalizer_generation = 1
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()

        replacement = _FakeVoicePhysicalizer(accepts_new_down=False)
        replacement.is_running = True
        self.app._voice_key_physicalizer = replacement
        self.app._voice_key_physicalizer_generation = 2
        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
        ) as build:
            timers[0].fire()

        self.assertEqual(replacement.stop_calls, 0)
        self.assertIs(self.app._voice_key_physicalizer, replacement)
        build.assert_not_called()

    def test_required_confirmation_failure_closes_admission_and_schedules_recovery(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer(accepts_new_down=False)
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 7
        self.app._voice_key_physicalizer_ready = True
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        snapshot = mock.Mock(
            generation=11,
            receipt_callback_entry_delta=0,
            receipt_marker_callback_delta=0,
        )
        snapshot.trace_fields.return_value = {}

        self.app._on_voice_key_physicalizer_health_failure(
            physicalizer,
            7,
            "required_confirmation_timeout",
            snapshot,
        )

        self.assertFalse(self.app._voice_key_physicalizer_ready)
        self.assertEqual(
            self.app._runtime_voice_key_physicalizer_state,
            "recovering",
        )
        self.assertEqual(len(timers), 1)

    def test_tracking_lost_owner_still_running_never_republishes_ready(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer()
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 8
        self.app._voice_key_physicalizer_lost_generation = 8
        self.app._voice_key_physicalizer_ready = False
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()

        timers[0].fire()

        self.assertFalse(self.app._voice_key_physicalizer_ready)
        self.assertEqual(physicalizer.stop_calls, 0)
        self.assertEqual(len(timers), 2)

    def test_degraded_owner_waits_for_cleanup_then_restarts_once(self):
        timers = []
        old = _FakeVoicePhysicalizer(accepts_new_down=False)
        old.is_running = True
        replacement = _FakeVoicePhysicalizer()
        self.app._voice_key_physicalizer = old
        self.app._voice_key_physicalizer_generation = 4
        self.app._voice_key_physicalizer_ready = False
        self.app._voice_shortcut.pending_tokens = ("ralt",)
        self.app._voice_key_physicalizer_degraded_retry_deadline = (
            time.monotonic() + 10.0
        )
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()

        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            return_value=replacement,
        ) as build:
            timers[0].fire()
            self.assertEqual(old.stop_calls, 0)
            self.assertEqual(len(timers), 2)
            self.app._voice_shortcut.pending_tokens = None
            timers[1].fire()

        self.assertEqual(old.stop_calls, 1)
        self.assertEqual(replacement.start_calls, 1)
        self.assertIs(self.app._voice_key_physicalizer, replacement)
        self.assertTrue(self.app._voice_key_physicalizer_ready)
        build.assert_called_once_with()

    def test_persistent_cleanup_debt_enters_failed_state_without_restart_loop(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer(accepts_new_down=False)
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 3
        self.app._voice_key_physicalizer_ready = False
        self.app._voice_shortcut.pending_tokens = ("ralt",)
        self.app._voice_key_physicalizer_degraded_retry_deadline = (
            time.monotonic() - 0.1
        )
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()

        timers[0].fire()

        self.assertEqual(physicalizer.stop_calls, 0)
        self.assertEqual(len(timers), 1)
        self.assertEqual(
            self.app._runtime_voice_key_physicalizer_state,
            "failed",
        )

    def test_late_direct_key_up_resumes_failed_degraded_owner_recovery(self):
        timers = []
        old = _FakeVoicePhysicalizer(accepts_new_down=False)
        old.is_running = True
        replacement = _FakeVoicePhysicalizer()
        self.app._voice_key_physicalizer = old
        self.app._voice_key_physicalizer_generation = 9
        self.app._voice_key_physicalizer_ready = False
        self.app._voice_key_physicalizer_degraded_retry_deadline = (
            time.monotonic() - 0.1
        )
        self.app._voice_shortcut.pending_tokens = None
        self.app._voice_shortcut.controller.on_mic_button_pressed()
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()

        timers[0].fire()

        self.assertTrue(self.app._voice_shortcut.controller.active)
        self.assertIsNone(self.app._voice_shortcut.pending_tokens)
        self.assertEqual(
            self.app._runtime_voice_key_physicalizer_state,
            "failed",
        )
        self.assertEqual(len(timers), 1)

        with mock.patch.object(
            win32_input,
            "send_voice_key_combo_up",
        ) as send_up, mock.patch.object(
            win32_input,
            "send_voice_key_combo_down",
        ) as send_down, mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            return_value=replacement,
        ):
            with self.app._voice_shortcut.lock:
                self.assertTrue(
                    self.app._release_hold_voice_on_physical_release_locked(
                        "late physical release"
                    )
                )
            self.assertEqual(len(timers), 2)
            timers[1].fire()

        send_up.assert_called_once_with(DEFAULT_VOICE_TOKENS)
        send_down.assert_not_called()
        self.assertEqual(old.stop_calls, 1)
        self.assertEqual(replacement.start_calls, 1)
        self.assertEqual(replacement.stop_calls, 0)
        self.assertIs(self.app._voice_key_physicalizer, replacement)
        self.assertTrue(self.app._voice_key_physicalizer_ready)

    def test_replacement_start_failure_replaces_retired_generation_timer(self):
        timers = []
        retired = _FakeVoicePhysicalizer(accepts_new_down=False)
        failed_replacement = _FakeVoicePhysicalizer(
            start_error=(
                app_module.voice_key_physicalizer_windows.
                VoiceKeyPhysicalizerUnavailableError("failed")
            )
        )
        ready_replacement = _FakeVoicePhysicalizer()
        self.app._voice_key_physicalizer = retired
        self.app._voice_key_physicalizer_generation = 12
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()
        retired_callback = timers[0].callback

        retired.is_running = False
        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
            side_effect=[failed_replacement, ready_replacement],
        ):
            self.app._start_voice_key_physicalizer(recovering=True)
            self.assertTrue(timers[0].cancelled)
            self.assertEqual(len(timers), 2)
            self.assertIs(
                self.app._voice_key_physicalizer_retry_timer,
                timers[1],
            )

            retired_callback()
            self.assertIs(
                self.app._voice_key_physicalizer_retry_timer,
                timers[1],
            )
            timers[1].fire()
            retired_callback()

        self.assertEqual(failed_replacement.start_calls, 1)
        self.assertEqual(ready_replacement.start_calls, 1)
        self.assertEqual(ready_replacement.stop_calls, 0)
        self.assertIs(self.app._voice_key_physicalizer, ready_replacement)
        self.assertTrue(self.app._voice_key_physicalizer_ready)

    def test_failed_degraded_owner_stop_never_starts_replacement(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer(
            accepts_new_down=False,
            stop_error=RuntimeError("stuck"),
        )
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 5
        self.app._voice_key_physicalizer_ready = False
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._voice_key_physicalizer_lifecycle_lock:
            self.app._schedule_voice_key_physicalizer_recovery_locked()

        with mock.patch.object(
            app_module.voice_key_physicalizer_windows,
            "VoiceKeyPhysicalizer",
        ) as build:
            timers[0].fire()

        self.assertEqual(physicalizer.stop_calls, 1)
        self.assertIs(self.app._voice_key_physicalizer, physicalizer)
        self.assertEqual(
            self.app._runtime_voice_key_physicalizer_state,
            "failed",
        )
        build.assert_not_called()

    def test_repeated_raw_start_failures_keep_one_capped_backoff_timer(self):
        timers = []

        def timer_factory(delay, callback):
            timer = _ManualTimer(callback)
            timer.delay = delay
            timers.append(timer)
            return timer

        self.app._raw_input_timer_factory = timer_factory
        listener = _FakeRecoveringRawListener(
            start_error=raw_input_windows.RawInputUnavailableError("no device"),
        )
        with mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
            side_effect=lambda *args, **kwargs: listener,
        ):
            self.app._start_hid_listener()
            with self.app._raw_input_lifecycle_lock:
                self.app._schedule_raw_input_recovery_locked()
            self.assertEqual(len(timers), 1)
            for _index in range(6):
                timers[-1].fire()

        self.assertEqual(
            [timer.delay for timer in timers],
            [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0],
        )

    def test_raw_recovery_stop_failure_retains_old_listener_and_never_starts_second(self):
        timers = []
        old_listener = _FakeRecoveringRawListener(
            stop_error=raw_input_windows.RawInputUnavailableError("stuck")
        )
        old_listener.is_running = True
        self.app._hid_listener = old_listener
        self.app._raw_input_generation = 4
        self.app._raw_input_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._raw_input_lifecycle_lock:
            self.app._schedule_raw_input_recovery_locked()

        with mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
        ) as build_listener, mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
        ) as enumerate_paths:
            timers[0].fire()

        self.assertEqual(old_listener.stop_calls, 1)
        self.assertIs(self.app._hid_listener, old_listener)
        build_listener.assert_not_called()
        enumerate_paths.assert_not_called()
        self.assertEqual(len(timers), 2)
        # The keyboard safety tracker is auxiliary on the only shipped profile:
        # its state is logged, never published, so a stuck stop must not
        # overwrite the receiver's own device status.
        self.assertEqual(self.app._runtime_raw_input_state, "starting")

    def test_physical_tracking_loss_schedules_listener_replacement(self):
        timers = []
        old_listener = _FakeRecoveringRawListener()
        old_listener.is_running = True
        new_listener = _FakeRecoveringRawListener()
        self.app._hid_listener = old_listener
        self.app._raw_input_generation = 4
        self.app._raw_input_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )

        self.app._on_physical_keyboard_tracking_lost(
            "physical_keyboard_edge_mismatch",
            _listener=old_listener,
            _generation=4,
        )

        self.assertEqual(self.app._raw_input_lost_generation, 4)
        # Unhealthy is the tracker's own verdict and stays internal: publishing
        # it would replace the receiver's device status (chromecast_ready).
        self.assertEqual(self.app._runtime_raw_input_state, "starting")
        self.assertEqual(len(timers), 1)

        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["fake-path"],
        ), mock.patch.object(
            hid_identity,
            "select_single_device_path",
            return_value="fake-path",
        ), mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
            return_value=new_listener,
        ):
            timers[0].fire()

        self.assertEqual(old_listener.stop_calls, 1)
        self.assertEqual(new_listener.start_calls, 1)
        self.assertIs(self.app._hid_listener, new_listener)
        # Replacement is real, but the replacement is a keyboard-safety detail
        # the receiver does not publish on this profile.
        self.assertEqual(self.app._runtime_raw_input_state, "starting")

    def test_inflight_raw_recovery_cannot_restart_after_final_shutdown(self):
        stop_started = threading.Event()
        allow_stop = threading.Event()
        timers = []

        class BlockingStopListener(_FakeRecoveringRawListener):
            def stop(self):
                self.stop_calls += 1
                stop_started.set()
                allow_stop.wait(1.0)
                self.is_running = False

        old_listener = BlockingStopListener()
        old_listener.is_running = True
        self.app._hid_listener = old_listener
        self.app._raw_input_generation = 4
        self.app._raw_input_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        with self.app._raw_input_lifecycle_lock:
            self.app._schedule_raw_input_recovery_locked()
        recovery_worker = threading.Thread(target=timers[0].fire)
        shutdown_worker = threading.Thread(target=self.app._stop_input_channels)

        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
        ) as enumerate_paths, mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
        ) as build_listener:
            recovery_worker.start()
            self.assertTrue(stop_started.wait(1.0))
            shutdown_worker.start()
            allow_stop.set()
            recovery_worker.join(1.0)
            shutdown_worker.join(1.0)

        self.assertFalse(recovery_worker.is_alive())
        self.assertFalse(shutdown_worker.is_alive())
        self.assertEqual(old_listener.stop_calls, 1)
        enumerate_paths.assert_not_called()
        build_listener.assert_not_called()
        self.assertTrue(self.app._raw_input_stopping)
        self.assertFalse(self.app._accept_input_events)
        self.assertIsNone(self.app._hid_listener)
        self.assertIsNone(self.app._raw_input_retry_timer)

    def test_raw_shutdown_cancels_recovery_and_never_restarts(self):
        timers = []

        def timer_factory(_delay, callback):
            timer = _ManualTimer(callback)
            timers.append(timer)
            return timer

        self.app._raw_input_timer_factory = timer_factory
        listener = _FakeRecoveringRawListener(
            start_error=raw_input_windows.RawInputUnavailableError("no device"),
        )
        with mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
            side_effect=lambda *args, **kwargs: listener,
        ):
            self.app._start_hid_listener()
            self.app._stop_input_channels()
            timers[0].fire()

        self.assertTrue(timers[0].cancelled)
        self.assertEqual(listener.start_calls, 1)
        self.assertIsNone(self.app._raw_input_retry_timer)
        self.assertFalse(self.app._accept_input_events)

    def test_uncontrolled_recovery_listener_is_retained_and_retried(self):
        timers = []
        listener = _FakeRecoveringRawListener(
            start_error=raw_input_windows.RawInputUnavailableError("stuck"),
            running_after_error=True,
        )

        def timer_factory(delay, callback):
            timer = _ManualTimer(callback)
            timer.delay = delay
            timers.append(timer)
            return timer

        self.app._raw_input_timer_factory = timer_factory
        with self.app._raw_input_lifecycle_lock:
            self.app._schedule_raw_input_recovery_locked()
        with mock.patch.object(
            app_module.raw_input_windows,
            "enumerate_matching_device_paths",
            return_value=["fake-path"],
        ), mock.patch.object(
            hid_identity,
            "select_single_device_path",
            return_value="fake-path",
        ), mock.patch.object(
            app_module.raw_input_windows,
            "RawInputButtonListener",
            return_value=listener,
        ):
            timers[0].fire()

        self.assertIs(self.app._hid_listener, listener)
        self.assertEqual(listener.start_calls, 1)
        self.assertEqual(len(timers), 2)
        self.assertEqual([timer.delay for timer in timers], [1.0, 2.0])

    def test_stale_physicalizer_callback_cannot_replace_current_state(self):
        timers = []
        old = _FakeVoicePhysicalizer()
        current = _FakeVoicePhysicalizer()
        current.is_running = True
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        self.app._voice_key_physicalizer = current
        self.app._voice_key_physicalizer_generation = 2
        self.app._voice_key_physicalizer_ready = True

        with mock.patch.object(
            self.app,
            "_force_voice_hold_release_locked",
        ) as release_voice, mock.patch.object(
            self.app,
            "_release_pending_button_keys",
        ) as release_buttons:
            self.app._on_voice_key_physicalizer_tracking_lost(old, 1)

        self.assertTrue(self.app._voice_key_physicalizer_ready)
        self.assertEqual(timers, [])
        release_voice.assert_not_called()
        release_buttons.assert_not_called()

    def test_physicalizer_loss_cancels_chromecast_voice_and_schedules_recovery(self):
        timers = []
        host = _FakeChromecastVoiceHost()
        physicalizer = _FakeVoicePhysicalizer()
        physicalizer.is_running = True
        self.app._chromecast_runtime.voice_host = host
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 1
        self.app._voice_key_physicalizer_ready = True
        self.app._voice_shortcut.hotkey = app_module.hotkey.HotkeySpec.parse("ralt")
        self.app._voice_shortcut.pending_tokens = ("ralt",)
        self.app._voice_shortcut.pending_backend = (
            app_module._VOICE_HOTKEY_BACKEND_MARKED
        )
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )

        with mock.patch.object(
            self.app,
            "_force_voice_hold_release_locked",
            return_value=True,
        ) as release_voice, mock.patch.object(
            self.app,
            "_release_pending_button_keys",
        ) as release_buttons:
            self.app._on_voice_key_physicalizer_tracking_lost(
                physicalizer,
                1,
            )

        # Voice is owned by the receiver's host on the only shipped profile, so
        # losing the marker cancels that recording instead of force-releasing a
        # synthetic key hold the app never sent.
        self.assertEqual(host.cancel_calls, 1)
        release_voice.assert_not_called()
        release_buttons.assert_not_called()
        self.assertFalse(self.app._voice_key_physicalizer_ready)
        self.assertEqual(self.app._voice_key_physicalizer_lost_generation, 1)
        self.assertEqual(len(timers), 1)

    def test_physicalizer_loss_with_healthy_raw_preserves_other_combos(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer()
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 1
        self.app._voice_key_physicalizer_ready = True
        self.app._voice_shortcut.hotkey = app_module.hotkey.HotkeySpec.parse("lctrl+f9")
        self.app._voice_shortcut.pending_tokens = ("lctrl", "f9")
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )

        with mock.patch.object(
            self.app,
            "_force_voice_hold_release_locked",
        ) as release_voice, mock.patch.object(
            self.app,
            "_release_pending_button_keys",
        ) as release_buttons:
            self.app._on_voice_key_physicalizer_tracking_lost(
                physicalizer,
                1,
            )

        release_voice.assert_not_called()
        release_buttons.assert_not_called()

    def test_second_tracker_loss_cancels_voice_and_leaves_button_keys_alone(self):
        timers = []
        host = _FakeChromecastVoiceHost()
        physicalizer = _FakeVoicePhysicalizer()
        physicalizer.is_running = True
        self.app._chromecast_runtime.voice_host = host
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 1
        self.app._voice_key_physicalizer_ready = True
        self.app._voice_shortcut.pending_tokens = ("lctrl", "lwin")
        self.app._button_key_release_pending = ("lctrl", "lwin")
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        raw_input_windows._set_physical_keyboard_tracker_active(False)

        with mock.patch.object(
            self.app,
            "_force_voice_hold_release_locked",
            return_value=True,
        ) as release_voice, mock.patch.object(
            self.app,
            "_release_pending_button_keys",
            return_value=True,
        ) as release_buttons:
            self.app._on_voice_key_physicalizer_tracking_lost(
                physicalizer,
                1,
            )

        # One cancel, not one per tracked owner: the pending button combo is
        # not a voice hold, so it is not released by this path at all.
        self.assertEqual(host.cancel_calls, 1)
        release_voice.assert_not_called()
        release_buttons.assert_not_called()
        self.assertEqual(len(timers), 1)

    def test_dual_tracker_loss_without_owned_hold_sends_no_key_up(self):
        timers = []
        physicalizer = _FakeVoicePhysicalizer()
        physicalizer.is_running = True
        self.app._voice_key_physicalizer = physicalizer
        self.app._voice_key_physicalizer_generation = 1
        self.app._voice_key_physicalizer_ready = True
        self.app._voice_key_physicalizer_timer_factory = (
            lambda _delay, callback: timers.append(_ManualTimer(callback))
            or timers[-1]
        )
        raw_input_windows._set_physical_keyboard_tracker_active(False)

        with mock.patch.object(
            self.app,
            "_force_voice_hold_release_locked",
        ) as release_voice, mock.patch.object(
            win32_input,
            "send_key_combo_up",
        ) as send_up:
            self.app._on_voice_key_physicalizer_tracking_lost(
                physicalizer,
                1,
            )

        release_voice.assert_not_called()
        send_up.assert_not_called()

    def test_raw_tracker_loss_retries_pending_combo_even_with_physicalizer_ready(self):
        self.app._voice_key_physicalizer_ready = True
        self.app._button_key_release_pending = ("lctrl", "f9")

        with mock.patch.object(win32_input, "send_key_combo_up") as send_up:
            self.app._on_physical_keyboard_tracking_lost("raw_failed")

        send_up.assert_called_once_with(("lctrl", "f9"))
        self.assertIsNone(self.app._button_key_release_pending)

    def test_raw_tracker_loss_without_button_debt_sends_no_button_key_up(self):
        with mock.patch.object(win32_input, "send_key_combo_up") as send_up:
            self.app._on_physical_keyboard_tracking_lost("raw_failed")

        send_up.assert_not_called()

    def test_final_input_shutdown_stops_the_surviving_owners(self):
        # Chromecast's HID tap belongs to its worker process, so app.py owns
        # only the Raw Input keyboard-safety listener and the voice
        # physicalizer.
        raw = _FakeHidListener()
        physicalizer = _FakeInputOwner()
        self.app._hid_listener = raw
        self.app._voice_key_physicalizer = physicalizer

        self.app._stop_input_channels()

        self.assertEqual(raw.stop_calls, 1)
        self.assertEqual(physicalizer.stop_calls, 1)
        self.assertIsNone(self.app._hid_listener)
        self.assertIsNone(self.app._voice_key_physicalizer)
        self.assertFalse(self.app._accept_input_events)

    def test_final_input_shutdown_cancels_release_retry_before_final_key_up(self):
        self.app._button_key_release_pending = ("up",)
        with self.app._button_action_lock:
            self.app._schedule_button_input_release_retry_locked()
        retry_timer = self._button_input_release_timers[0]

        with mock.patch.object(win32_input, "send_key_combo_up") as send_up:
            self.app._stop_input_channels()
            retry_timer.fire()

        self.assertTrue(retry_timer.cancelled)
        send_up.assert_called_once_with(("up",))
        self.assertIsNone(self.app._button_key_release_pending)
        self.assertIsNone(self.app._button_input_release_retry_timer)


class CleanupOwnershipTests(_AppWiringTestCase):
    """BLE retries clean BLE/audio state without tearing down input."""


    def test_input_shutdown_failure_retains_hid_owner(self):
        hid = _FakeHidListener(stop_raises=True)
        self.app._hid_listener = hid

        with self.assertRaises(app_module.CleanupIncompleteError) as ctx:
            self.app._stop_input_channels()
        self.assertIn("Raw Input listener", str(ctx.exception))

        self.assertIs(self.app._hid_listener, hid)
        self.assertFalse(self.app._accept_input_events)


class StartHidListenerOwnershipTests(_AppWiringTestCase):
    """RawInputButtonListener intentionally
    retains its thread/window when its own bounded failed-start cleanup
    cannot stop them (see raw_input_windows.py's ``_abandon_failed_start``).
    ``_start_hid_listener()`` must consult ``is_running`` rather than
    unconditionally clearing ``self._hid_listener`` to ``None`` on any
    failed ``start()`` - doing so would lose the owner and let a later
    ``_connect_once()`` generation start a second listener over a still-
    live one (the exact defect class this eliminates; see
    also CleanupOwnershipTests' end-to-end supervisor test above, which
    proves no second connect() generation is ever reached once cleanup
    itself fails on a retained owner).
    """

    def _patch_device_discovery(self, fake_listener):
        original_enumerate = app_module.raw_input_windows.enumerate_matching_device_paths
        original_select = hid_identity.select_single_device_path
        original_listener_cls = app_module.raw_input_windows.RawInputButtonListener
        app_module.raw_input_windows.enumerate_matching_device_paths = lambda **_kwargs: ["fake-path"]
        hid_identity.select_single_device_path = lambda paths: paths[0]
        app_module.raw_input_windows.RawInputButtonListener = lambda callback: fake_listener

        def _restore():
            app_module.raw_input_windows.enumerate_matching_device_paths = original_enumerate
            hid_identity.select_single_device_path = original_select
            app_module.raw_input_windows.RawInputButtonListener = original_listener_cls

        return _restore

    def test_a_failed_start_that_is_still_running_retains_the_owner_and_raises(self):
        fake_listener = _FakeHidListenerForFailedStart(is_running_after_failed_start=True)
        restore = self._patch_device_discovery(fake_listener)
        try:
            with self.assertRaises(app_module.raw_input_windows.RawInputUnavailableError):
                self.app._start_hid_listener()
        finally:
            restore()

        self.assertIs(self.app._hid_listener, fake_listener)
        self.assertEqual(fake_listener.start_calls, 1)

    def test_a_failed_start_confirmed_stopped_clears_the_owner(self):
        fake_listener = _FakeHidListenerForFailedStart(is_running_after_failed_start=False)
        restore = self._patch_device_discovery(fake_listener)
        try:
            self.app._start_hid_listener()  # must not raise
        finally:
            restore()

        self.assertIsNone(self.app._hid_listener)
        self.assertEqual(fake_listener.start_calls, 1)






class LoggingHandlerCleanupRegressionTests(unittest.TestCase):
    """Proves _AppWiringTestCase's
    tearDown fix (close/remove the FileHandler, reset ``_configured``)
    actually decouples one app build's logging handler from the next -
    the exact defect that made a real Windows CI runner's
    ``tempfile.TemporaryDirectory().cleanup()`` raise a PermissionError
    on the very first test the suite's discovery order ever runs
    (``CleanupOwnershipTests.test_ble_close_failure_retains_ble_owner_but_
    still_completes_hid_and_playback``): ``logging_setup.get_logger()``
    configures its FileHandler exactly once per process and never closes
    it, so without this cleanup the handle stays open inside that first
    test's temp directory for the rest of the run - and Windows, unlike
    POSIX, refuses to delete a directory containing a still-open handle.
    """

    def test_a_second_app_build_gets_its_own_fresh_handler_after_cleanup(self):
        tmp1 = tempfile.TemporaryDirectory()
        loop1 = None
        try:
            # _build_app_with_owned_loop() (not the bare _build_app()): this
            # test constructs ChromecastApp synchronously, same as
            # _AppWiringTestCase.setUp() - see
            # EventLoopOwnershipRegressionTests for why a bare
            # asyncio.get_event_loop() call here would leak too.
            _, loop1 = _build_app_with_owned_loop(Path(tmp1.name))
            logger = logging.getLogger(logging_setup.LOGGER_NAME)
            self.assertEqual(len(logger.handlers), 1)
            handler1 = logger.handlers[0]
            self.assertEqual(
                Path(handler1.baseFilename).parent, Path(tmp1.name) / "logs"
            )
            # App logging is asynchronous; wait for the queued startup record
            # before inspecting its worker-owned stream.
            self.assertTrue(handler1.flush_pending())
            self.assertIsNotNone(handler1.stream)

            # Exactly what _AppWiringTestCase.tearDown now does.
            handler1.close()
            logger.removeHandler(handler1)
            logging_setup._configured = False

            self.assertIsNone(handler1.stream)
            self.assertEqual(logger.handlers, [])
        finally:
            asyncio.set_event_loop(None)
            if loop1 is not None:
                loop1.close()
            for handler in list(logging.getLogger(logging_setup.LOGGER_NAME).handlers):
                handler.close()
                logging.getLogger(logging_setup.LOGGER_NAME).removeHandler(handler)
            logging_setup._configured = False
            # Must not raise: on Windows this would be the PermissionError
            # from outcome 1 if the handle above were still open.
            tmp1.cleanup()

        tmp2 = tempfile.TemporaryDirectory()
        loop2 = None
        try:
            _, loop2 = _build_app_with_owned_loop(Path(tmp2.name))
            logger = logging.getLogger(logging_setup.LOGGER_NAME)
            self.assertEqual(len(logger.handlers), 1)
            handler2 = logger.handlers[0]
            self.assertIsNot(handler2, handler1)
            self.assertEqual(
                Path(handler2.baseFilename).parent, Path(tmp2.name) / "logs"
            )

            handler2.close()
            logger.removeHandler(handler2)
            logging_setup._configured = False
        finally:
            asyncio.set_event_loop(None)
            if loop2 is not None:
                loop2.close()
            tmp2.cleanup()


class EventLoopOwnershipRegressionTests(unittest.TestCase):
    """Regression for real Windows run 29644660267:
    425 tests passed ("OK (skipped=3)"), then the process printed an ignored
    "unclosed event loop" ResourceWarning for a ProactorEventLoop plus two
    unclosed self-pipe sockets - AFTER unittest's own summary, so
    -W error::ResourceWarning never saw it and the step still exited 0.

    Root cause: ChromecastApp.__init__ builds a ChromecastRuntime, whose
    __init__ captures ``loop or asyncio.get_event_loop()``
    (google_runtime.py). _build_app() runs synchronously in
    _AppWiringTestCase.setUp(), off any running loop - unlike the real app,
    which only ever constructs ChromecastApp inside ``asyncio.run(_run())``
    (app.py), where get_event_loop() correctly returns asyncio.run()'s own
    loop. With no running loop and nothing set for this thread,
    asyncio.get_event_loop() silently creates and caches an implicit
    default loop - shared by every _AppWiringTestCase subclass's setUp() -
    that nothing in the old test suite ever closed.

    These tests prove both halves of the fix: (1) the fixed setUp()/
    tearDown() pattern threads a per-test OWNED loop into ChromecastRuntime
    instead of that ambient default, and (2) deterministically forcing the
    exact condition real interpreter shutdown eventually creates (every
    strong reference to a loop dropped, including asyncio's own thread-local
    cache, then a GC pass) reproduces the red evidence exactly for the OLD
    pattern while the FIXED pattern never reproduces it - in an isolated
    subprocess, so this test process's own asyncio/event-loop state is never
    touched either way.
    """

    def test_unowned_default_loop_pattern_reproduces_the_exact_red_evidence(self):
        # The OLD (pre-fix) construction pattern - asyncio.get_event_loop()
        # with no owned/running loop - run in an isolated subprocess, then
        # forced through the exact condition real interpreter shutdown
        # eventually creates. This deterministically reproduces the red
        # evidence's exact shape: one "unclosed event loop" plus two
        # "unclosed <socket.socket" warnings, both printed as unraisable
        # exceptions from inside __del__, while the script's own exit code
        # still reports 0 - proving why -W error::ResourceWarning alone
        # could never have caught it.
        # On Python 3.14+ a bare asyncio.get_event_loop() with no current loop
        # raises RuntimeError instead of implicitly creating one, so the old
        # pattern cannot be constructed as written. What this test is really
        # about is the state that pattern produced - an event loop nobody
        # owns and nobody closes - and that is still reproducible there by
        # creating the loop explicitly and then dropping the policy's
        # reference to it. Older interpreters keep the original script
        # verbatim, so the historical shape is still what gets asserted.
        if sys.version_info >= (3, 14):
            prologue = "asyncio.set_event_loop(asyncio.new_event_loop())\n"
        else:
            prologue = ""
        script = (
            "import asyncio, gc\n"
            "class _Sup:\n"
            "    def __init__(self):\n"
            "        self._loop = asyncio.get_event_loop()\n"
            + prologue +
            "objs = [_Sup() for _ in range(3)]\n"
            "assert all(o._loop is objs[0]._loop for o in objs)\n"
            "del objs\n"
            "asyncio.get_event_loop_policy()._local._loop = None\n"
            "gc.collect()\n"
            "print('done')\n"
        )
        result = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("unclosed event loop", result.stderr)
        self.assertEqual(result.stderr.count("unclosed <socket.socket"), 2)

    def test_owned_and_closed_loop_pattern_never_reproduces_the_red_evidence(self):
        # Same forced-shutdown stress as the test above, but using the FIXED
        # pattern (_AppWiringTestCase.setUp()/tearDown()'s own approach: a
        # fresh loop is created, set current, then explicitly closed)
        # instead of the bare default-loop getter - proving the fix, not
        # just the bug.
        script = (
            "import asyncio, gc\n"
            "class _Sup:\n"
            "    def __init__(self, loop=None):\n"
            "        self._loop = loop or asyncio.get_event_loop()\n"
            "def _build_owned():\n"
            "    loop = asyncio.new_event_loop()\n"
            "    asyncio.set_event_loop(loop)\n"
            "    sup = _Sup()\n"
            "    asyncio.set_event_loop(None)\n"
            "    loop.close()\n"
            "    return sup\n"
            "objs = [_build_owned() for _ in range(3)]\n"
            "del objs\n"
            "asyncio.get_event_loop_policy()._local._loop = None\n"
            "gc.collect()\n"
            "print('done')\n"
        )
        result = subprocess.run(
            [sys.executable, "-W", "error::ResourceWarning", "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0)
        self.assertNotIn("ResourceWarning", result.stderr)
        self.assertNotIn("unclosed", result.stderr)


async def _record_connect(connect_calls):
    connect_calls.append(1)


if __name__ == "__main__":
    unittest.main()
