"""Entry point wiring: connects the pieces into a running remote-control client.

Windows-only end-to-end. NOT exercised against real hardware in this
candidate - no device pairing/control happens anywhere in this repository or
its tests, per the project's hard boundary. See this package's top-level
README.md "Known gaps" section for what remains 待核验 (to be verified).

Scope: only one remote profile ships, so this module carries the Chromecast
path alone, whose receiver owns its own lifecycle inside
``chromecast_runtime.ChromecastRuntime``.

Input ownership: Raw Input, the marked voice-key physicalizer, and the button
release trackers belong to the bridge-worker lifetime. They are shared safety
infrastructure, not part of any single remote's protocol, so a receiver loss
must not turn ordinary button handling off. Cleanup follows one rule
throughout  an owner reference is cleared only after that
resource confirms it stopped, and a failure to confirm leaves the owner in
place rather than starting a new session over a live one.

Voice fail-closed ordering 
the host hotkey is only sent once the voice path has fully delivered; a partial
delivery is rolled back instead of being left half-applied. A playback write
failure also fails closed rather than logging indefinitely. Blocking writes run
on a bounded FIFO worker so ordinary button handling never waits on them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
import threading
import time
from typing import List, Optional, Sequence, Tuple

from . import __version__
from . import (
    voice_audio_session,
    voice_shortcut_session,
    action_executor,
    bridge_launcher,
    bridge_runtime_status,
    bridge_tray_windows,
    button_combo,
    button_gesture,
    chromecast_host_activity,
    chromecast_runtime,
    config,
    diagnostic_trace,
    doubao_rpc,
    element_navigation_control_windows,
    element_navigation_runtime,
    hotkey,
    key_detection_bridge,
    key_mapping,
    logging_setup,
    physical_key_tristate_windows,
    raw_input_windows,
    remote_selection,
    system_playback_mute,
    voice_key_physicalizer_windows,
    voice_controller,
    voice_interaction_diagnostics_windows,
    voice_playback_session_windows,
    voice_program_manager,
    wetype_control_windows,
    win32_input,
    win32_keys,
)
from .voice_shortcut_session import (
    _VOICE_HOTKEY_BACKEND_MARKED,
    _VOICE_HOTKEY_BACKEND_CHATTERFLY,
    _VOICE_HOTKEY_BACKEND_WETYPE,
    _VOICE_HOTKEY_BACKEND_DOUBAO,
)


class CleanupIncompleteError(RuntimeError):
    """Raised when an owned BLE/audio or process-input resource stays live."""


_BUTTON_ACTION_KEY_TOKENS = {
    key_mapping.ActionKind.ESCAPE: ("escape",),
    key_mapping.ActionKind.RETURN: ("enter",),
    key_mapping.ActionKind.ARROW_UP: ("up",),
    key_mapping.ActionKind.ARROW_DOWN: ("down",),
    key_mapping.ActionKind.ARROW_LEFT: ("left",),
    key_mapping.ActionKind.ARROW_RIGHT: ("right",),
    key_mapping.ActionKind.DELETE_BACKWARD: ("backspace",),
    key_mapping.ActionKind.SHOW_DESKTOP: ("win", "m"),
    key_mapping.ActionKind.CONTEXT_MENU: ("apps",),
    key_mapping.ActionKind.APP_SWITCHER: ("ctrl", "alt", "tab"),
    key_mapping.ActionKind.SYSTEM_VOLUME_UP: ("volume_up",),
    key_mapping.ActionKind.SYSTEM_VOLUME_DOWN: ("volume_down",),
    key_mapping.ActionKind.SYSTEM_VOLUME_MUTE: ("volume_mute",),
    key_mapping.ActionKind.PLAY_PAUSE: ("media_play_pause",),
}

_BUTTON_ACTION_MOUSE_BUTTONS = {
    key_mapping.ActionKind.MOUSE_LEFT_CLICK: "left",
    key_mapping.ActionKind.MOUSE_RIGHT_CLICK: "right",
    key_mapping.ActionKind.MOUSE_MIDDLE_CLICK: "middle",
    key_mapping.ActionKind.MOUSE_X1_CLICK: "x1",
    key_mapping.ActionKind.MOUSE_X2_CLICK: "x2",
}

# Keep the first-repeat delay shared, but make ordinary D-pad movement quicker.
_DIRECTION_BUTTON_REPEAT_INTERVAL_SECONDS = 0.065

# Semantic tap mappings only. Chromecast physical-edge correlation is separate.
_BUTTON_ACTION_NAVIGATION_VKS = {
    key_mapping.ActionKind.ARROW_UP: 0x26,
    key_mapping.ActionKind.ARROW_DOWN: 0x28,
    key_mapping.ActionKind.ARROW_LEFT: 0x25,
    key_mapping.ActionKind.ARROW_RIGHT: 0x27,
    key_mapping.ActionKind.RETURN: 0x0D,
    key_mapping.ActionKind.ESCAPE: 0x1B,
    key_mapping.ActionKind.CONTEXT_MENU: 0x5D,
    key_mapping.ActionKind.SYSTEM_VOLUME_UP: 0xAF,
    key_mapping.ActionKind.SYSTEM_VOLUME_DOWN: 0xAE,
}

# The remote has no dedicated parent/child actions. A user-mapped, unmodified
# PageUp/PageDown tap keeps those navigation steps available without claiming
# the computer keyboard or changing multi-key shortcut semantics.
_BUTTON_NAVIGATION_SINGLE_KEYS = {
    ("pageup",): 0x21,
    ("page_up",): 0x21,
    ("pagedown",): 0x22,
    ("page_down",): 0x22,
}

_RAW_FALLBACK_KEY_TOKENS = {
    "mic": "f5",
    "right": "right",
    "left": "left",
    "down": "down",
    "up": "up",
    "ok": "enter",
    "home": "home",
    "menu": "apps",
    "tv": "backtick",
    "power": "vk_5f",
    "volume_mute": "volume_mute",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "back": "browser_back",
}

_KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS = 1.0
_KEY_DETECTION_MIC_MAX_SECONDS = 10.0
_ORDINARY_MIC_RELEASE_GUARD_SECONDS = 0.12
_ORDINARY_MIC_SOURCE_STALE_SECONDS = 2.0
_ORDINARY_MIC_MAX_SECONDS = 10.0
_KEY_DETECTION_SUPPRESSION_MAX_SECONDS = 2.0
_RUNTIME_STATUS_HEARTBEAT_SECONDS = 5.0
_VOICE_KEY_PHYSICALIZER_RETRY_SECONDS = 1.0
_VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS = 10.0
_RAW_INPUT_RETRY_INITIAL_SECONDS = 1.0
_RAW_INPUT_RETRY_MAX_SECONDS = 30.0
_BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS = 0.1
_BUTTON_INPUT_RELEASE_RETRY_MAX_SECONDS = 2.0
_DOUBAO_HOST_READY_POLL_SECONDS = 0.05
_DOUBAO_PROCESS_READY_TIMEOUT_SECONDS = 2.0
# Eager Doubao physicalizer start: attach Frida to ImeService.exe at bridge
# start instead of waiting for the first hotkey press.  The window bounds how
# long we wait for ImeService.exe to appear after the voice program launches.
_DOUBAO_EAGER_START_WINDOW_SECONDS = 10.0
_DOUBAO_EAGER_POLL_SECONDS = 0.5


def _runtime_voice_hotkey_text(config_data: dict) -> str:
    configured = str(config_data.get("voice_hotkey", "")).strip()
    return configured or key_mapping.voice_hotkey_for_trigger_mode(
        key_mapping.VoiceTriggerMode.HOLD
    )


def open_configured_application(action: key_mapping.ButtonAction) -> bool:
    """Application-action seam kept at the app boundary for testability."""

    return action_executor.open_configured_application(action)




class ChromecastApp:
    def __init__(self, *, launch_voice_program_on_start: bool = True) -> None:
        self._config_root = config.config_root()
        self._config_path = config.config_path(self._config_root)
        self._config = config.load_config(self._config_path)
        from . import remote_selection
        self._selected_remote_key = remote_selection.active_key(self._config)
        selected_profile = remote_selection.active_profile(self._config)
        self._remote_profile = selected_profile
        if selected_profile and not remote_selection.runtime_ready(selected_profile):
            raise remote_selection.SelectionError("当前型号尚未提供接收，请在设备页重新选择。")
        self._config_mtime_ns = self._settings_file_mtime_ns(self._config_path)
        self._bindings_path = config.key_bindings_path(self._config_root)
        self._bindings = config.load_key_bindings(
            self._bindings_path
        )
        self._removed_voice_bindings = config.normalize_voice_product_boundary(
            self._config,
            self._bindings,
        )
        self._bindings_mtime_ns = self._settings_file_mtime_ns(self._bindings_path)
        self._button_mapping_lock = threading.RLock()
        self._button_gestures = button_gesture.ButtonGestureDispatcher(
            is_action_configured=self._is_button_action_configured,
            is_repeatable=self._is_button_repeatable,
            on_trigger=self._on_button_trigger,
            repeat_interval_for=self._button_repeat_interval,
            on_idle=self._apply_pending_settings_if_idle,
            on_diagnostic=self._on_button_gesture_diagnostic,
            is_sustained=self._is_mouse_hold_button,
            on_hold_start=lambda button, token: self._on_button_trigger(
                button, button_gesture.ButtonTrigger.LONG_PRESS, hold_owner=token),
            on_hold_end=self._end_button_mouse_hold,
        )
        self._button_combos = button_combo.ButtonComboRecognizer()
        # Apply the persisted app.log switch *before* the first get_logger():
        # that call is what installs the file handler for this process.
        logging_setup.set_app_log_enabled(
            bool(self._config.get("app_log_enabled", True)), self._config_root
        )
        self._logger: logging.Logger = logging_setup.get_logger(self._config_root)
        self._diagnostic_trace = diagnostic_trace.DiagnosticTrace(
            self._config_root,
            enabled=bool(self._config.get("diagnostic_trace_enabled", False)),
        )
        win32_input.set_diagnostic_trace(self._diagnostic_trace)
        voice_key_physicalizer_windows.set_diagnostic_trace(self._diagnostic_trace)
        wetype_control_windows.set_diagnostic_trace(self._diagnostic_trace)
        doubao_rpc.set_diagnostic_trace(self._diagnostic_trace)
        element_navigation_runtime.set_diagnostic_trace(self._diagnostic_trace)
        self._record_device_diagnostic_context()
        self._voice_attempt_id: Optional[str] = None
        self._runtime_identity = bridge_runtime_status.current_runtime_identity(
            __version__
        )
        self._runtime_status_lock = threading.Lock()
        self._runtime_connection_state = (
            bridge_runtime_status.BridgeConnectionState.WAITING_FOR_DEVICE
        )
        self._runtime_raw_input_state = "starting"
        self._runtime_hid_tap_state = "starting"
        self._runtime_voice_key_physicalizer_state = "starting"
        self._runtime_last_button_at: Optional[float] = None
        self._runtime_last_button_source = ""
        self._runtime_voice_active = False
        self._runtime_voice_state = bridge_runtime_status.VOICE_RUNTIME_NOT_TESTED
        self._runtime_voice_provider = ""
        self._runtime_voice_updated_at: Optional[float] = None
        self._runtime_battery_level: Optional[int] = None
        self._runtime_last_button_publish_monotonic = 0.0
        self._logger.info(
            "startup: app identity: version=%s runtime=%s package=%s",
            self._runtime_identity.app_version,
            self._runtime_identity.runtime_kind,
            self._runtime_identity.package_name,
        )
        if launch_voice_program_on_start and (
            selected_profile != remote_selection.CHROMECAST_PROFILE
            or self._config.get("voice_program", {}).get("provider")
            == voice_program_manager.VOICE_PROGRAM_SOGOU
        ):
            try:
                voice_program_result = (
                    voice_program_manager.launch_configured_at_bridge_start(self._config)
                )
            except Exception:
                self._logger.exception(
                    "voice program: optional bridge-start launch failed unexpectedly"
                )
            else:
                if voice_program_result.code not in {"not_requested", "disabled"}:
                    self._logger.info(
                        "voice program: provider=%s launch_result=%s",
                        voice_program_result.provider_id,
                        voice_program_result.code,
                    )
        else:
            self._logger.info(
                "voice program: startup launch skipped during diagnostics recovery"
            )
        if self._removed_voice_bindings:
            self._logger.warning(
                "legacy voice mappings disabled until user reselects actions: %s",
                sorted(self._removed_voice_bindings),
            )
        # Apply the persisted audio-ducking switch here: the voice pipeline
        # runs in this process and a settings write only reloads the bridge.
        system_playback_mute.set_enabled(
            bool(self._config.get("audio_ducking_enabled", False))
        )
        self._playback_ducker = system_playback_mute.PlaybackDucker(
            logger=self._logger,
        )
        self._voice_shortcut = voice_shortcut_session.VoiceShortcutSession(
            hotkey_text=_runtime_voice_hotkey_text(self._config),
            logger=self._logger, trace=self._diagnostic_trace,
            services=voice_shortcut_session.ShortcutServices(
                configured_backend=lambda: self._configured_voice_hotkey_backend(),
                prepare_doubao=lambda tokens: self._prepare_doubao_voice_physicalizer(tokens),
                resume_tracking=lambda: self._resume_degraded_voice_key_physicalizer_recovery(),
                set_result=lambda *args, **kw: self._set_runtime_voice_result(*args, **kw),
                current_state=self._current_voice_runtime_state,
                prepare_mute_guard=self._prepare_wetype_playback_mute_guard,
                duck_playback=self._playback_ducker.begin,
                restore_playback=self._playback_ducker.end,
            ),
        )
        # Attach Frida to the Doubao IME up front so the first voice-key /
        # voice-keyboard hold does not pay the ~0.6s frida.attach cost.  The
        # key-path prepare_doubao call stays as an idempotent fallback.
        self._start_doubao_voice_physicalizer_eager()
        self._pending_voice_settings = None
        self._pending_config = None
        self._pending_bindings = None
        self._pending_output_endpoint: Optional[Tuple[str, str]] = None
        self._voice_audio_start_fallback_pending = False
        self._voice_focus_before: Optional[
            voice_interaction_diagnostics_windows.FocusSnapshot
        ] = None
        self._voice_focus_provider = ""
        self._voice_focus_submit_method = ""
        self._voice_text_observation = "unknown"
        self._doubao_capture_watch_factory = lambda target_pid: (
            chromecast_host_activity.CaptureWatch(
                reader=lambda: chromecast_host_activity.read_doubao_capture_for_pid(
                    target_pid
                ),
                fast_start=True,
            )
        )
        self._voice_pcm_min_arrival_sequence: Optional[int] = None
        self._button_action_lock = threading.RLock()
        self._button_key_release_pending: Optional[Tuple[str, ...]] = None
        self._button_mouse_release_pending: Optional[str] = None
        self._button_mouse_holds: Dict[str, set[object]] = {}
        self._button_hold_release_pending: set[str] = set()
        self._button_input_release_retry_timer: Optional[object] = None
        self._button_input_release_retry_token: Optional[object] = None
        self._button_input_release_retry_delay = (
            _BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS
        )
        self._button_input_release_retry_stopping = False
        self._button_input_release_timer_factory = threading.Timer
        # Raw Input and the ATVV control channel arrive on different worker
        # threads. Serialize the voice state machine so one physical press
        # cannot race into two host shortcut deliveries.
        self._logger.info(
            "startup: voice settings active: trigger_mode=%s hotkey=%s",
            self._voice_shortcut.controller.trigger_mode.value,
            self._voice_shortcut.hotkey.serialize(),
        )
        # One Chromecast microphone press is reported independently by HID, the
        # ATVV mic opcode, and sometimes AUDIO_STARTED first. Keep all reports
        # in one gesture so one real press produces one host down and release.
        self._voice_mic_gesture_active = False
        self._voice_mic_gesture_audio_started = False
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = False
        self._voice_mic_gesture_hid_released = False
        self._voice_mic_gesture_direct_hid_seen = False
        self._voice_mic_gesture_sources_down: set[str] = set()
        self._voice_hold_watchdog_timer: Optional[object] = None
        self._voice_hold_watchdog_token: Optional[object] = None
        self._voice_hold_watchdog_timer_factory = threading.Timer
        self._ordinary_mic_lock = threading.Lock()
        self._ordinary_mic_sources_down: set[str] = set()
        self._ordinary_mic_late_sources_down: set[str] = set()
        self._ordinary_mic_late_source_deadlines: dict[str, float] = {}
        self._ordinary_mic_sources_seen: set[str] = set()
        self._ordinary_mic_gesture_started_at = 0.0
        self._ordinary_mic_release_guard_until = 0.0
        self._ordinary_mic_gesture_active = False
        self._unsolicited_mic_close_pending = False
        self._voice_audio_stream_active = False
        self._voice_audio_stop_processed = False
        self._voice_pcm_forwarding_enabled = False
        self._hid_listener: Optional[raw_input_windows.RawInputButtonListener] = None
        self._raw_input_lifecycle_lock = threading.RLock()
        self._raw_input_operation_lock = threading.RLock()
        self._raw_input_generation = 0
        self._raw_input_lost_generation = -1
        self._raw_input_stopping = False
        self._raw_input_retry_timer: Optional[object] = None
        self._raw_input_retry_token: Optional[object] = None
        self._raw_input_retry_delay = _RAW_INPUT_RETRY_INITIAL_SECONDS
        self._raw_input_timer_factory = threading.Timer
        self._raw_windows_key_down_query = (
            raw_input_windows.physical_key_is_down_before_injection
        )
        self._voice_key_physicalizer: Optional[
            voice_key_physicalizer_windows.VoiceKeyPhysicalizer
        ] = None
        self._voice_key_physicalizer_ready = False
        self._voice_key_physicalizer_lifecycle_lock = threading.RLock()
        self._voice_key_physicalizer_operation_lock = threading.Lock()
        self._voice_key_physicalizer_generation = 0
        self._voice_key_physicalizer_lost_generation = -1
        self._voice_key_physicalizer_stopping = False
        self._voice_key_physicalizer_retry_timer: Optional[object] = None
        self._voice_key_physicalizer_retry_token: Optional[object] = None
        self._voice_key_physicalizer_degraded_retry_deadline = 0.0
        self._voice_key_physicalizer_timer_factory = threading.Timer
        # Optional, independent observer for one operator-chosen physical
        # modifier. It stays None until the user configures the key, so the
        # default install adds no keyboard hook at all.
        self._physical_key_tristate: Optional[
            physical_key_tristate_windows.PhysicalKeyTriStateController
        ] = None
        self._physical_key_tristate_token = ""
        self._physical_key_tristate_lock = threading.Lock()
        self._direct_hid_usages: set[int] = set()
        self._direct_hid_report_seq = 0
        self._direct_hid_lock = threading.Lock()
        self._input_arbitration_lock = threading.RLock()
        # True only after the injected endpoint confirms it copied and cleared
        # a real Chromecast report before Windows could translate that report.
        self._direct_hid_interception_ready = False
        # The helper reports ATTACHED_WAITING_IO after the hook and lease are
        # armed but before the first intercepted report proves end-to-end I/O.
        # Suspend Raw Input mapping during that short handover so one physical
        # hold cannot be split between two owners.
        self._direct_hid_interception_armed = False
        self._direct_hid_handover_waiting_for_neutral = False
        self._raw_fallback_buttons_down: set[str] = set()
        self._raw_fallback_physical_buttons_down: dict[
            str, tuple[str, Optional[str]]
        ] = {}
        self._raw_fallback_release_debts: dict[
            str, tuple[set[str], set[str]]
        ] = {}
        self._raw_fallback_tracking_active = False
        self._raw_fallback_hold_guards: dict[str, tuple[object, object]] = {}
        self._raw_fallback_timer_factory = threading.Timer
        self._input_rearm_blocked_buttons: set[str] = set()
        self._key_detection_suppressed_buttons: set[str] = set()
        self._key_detection_suppression_deadlines: dict[str, float] = {}
        self._key_detection_mic_lock = threading.Lock()
        self._key_detection_mic_gesture_active = False
        self._key_detection_mic_gesture_started_at = 0.0
        self._key_detection_mic_release_deadline: Optional[float] = None
        self._key_detection_mic_audio_started = False
        self._key_detection_mic_sources_down: set[str] = set()
        self._voice_audio = voice_audio_session.VoiceAudioSession(
            config=lambda: self._config,
            logger=self._logger,
            disable_forwarding=lambda: setattr(self, "_voice_pcm_forwarding_enabled", False),
            on_first_frame=self._on_voice_audio_first_frame,
        )
        self._event_loop = asyncio.get_event_loop()
        # Physical input belongs to the bridge-worker lifetime, not to one BLE
        # connection attempt. BLE callbacks have their own generation gate.
        self._accept_input_events = True

        # Construct shared-service bindings after their state is initialized.
        self._chromecast_runtime = chromecast_runtime.ChromecastRuntime(
            chromecast_runtime.RuntimeServices(
                selected_key=lambda: self._selected_remote_key,
                logger=self._logger,
                create_voice_host=self._create_chromecast_voice_host,
                set_input_state=lambda **kw: self._set_runtime_input_state(**kw),
                publish_status=lambda state: self._publish_runtime_status(state),
                prepare_input_lifecycle=lambda: self._prepare_input_lifecycle(),
                start_hid_listener=lambda: self._start_hid_listener(),
                stop_input=lambda: self._stop_input_channels(),
                disable_input=lambda: setattr(self, "_accept_input_events", False),
                cancel_mappings=self._cancel_chromecast_mappings,
                release_inputs=self._release_chromecast_inputs,
                route_edge=self._route_chromecast_edge,
            )
        )

    def _create_chromecast_voice_host(self):
        from .chromecast_voice_host import VoiceHost, VoiceHostServices
        return VoiceHost(VoiceHostServices(
            audio=self._voice_audio,
            shortcut=self._voice_shortcut,
            settings=lambda: self._config,
            config_root=self._config_root,
            logger=self._logger,
            configured_backend=lambda: self._configured_voice_hotkey_backend(),
            voice_mapping_enabled=lambda: self._voice_mode_for_primary_button(
                "mic", self._primary_button_action("mic")) is not None,
            ensure_key_tracking=lambda *args: self._ensure_voice_key_physicalizer_for_hotkey(*args),
            reload_settings=lambda: self._reload_settings_if_changed(),
            apply_pending_settings=lambda: self._apply_pending_voice_settings_if_idle_locked(),
            begin_diagnostic=lambda *args, **kw: self._ensure_voice_diagnostic_attempt(*args, **kw),
            finish_diagnostic=lambda *args, **kw: self._finish_voice_diagnostic_attempt(*args, **kw),
            set_active=lambda active: self._set_runtime_voice_active(active),
            set_result=lambda *args, **kw: self._set_runtime_voice_result(*args, **kw),
            wait_sogou=lambda: self._wait_for_sogou_voice_process(),
            disable_forwarding=lambda: setattr(self, "_voice_pcm_forwarding_enabled", False),
        ))

    def _current_voice_runtime_state(self):
        with self._runtime_status_lock:
            return self._runtime_voice_state

    def _on_voice_audio_first_frame(self):
        with self._runtime_status_lock:
            voice_active = self._runtime_voice_active
        if voice_active:
            self._set_runtime_voice_result(bridge_runtime_status.VOICE_RUNTIME_RECEIVING_AUDIO)

    def _cancel_chromecast_mappings(self):
        with self._input_arbitration_lock:
            self._accept_input_events = False
            self._button_combos.reset()
            self._button_gestures.reset()

    def _release_chromecast_inputs(self):
        with self._button_action_lock:
            self._button_input_release_retry_stopping = True
            self._cancel_button_input_release_retry_locked(reset_delay=False)
            return self._release_pending_button_inputs()

    def _route_chromecast_edge(self, edge):
        if edge.action == "cancel":
            with self._input_arbitration_lock:
                self._cancel_input_gestures_for_buttons({edge.button}, reason="chromecast_cancel",
                                                       block_until_release=False)
        else:
            self._on_button_event(edge.button, edge.action == "down", event_source="chromecast")

    # -- lifecycle: driven by ChromecastRuntime -------------------

    async def run(self) -> None:
        heartbeat = self._event_loop.create_task(
            self._runtime_status_heartbeat()
        )
        try:
            # KNOWN_PROFILES (remote_selection) currently ships only CHROMECAST_PROFILE,
            # so this is the sole active path; the fallback below is kept as a defensive
            # guard for any future profile rather than removable dead code.
            if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
                await self._chromecast_runtime.run()
                return
            raise ValueError(f"未支持的遥控器类型: {self._remote_profile!r}")
        finally:
            # ChromecastRuntime owns input teardown itself (it waits for the
            # voice host and receiver to confirm cleanup before releasing the
            # input channels), so the app layer only closes diagnostics here.
            element_navigation_runtime.clear_diagnostic_trace(self._diagnostic_trace)
            self._diagnostic_trace.close()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def stop(self) -> None:
        if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
            await self._chromecast_runtime.stop()
            return
        raise ValueError(f"未支持的遥控器类型: {self._remote_profile!r}")

    def request_connection_retry_now(self) -> None:
        return  # No unsolicited, repeated UAC prompts or cross-device retry.

    def _press_wetype_voice_keys(self, tokens):
        win32_input.send_wetype_voice_key_combo_down(tokens)

    def _release_wetype_voice_keys(self, tokens):
        win32_input.send_wetype_voice_key_combo_up(tokens)




    async def _runtime_status_heartbeat(self) -> None:
        while True:
            self._publish_runtime_status()
            await asyncio.sleep(_RUNTIME_STATUS_HEARTBEAT_SECONDS)

    def _publish_runtime_status(
        self,
        state: Optional[bridge_runtime_status.BridgeConnectionState] = None,
    ) -> None:
        with self._runtime_status_lock:
            if state is not None:
                self._runtime_connection_state = state
            if (
                self._runtime_connection_state
                is not bridge_runtime_status.BridgeConnectionState.CONNECTED
            ):
                self._runtime_battery_level = None
            try:
                bridge_runtime_status.publish_status(
                    self._config_root,
                    self._runtime_connection_state,
                    identity=self._runtime_identity,
                    raw_input_state=self._runtime_raw_input_state,
                    hid_tap_state=self._runtime_hid_tap_state,
                    voice_key_physicalizer_state=(
                        self._runtime_voice_key_physicalizer_state
                    ),
                    last_button_at=self._runtime_last_button_at,
                    last_button_source=self._runtime_last_button_source,
                    voice_active=self._runtime_voice_active,
                    voice_runtime_state=self._runtime_voice_state,
                    voice_runtime_provider=self._runtime_voice_provider,
                    voice_runtime_updated_at=self._runtime_voice_updated_at,
                    battery_level=self._runtime_battery_level,
                )
            except (OSError, ValueError):
                self._logger.exception(
                    "bridge runtime status update failed: state=%s",
                    self._runtime_connection_state.value,
                )

    def _set_runtime_input_state(
        self,
        *,
        raw_input_state: Optional[str] = None,
        hid_tap_state: Optional[str] = None,
        voice_key_physicalizer_state: Optional[str] = None,
    ) -> None:
        with self._runtime_status_lock:
            if raw_input_state is not None:
                self._runtime_raw_input_state = str(raw_input_state)
            if hid_tap_state is not None:
                self._runtime_hid_tap_state = str(hid_tap_state)
            if voice_key_physicalizer_state is not None:
                self._runtime_voice_key_physicalizer_state = str(
                    voice_key_physicalizer_state
                )
        self._publish_runtime_status()

    def _set_runtime_voice_active(self, active: bool) -> None:
        active = bool(active)
        with self._runtime_status_lock:
            if active == self._runtime_voice_active:
                return
            self._runtime_voice_active = active
        self._publish_runtime_status()


    def _set_runtime_voice_result(
        self,
        state: str,
        *,
        provider: Optional[str] = None,
    ) -> None:
        resolved_provider = provider
        if resolved_provider is None:
            resolved_provider = str(
                voice_program_manager.normalize_voice_program_settings(
                    self._config.get("voice_program")
                )["provider"]
            )
        with self._runtime_status_lock:
            previous_state = getattr(self, '_runtime_voice_state', 'not_tested')
            previous_provider = getattr(self, '_runtime_voice_provider', '')
            self._runtime_voice_state = str(state)
            self._runtime_voice_provider = str(resolved_provider)
            self._runtime_voice_updated_at = time.time()
        self._publish_runtime_status()
        trace = getattr(self, '_diagnostic_trace', None)
        if ((previous_state, previous_provider) != (str(state), str(resolved_provider))
                and state in {bridge_runtime_status.VOICE_RUNTIME_HOST_START_FAILED,
                              bridge_runtime_status.VOICE_RUNTIME_HOST_STOP_FAILED,
                              bridge_runtime_status.VOICE_RUNTIME_OUTPUT_OPEN_FAILED}):
            self._logger.warning("Voice runtime failed: state=%s provider=%s", state, resolved_provider,
                                 extra={"failure_key": f"voice:{resolved_provider}:{state}"})
        if (state == bridge_runtime_status.VOICE_RUNTIME_HOST_UNCONFIRMED
                and (previous_state, previous_provider) != (str(state), str(resolved_provider))):
            self._logger.info("Voice runtime unconfirmed: state=%s provider=%s; audio received, host capture unknown",
                              state, resolved_provider)
        if trace is not None and (previous_state, previous_provider) != (str(state), str(resolved_provider)):
            try:
                trace.emit('voice_runtime_state', state=str(state), previous_state=previous_state,
                           provider=str(resolved_provider), **trace.current_context())
            except Exception:
                pass  # Diagnostic failures must never change voice state or cleanup.



    def _prepare_wetype_playback_mute_guard(self):
        sink = self._voice_audio.sink
        if sink is None or not getattr(sink, "ready", False):
            return None
        name = getattr(sink, "endpoint_name", "")
        if not isinstance(name, str) or not name:
            return None
        return voice_playback_session_windows.prepare_playback_mute_guard(name)








    def _record_runtime_button(self, event_source: str) -> None:
        now_wall = time.time()
        now_monotonic = time.monotonic()
        with self._runtime_status_lock:
            self._runtime_last_button_at = now_wall
            self._runtime_last_button_source = str(event_source)
            if now_monotonic - self._runtime_last_button_publish_monotonic < 0.5:
                return
            self._runtime_last_button_publish_monotonic = now_monotonic
        self._publish_runtime_status()

    def clear_runtime_status(self) -> None:
        try:
            bridge_runtime_status.clear_status(
                self._config_root,
                pid=os.getpid(),
            )
        except OSError:
            self._logger.exception("bridge runtime status cleanup failed")

    def _prepare_input_lifecycle(self) -> None:
        """Device preselection check + open input gate + reset stopping flags (shared by both profiles)."""

        from . import remote_selection
        if not self._selected_remote_key:
            raise remote_selection.SelectionError("请先在设备页选择要使用的遥控器。")

        with self._voice_shortcut.lock:
            self._accept_input_events = True
        with self._voice_key_physicalizer_lifecycle_lock:
            self._voice_key_physicalizer_stopping = False
        with self._raw_input_lifecycle_lock:
            self._raw_input_stopping = False
        self._sync_physical_key_tristate()

    def _physical_key_tristate_tokens(self) -> Tuple[str, ...]:
        """The configured voice shortcut, as token names.

        The watched key is *not* removed here: the tri-state controller owns
        that decision, because it compares virtual-key codes and therefore
        also catches aliases of the same key.
        """

        try:
            tokens = self._voice_shortcut.hotkey.modifiers + (
                self._voice_shortcut.hotkey.key,
            )
        except Exception:
            return ()
        return tuple(token for token in tokens if token)

    def _tristate_voice_press(self, tokens: Sequence[str] = ()) -> bool:
        """Start voice exactly the way the remote's own mic button does.

        Reusing ``VoiceShortcutSession.apply`` keeps one voice pipeline: the
        input-profile switch, the provider control and Doubao's in-process
        Frida hook all run as they do for the BLE button, so a physical hold
        cannot drift from the remote behavior.

        ``tokens`` is already stripped of the watched key, which the
        operator's own finger holds. Re-injecting it would be refused by the
        preflight guard and drop the entire combo.
        """

        tokens = tuple(tokens)
        if not tokens:
            self._logger.warning(
                "physical key tri-state: voice press skipped; the shortcut "
                "has no key left besides the watched one"
            )
            return False
        shortcut = self._voice_shortcut
        with shortcut.lock:
            if shortcut.pending_tokens is not None:
                # A previous hold still owes a key-up; releasing it first
                # keeps one voice session from stacking on another.
                if not shortcut.release_pending():
                    self._logger.warning(
                        "physical key tri-state: previous voice shortcut "
                        "cleanup is still pending"
                    )
                    return False
            action = shortcut.controller.on_mic_button_pressed()
            delivered = shortcut.apply(
                action,
                tokens_override=tokens,
            )
            if not delivered:
                shortcut.controller.cancel_pending()
            return bool(delivered)

    def _tristate_voice_release(self, tokens: Sequence[str] = ()) -> bool:
        """Release the voice hold the tri-state press opened.

        ``tokens`` are the ones that actually went down. Never fall back to
        the configured shortcut here: that set contains the watched key,
        which the operator's finger holds and which must not be lifted.
        """

        tokens = tuple(tokens)
        if not tokens:
            return True
        shortcut = self._voice_shortcut
        with shortcut.lock:
            action = shortcut.controller.on_mic_button_released()
            if action is None:
                return True
            released = shortcut.apply(
                action,
                tokens_override=tokens,
            )
            if not released:
                shortcut.controller.restore_pending(action)
            return bool(released)

    def _ensure_marked_delivery_for_tristate(self) -> bool:
        """Force the marked hook for the tri-state's configured shortcut.

        The ordinary remote path only installs the hook for right-Alt
        shortcuts. The tri-state sends whatever the provider configured
        (doubao uses ``lctrl+lwin+lalt``), so it must ask for it explicitly.

        Only the backends that actually emit marked key events have such a
        path. For the others the tri-state has no marked delivery to
        prepare, which is reported as "not available" rather than "ready".
        """

        backend = self._configured_voice_hotkey_backend()
        if backend not in {
            _VOICE_HOTKEY_BACKEND_MARKED,
            _VOICE_HOTKEY_BACKEND_DOUBAO,
        }:
            return False
        return self._ensure_voice_key_physicalizer_for_hotkey(
            self._physical_key_tristate_tokens(),
            backend,
            required=True,
        )

    def _sync_physical_key_tristate(self) -> None:
        """Start, stop, or retarget the physical-key observer from config."""

        token = config.normalize_voice_physical_key(
            self._config.get("voice_physical_key", "")
        )
        with self._physical_key_tristate_lock:
            current = self._physical_key_tristate
            if not token:
                self._physical_key_tristate = None
                self._physical_key_tristate_token = ""
                observer = current
            elif current is not None and self._physical_key_tristate_token == token:
                current.update_shortcut(self._physical_key_tristate_tokens())
                return
            else:
                self._physical_key_tristate = None
                self._physical_key_tristate_token = ""
                observer = current
        if observer is not None:
            try:
                observer.stop()
            except Exception:
                self._logger.exception(
                    "physical key tri-state: observer did not stop cleanly"
                )
        if not token:
            return
        try:
            vk_code = win32_keys.resolve_vk_codes((token,))[0]
        except Exception:
            self._logger.warning(
                "physical key tri-state: ignoring unresolvable key %r", token
            )
            return
        if not self._accept_input_events:
            return
        observer = physical_key_tristate_windows.PhysicalKeyTriStateController(
            vk_code,
            self._physical_key_tristate_tokens(),
            logger=self._logger,
            ensure_physicalizer=self._ensure_marked_delivery_for_tristate,
            voice_press=self._tristate_voice_press,
            voice_release=self._tristate_voice_release,
        )
        try:
            observer.start()
        except Exception:
            self._logger.exception(
                "physical key tri-state: observer failed to start for %s", token
            )
            return
        with self._physical_key_tristate_lock:
            replacement = self._physical_key_tristate
            self._physical_key_tristate = observer
            self._physical_key_tristate_token = token
        if replacement is not None:
            # A concurrent sync installed another observer first. Only one
            # hook may watch the key, so drop the loser.
            try:
                replacement.stop()
            except Exception:
                self._logger.exception(
                    "physical key tri-state: superseded observer did not stop"
                )
        self._logger.info(
            "startup: physical key tri-state enabled key=%s shortcut=%s",
            token,
            "+".join(self._physical_key_tristate_tokens()) or "(empty)",
        )

    def _stop_physical_key_tristate(self) -> None:
        with self._physical_key_tristate_lock:
            observer = self._physical_key_tristate
            self._physical_key_tristate = None
            self._physical_key_tristate_token = ""
        if observer is None:
            return
        try:
            observer.stop()
        except Exception:
            self._logger.exception(
                "physical key tri-state: observer did not stop cleanly"
            )

    def _start_voice_key_physicalizer(self, *, recovering: bool = False) -> None:
        with self._voice_key_physicalizer_operation_lock:
            with self._voice_key_physicalizer_lifecycle_lock:
                if (
                    self._voice_key_physicalizer_stopping
                    or not self._accept_input_events
                ):
                    return
                current = self._voice_key_physicalizer
                if (
                    current is not None
                    and current.is_running
                    and bool(getattr(current, "accepts_new_down", True))
                ):
                    return
                if current is not None and current.is_running:
                    self._voice_key_physicalizer_ready = False
                    if not self._voice_key_physicalizer_degraded_retry_deadline:
                        self._voice_key_physicalizer_degraded_retry_deadline = (
                            time.monotonic()
                            + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
                        )
                    self._schedule_voice_key_physicalizer_recovery_locked()
                    return
                # A queued callback belongs to the owner/generation captured
                # when it was scheduled. Retire that work before publishing a
                # replacement so a failed replacement start can own the sole
                # retry slot.
                self._cancel_voice_key_physicalizer_retry_locked()
                self._voice_key_physicalizer = None
                self._voice_key_physicalizer_generation += 1
                generation = self._voice_key_physicalizer_generation
                self._voice_key_physicalizer_lost_generation = -1
                physicalizer = (
                    voice_key_physicalizer_windows.VoiceKeyPhysicalizer()
                )
                self._voice_key_physicalizer = physicalizer
                self._voice_key_physicalizer_ready = False
                state = "recovering" if recovering else "starting"
            self._set_runtime_input_state(
                voice_key_physicalizer_state=state
            )
            set_tracking_lost_callback = getattr(
                physicalizer,
                "set_tracking_lost_callback",
                None,
            )
            if callable(set_tracking_lost_callback):
                set_tracking_lost_callback(
                    lambda physicalizer=physicalizer, generation=generation: (
                        self._on_voice_key_physicalizer_tracking_lost(
                            physicalizer,
                            generation,
                        )
                    )
                )
            set_health_failure_callback = getattr(
                physicalizer,
                "set_health_failure_callback",
                None,
            )
            if callable(set_health_failure_callback):
                set_health_failure_callback(
                    lambda reason, snapshot, physicalizer=physicalizer,
                    generation=generation: (
                        self._on_voice_key_physicalizer_health_failure(
                            physicalizer,
                            generation,
                            reason,
                            snapshot,
                        )
                    )
                )
            try:
                physicalizer.start()
            except (
                voice_key_physicalizer_windows.
                VoiceKeyPhysicalizerUnavailableError
            ) as exc:
                with self._voice_key_physicalizer_lifecycle_lock:
                    if (
                        self._voice_key_physicalizer is physicalizer
                        and self._voice_key_physicalizer_generation == generation
                    ):
                        self._voice_key_physicalizer_ready = False
                        if not physicalizer.is_running:
                            self._voice_key_physicalizer = None
                        self._schedule_voice_key_physicalizer_recovery_locked()
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="failed"
                )
                if physicalizer.is_running:
                    self._logger.exception(
                        "startup: voice key physicalizer failed but is still running; "
                        "owner retained for cleanup"
                    )
                    raise
                self._logger.warning(
                    "startup: voice key physicalizer unavailable; recovery scheduled: %s",
                    exc,
                )
                return

            stop_stale_instance = False
            with self._voice_key_physicalizer_lifecycle_lock:
                current_generation = self._voice_key_physicalizer_generation
                stale = (
                    self._voice_key_physicalizer is not physicalizer
                    or current_generation != generation
                    or self._voice_key_physicalizer_stopping
                )
                lost = self._voice_key_physicalizer_lost_generation == generation
                if stale:
                    stop_stale_instance = physicalizer.is_running
                elif lost or not physicalizer.is_running:
                    self._voice_key_physicalizer_ready = False
                    self._schedule_voice_key_physicalizer_recovery_locked()
                else:
                    self._voice_key_physicalizer_ready = True
                    self._voice_key_physicalizer_degraded_retry_deadline = 0.0
            if stop_stale_instance:
                try:
                    physicalizer.stop()
                except Exception:
                    self._logger.exception(
                        "cleanup: stale voice key physicalizer did not stop"
                    )
                return
            if lost or not physicalizer.is_running:
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="recovering"
                )
                return
            self._set_runtime_input_state(
                voice_key_physicalizer_state="ready"
            )
            self._logger.info("startup: marked voice key physicalizer enabled")

    def _cancel_voice_key_physicalizer_retry_locked(self) -> None:
        timer = self._voice_key_physicalizer_retry_timer
        self._voice_key_physicalizer_retry_timer = None
        self._voice_key_physicalizer_retry_token = None
        if timer is None:
            return
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException:
                pass

    def _schedule_voice_key_physicalizer_recovery_locked(self) -> None:
        if (
            self._voice_key_physicalizer_stopping
            or not self._accept_input_events
            or self._voice_key_physicalizer_retry_timer is not None
        ):
            return
        token = object()
        expected_physicalizer = self._voice_key_physicalizer
        expected_generation = self._voice_key_physicalizer_generation
        try:
            timer = self._voice_key_physicalizer_timer_factory(
                _VOICE_KEY_PHYSICALIZER_RETRY_SECONDS,
                lambda token=token,
                expected_physicalizer=expected_physicalizer,
                expected_generation=expected_generation: (
                    self._recover_voice_key_physicalizer(
                        token,
                        expected_physicalizer,
                        expected_generation,
                    )
                ),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._voice_key_physicalizer_retry_token = token
            self._voice_key_physicalizer_retry_timer = timer
            timer.start()
        except BaseException as exc:
            if self._voice_key_physicalizer_retry_token is token:
                self._voice_key_physicalizer_retry_token = None
                self._voice_key_physicalizer_retry_timer = None
            self._logger.error(
                "voice key physicalizer recovery timer failed: error_type=%s",
                type(exc).__name__,
            )

    def _recover_voice_key_physicalizer(
        self,
        token: object,
        expected_physicalizer: Optional[
            voice_key_physicalizer_windows.VoiceKeyPhysicalizer
        ],
        expected_generation: int,
    ) -> None:
        with self._voice_key_physicalizer_lifecycle_lock:
            if self._voice_key_physicalizer_retry_token is not token:
                return
            self._voice_key_physicalizer_retry_token = None
            self._voice_key_physicalizer_retry_timer = None
            if (
                self._voice_key_physicalizer_stopping
                or not self._accept_input_events
            ):
                return
            if (
                self._voice_key_physicalizer is not expected_physicalizer
                or self._voice_key_physicalizer_generation
                != expected_generation
            ):
                return
            physicalizer = self._voice_key_physicalizer
            generation = self._voice_key_physicalizer_generation
            running = bool(physicalizer is not None and physicalizer.is_running)
            accepts_new_down = bool(
                running and getattr(physicalizer, "accepts_new_down", True)
            )
            if running and accepts_new_down:
                if self._voice_key_physicalizer_lost_generation == generation:
                    self._schedule_voice_key_physicalizer_recovery_locked()
                    return
                self._voice_key_physicalizer_ready = True
                self._voice_key_physicalizer_degraded_retry_deadline = 0.0
                return
        if running:
            with self._voice_shortcut.lock:
                cleanup_blocks_recovery = bool(
                    self._voice_shortcut.pending_tokens is not None
                    or self._voice_shortcut.controller.active
                )
            if cleanup_blocks_recovery:
                with self._voice_key_physicalizer_lifecycle_lock:
                    if (
                        self._voice_key_physicalizer is not physicalizer
                        or self._voice_key_physicalizer_generation != generation
                        or self._voice_key_physicalizer_stopping
                        or not self._accept_input_events
                    ):
                        return
                    if (
                        self._voice_key_physicalizer_degraded_retry_deadline
                        and time.monotonic()
                        < self._voice_key_physicalizer_degraded_retry_deadline
                    ):
                        self._schedule_voice_key_physicalizer_recovery_locked()
                        return
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="failed"
                )
                self._diagnostic_trace.emit(
                    "voice_key_physicalizer_health",
                    status="blocked",
                    reason="owned_cleanup_unresolved",
                    app_generation=int(generation),
                )
                self._logger.error(
                    "voice key physicalizer recovery blocked: owned voice "
                    "shortcut cleanup is still unresolved"
                )
                return

            stop_succeeded = False
            with self._voice_key_physicalizer_operation_lock:
                with self._voice_key_physicalizer_lifecycle_lock:
                    if (
                        self._voice_key_physicalizer is not physicalizer
                        or self._voice_key_physicalizer_generation != generation
                        or self._voice_key_physicalizer_stopping
                        or not self._accept_input_events
                    ):
                        return
                try:
                    physicalizer.stop()
                except Exception:
                    self._logger.exception(
                        "voice key physicalizer recovery could not retire the "
                        "degraded owner; replacement not started"
                    )
                else:
                    stop_succeeded = True
                    with self._voice_key_physicalizer_lifecycle_lock:
                        if (
                            self._voice_key_physicalizer is physicalizer
                            and self._voice_key_physicalizer_generation == generation
                        ):
                            self._voice_key_physicalizer = None
                            self._voice_key_physicalizer_ready = False
            if not stop_succeeded:
                self._set_runtime_input_state(
                    voice_key_physicalizer_state="failed"
                )
                return
            self._set_runtime_input_state(
                voice_key_physicalizer_state="recovering"
            )
            self._start_voice_key_physicalizer(recovering=True)
            return

        with self._voice_key_physicalizer_lifecycle_lock:
            if (
                self._voice_key_physicalizer is not physicalizer
                or self._voice_key_physicalizer_generation != generation
                or self._voice_key_physicalizer_stopping
                or not self._accept_input_events
            ):
                return
            self._voice_key_physicalizer = None
        self._set_runtime_input_state(
            voice_key_physicalizer_state="recovering"
        )
        self._start_voice_key_physicalizer(recovering=True)

    def _resume_degraded_voice_key_physicalizer_recovery(self) -> None:
        """Resume one blocked recovery after authoritative cleanup succeeds."""

        with self._voice_key_physicalizer_lifecycle_lock:
            physicalizer = self._voice_key_physicalizer
            if (
                physicalizer is None
                or not physicalizer.is_running
                or bool(getattr(physicalizer, "accepts_new_down", True))
                or self._voice_key_physicalizer_stopping
                or not self._accept_input_events
            ):
                return
            self._voice_key_physicalizer_degraded_retry_deadline = (
                time.monotonic()
                + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
            )
            self._schedule_voice_key_physicalizer_recovery_locked()


    def _cancel_raw_input_retry_locked(self) -> None:
        timer = self._raw_input_retry_timer
        self._raw_input_retry_timer = None
        self._raw_input_retry_token = None
        if timer is None:
            return
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException:
                pass

    def _schedule_raw_input_recovery_locked(self) -> None:
        if (
            self._raw_input_stopping
            or not self._accept_input_events
            or self._raw_input_retry_timer is not None
        ):
            return
        token = object()
        delay = self._raw_input_retry_delay
        self._raw_input_retry_delay = min(
            _RAW_INPUT_RETRY_MAX_SECONDS,
            max(_RAW_INPUT_RETRY_INITIAL_SECONDS, delay * 2.0),
        )
        try:
            timer = self._raw_input_timer_factory(
                delay,
                lambda token=token: self._recover_raw_input_listener(token),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._raw_input_retry_token = token
            self._raw_input_retry_timer = timer
            timer.start()
        except BaseException as exc:  # noqa: BLE001 - input remains failed closed
            if self._raw_input_retry_token is token:
                self._raw_input_retry_token = None
                self._raw_input_retry_timer = None
            self._logger.error(
                "Raw Input recovery timer failed: error_type=%s",
                type(exc).__name__,
            )

    def _raw_input_callback_is_current(
        self,
        listener: Optional[raw_input_windows.RawInputButtonListener],
        generation: Optional[int],
    ) -> bool:
        if listener is None and generation is None:
            return True
        if listener is None or generation is None:
            return False
        with self._raw_input_lifecycle_lock:
            return (
                not self._raw_input_stopping
                and self._hid_listener is listener
                and self._raw_input_generation == int(generation)
            )

    def _cancel_raw_input_ownership_locked(self, *, reason: str) -> set[str]:
        """Cancel every Raw-owned edge while the input lock is held."""

        tracked_logical_buttons, tracked_physical_buttons = (
            self._raw_fallback_tracked_button_ids_locked()
        )
        raw_fallback_buttons = (
            set(self._raw_fallback_buttons_down) | tracked_logical_buttons
        )
        raw_fallback_physical_buttons = (
            self._take_raw_fallback_physical_buttons_locked(
                raw_fallback_buttons
            )
        )
        affected_buttons = raw_fallback_buttons | tracked_physical_buttons
        self._cancel_all_raw_fallback_hold_guards_locked()
        self._raw_fallback_buttons_down.clear()
        if affected_buttons:
            self._cancel_input_gestures(
                affected_buttons,
                reason=reason,
                block_until_release=True,
            )
            self._release_raw_fallback_keyups(
                raw_fallback_physical_buttons,
                reason=reason,
            )
        return affected_buttons

    def _recover_raw_input_listener(self, token: object) -> None:
        with self._raw_input_operation_lock:
            with self._input_arbitration_lock:
                with self._raw_input_lifecycle_lock:
                    if self._raw_input_retry_token is not token:
                        return
                    self._raw_input_retry_token = None
                    self._raw_input_retry_timer = None
                    if self._raw_input_stopping or not self._accept_input_events:
                        return
                    if (self._remote_profile == remote_selection.CHROMECAST_PROFILE
                            and self._chromecast_runtime.voice_host is not None
                            and not self._chromecast_runtime.voice_host.closed):
                        # Preserve physical ownership until the voice owner has
                        # released its keys. A tracker restart clears snapshots.
                        self._chromecast_runtime.voice_host.cancel_recording()
                        self._schedule_raw_input_recovery_locked()
                        return
                    if (self._remote_profile == remote_selection.CHROMECAST_PROFILE
                            and not self._release_pending_button_keys()):
                        self._schedule_raw_input_recovery_locked()
                        return
                    listener = self._hid_listener
                    if listener is not None:
                        # Invalidate every callback before stop() can emit
                        # forced releases. The arbitration lock also lets an
                        # already-running callback finish before its resulting
                        # state is cancelled below.
                        self._raw_input_generation += 1
                if listener is not None:
                    self._cancel_raw_input_ownership_locked(
                        reason="raw_input_recovery"
                    )

            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    self._set_raw_listener_state("failed_stopping")
                    self._logger.exception(
                        "Raw Input recovery could not stop the old listener"
                    )
                    with self._raw_input_lifecycle_lock:
                        self._schedule_raw_input_recovery_locked()
                    return
                with self._raw_input_lifecycle_lock:
                    if self._hid_listener is listener:
                        self._hid_listener = None
                        self._raw_fallback_tracking_active = False

            try:
                self._start_hid_listener_owned(recovering=True)
            except raw_input_windows.RawInputUnavailableError:
                self._logger.exception(
                    "Raw Input recovery retained a listener that did not start cleanly"
                )
                with self._raw_input_lifecycle_lock:
                    self._schedule_raw_input_recovery_locked()

    def _start_hid_listener(self, *, recovering: bool = False) -> None:
        with self._raw_input_operation_lock:
            self._start_hid_listener_owned(recovering=recovering)

    def _set_raw_listener_state(self, state: str) -> None:
        # Chromecast's device status belongs to its receiver, not the auxiliary
        # keyboard tracker. Do not replace chromecast_ready on tracker recovery.
        if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
            self._logger.info("keyboard safety tracking state=%s", state)
        else:
            self._set_runtime_input_state(raw_input_state=state)

    def _start_hid_listener_owned(self, *, recovering: bool = False) -> None:
        """Best-effort: buttons fail closed independently of BLE/voice.

        Multiple matching HID device paths -> fail closed for buttons only
        (log and leave the listener unstarted); this must not tear down the
        BLE/voice path, which does not depend on HID at all.

        A failed ``start()`` call does not
        necessarily mean the listener never came alive - it may have left a
        thread/window behind that its own bounded failed-start cleanup could
        not stop (see raw_input_windows.py's ``_abandon_failed_start()``,
        which keeps ``is_running`` honest for exactly this reason). Clearing
        ``self._hid_listener`` to ``None`` unconditionally here would lose
        that owner reference and let a later start attempt
        start a second listener over the still-live one. Only clear it once
        the listener itself confirms it is not running; otherwise retain and
        re-raise so the owning runtime still sees the failure and gets a
        chance to retry stopping it, exactly like any other retained-owner
        failure.
        """

        with self._input_arbitration_lock:
            with self._raw_input_lifecycle_lock:
                if self._raw_input_stopping or not self._accept_input_events:
                    return
                current = self._hid_listener
                if current is not None and bool(
                    getattr(current, "is_running", False)
                ):
                    return
                self._hid_listener = None
                self._raw_fallback_tracking_active = False
                self._raw_input_generation += 1
                generation = self._raw_input_generation
                self._raw_input_lost_generation = -1

        self._set_raw_listener_state("recovering" if recovering else "starting")
        # The Chromecast path injects HID taps through --google-hid-injector
        # instead of decoding raw buttons, so the listener runs in
        # keyboard-safety-only mode: None registers keyboard input and
        # physical-ownership tracking but decodes no button.
        device_path = None

        listener = raw_input_windows.RawInputButtonListener(
            self._on_raw_button_event
        )
        with self._raw_input_lifecycle_lock:
            if (
                self._raw_input_stopping
                or not self._accept_input_events
                or self._raw_input_generation != generation
            ):
                return
            self._hid_listener = listener
        set_physical_keyboard_tracking_lost_callback = getattr(
            listener,
            "set_physical_keyboard_tracking_lost_callback",
            None,
        )
        if callable(set_physical_keyboard_tracking_lost_callback):
            set_physical_keyboard_tracking_lost_callback(
                lambda reason: self._on_physical_keyboard_tracking_lost(
                    reason,
                    _listener=listener,
                    _generation=generation,
                )
            )
        try:
            listener.start(device_path)
        except raw_input_windows.RawInputUnavailableError as exc:
            if listener.is_running:
                self._set_raw_listener_state("failed_running")
                self._logger.exception(
                    "startup: Raw Input listener failed to start but is still running; "
                    "owner retained for cleanup to retry"
                )
                raise
            self._logger.info("startup: Raw Input listener failed to start: %s", exc)
            with self._raw_input_lifecycle_lock:
                if (
                    self._hid_listener is listener
                    and self._raw_input_generation == generation
                ):
                    self._hid_listener = None
                    self._raw_fallback_tracking_active = False
                    self._schedule_raw_input_recovery_locked()
            self._set_raw_listener_state("failed")
            return
        with self._input_arbitration_lock:
            with self._raw_input_lifecycle_lock:
                stale = (
                    self._raw_input_stopping
                    or not self._accept_input_events
                    or self._hid_listener is not listener
                    or self._raw_input_generation != generation
                )
                lost = self._raw_input_lost_generation == generation
                running = listener.is_running
                if not stale and (lost or not running):
                    self._schedule_raw_input_recovery_locked()
                elif not stale:
                    self._raw_input_retry_delay = (
                        _RAW_INPUT_RETRY_INITIAL_SECONDS
                    )
            if not stale:
                self._set_raw_listener_state("recovering" if lost or not running else "ready")
        if stale:
            if listener.is_running:
                try:
                    listener.stop()
                except Exception:
                    self._logger.exception(
                        "cleanup: stale Raw Input listener did not stop"
                    )
            return
        if lost or not running:
            return


    def _cancel_input_gestures(
        self,
        buttons: set[str],
        *,
        reason: str,
        block_until_release: bool,
    ) -> None:
        if block_until_release:
            self._block_input_until_release(buttons)
        self._button_combos.reset()
        self._button_gestures.reset()
        self._key_detection_suppressed_buttons.clear()
        self._key_detection_suppression_deadlines.clear()

        if "mic" in buttons:
            with self._ordinary_mic_lock:
                self._ordinary_mic_sources_down.clear()
                self._ordinary_mic_late_sources_down.clear()
                self._ordinary_mic_late_source_deadlines.clear()
                self._ordinary_mic_sources_seen.clear()
                self._ordinary_mic_gesture_started_at = 0.0
                self._ordinary_mic_release_guard_until = 0.0
                self._ordinary_mic_gesture_active = False
            with self._key_detection_mic_lock:
                self._reset_key_detection_mic_gesture_locked()
            with self._voice_shortcut.lock:
                self._voice_mic_gesture_sources_down.clear()
                self._voice_mic_gesture_hid_released = True
                if self._voice_shortcut.controller.active:
                    self._release_hold_voice_on_physical_release_locked(reason)
                if (
                    self._voice_mic_gesture_active
                    and not self._voice_audio_stream_active
                ):
                    self._finish_voice_mic_gesture()
                self._apply_pending_voice_settings_if_idle_locked()

        if buttons:
            self._logger.warning(
                "button input ownership cancelled: reason=%s buttons=%s",
                reason,
                sorted(buttons),
            )

    def _cancel_input_gestures_for_buttons(
        self,
        buttons: set[str],
        *,
        reason: str,
        block_until_release: bool,
    ) -> None:
        """Cancel only input state coupled to the named physical buttons."""

        affected_buttons = set(buttons)
        affected_buttons.update(
            self._button_combos.cancel_buttons(affected_buttons)
        )
        if block_until_release:
            self._block_input_until_release(affected_buttons)
        self._button_gestures.cancel_buttons(affected_buttons)
        self._key_detection_suppressed_buttons.difference_update(
            affected_buttons
        )
        for button_id in affected_buttons:
            self._key_detection_suppression_deadlines.pop(button_id, None)

        if "mic" in affected_buttons:
            with self._ordinary_mic_lock:
                self._ordinary_mic_sources_down.clear()
                self._ordinary_mic_late_sources_down.clear()
                self._ordinary_mic_late_source_deadlines.clear()
                self._ordinary_mic_sources_seen.clear()
                self._ordinary_mic_gesture_started_at = 0.0
                self._ordinary_mic_release_guard_until = 0.0
                self._ordinary_mic_gesture_active = False
            with self._key_detection_mic_lock:
                self._reset_key_detection_mic_gesture_locked()
            with self._voice_shortcut.lock:
                self._voice_mic_gesture_sources_down.clear()
                self._voice_mic_gesture_hid_released = True
                if self._voice_shortcut.controller.active:
                    self._release_hold_voice_on_physical_release_locked(reason)
                if (
                    self._voice_mic_gesture_active
                    and not self._voice_audio_stream_active
                ):
                    self._finish_voice_mic_gesture()
                self._apply_pending_voice_settings_if_idle_locked()

        if affected_buttons:
            self._logger.warning(
                "button input ownership cancelled: reason=%s buttons=%s",
                reason,
                sorted(affected_buttons),
            )

    def _take_raw_fallback_physical_buttons_locked(
        self,
        logical_buttons: set[str],
    ) -> set[str]:
        matched_signatures = {
            signature
            for signature, (logical_button, _physical_button) in (
                self._raw_fallback_physical_buttons_down.items()
            )
            if logical_button in logical_buttons
        }
        physical_buttons = {
            self._raw_fallback_physical_buttons_down[signature][1]
            for signature in matched_signatures
            if self._raw_fallback_physical_buttons_down[signature][1]
            in _RAW_FALLBACK_KEY_TOKENS
        }
        for signature in matched_signatures:
            tracked = self._raw_fallback_physical_buttons_down.pop(
                signature,
                None,
            )
            if tracked is not None:
                release_debt = self._raw_fallback_release_debts.setdefault(
                    signature,
                    (set(), set()),
                )
                debt_logical_buttons, debt_physical_buttons = release_debt
                debt_logical_buttons.add(tracked[0])
                if tracked[1] is not None:
                    debt_physical_buttons.add(tracked[1])

        # Compatibility for injected test owners and old listener fakes that
        # do not expose raw physical events. The real listener always enables
        # tracking before it can emit a logical edge, so production remaps
        # never guess the logical key here.
        if not self._raw_fallback_tracking_active:
            physical_buttons.update(
                logical_button
                for logical_button in logical_buttons
                if logical_button in _RAW_FALLBACK_KEY_TOKENS
            )
        return physical_buttons

    def _raw_fallback_tracked_button_ids_locked(
        self,
    ) -> tuple[set[str], set[str]]:
        logical_buttons = {
            logical_button
            for logical_button, _physical_button in (
                self._raw_fallback_physical_buttons_down.values()
            )
        }
        physical_buttons = {
            physical_button
            for _logical_button, physical_button in (
                self._raw_fallback_physical_buttons_down.values()
            )
            if physical_button in _RAW_FALLBACK_KEY_TOKENS
        }
        return logical_buttons, physical_buttons

    def _release_raw_fallback_keyups(
        self,
        physical_buttons: set[str],
        *,
        reason: str,
    ) -> None:
        tokens = tuple(
            dict.fromkeys(
                token
                for button in sorted(physical_buttons)
                if (token := _RAW_FALLBACK_KEY_TOKENS.get(button)) is not None
            )
        )
        if not tokens:
            return
        with self._button_action_lock:
            pending = self._button_key_release_pending or ()
            owed_tokens = tuple(dict.fromkeys((*pending, *tokens)))
            self._button_key_release_pending = owed_tokens
            if self._release_pending_button_keys():
                self._logger.info(
                    "raw Windows key-up safety release completed: reason=%s buttons=%s",
                    reason,
                    sorted(physical_buttons),
                )

    def _cancel_raw_fallback_hold_guard_locked(self, button_id: str) -> None:
        guard = self._raw_fallback_hold_guards.pop(button_id, None)
        if guard is None:
            return
        _token, timer = guard
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException as exc:  # noqa: BLE001 - token is already invalid
                self._logger.warning(
                    "raw fallback hold timer cancel failed: button=%s error_type=%s",
                    button_id,
                    type(exc).__name__,
                )

    def _cancel_all_raw_fallback_hold_guards_locked(self) -> None:
        for button_id in tuple(self._raw_fallback_hold_guards):
            self._cancel_raw_fallback_hold_guard_locked(button_id)

    def _arm_raw_fallback_hold_guard_locked(self, button_id: str) -> bool:
        self._cancel_raw_fallback_hold_guard_locked(button_id)
        token = object()
        try:
            timer = self._raw_fallback_timer_factory(
                button_gesture.ButtonGestureDispatcher.MAX_HOLD_SECONDS,
                lambda button_id=button_id, token=token: (
                    self._raw_fallback_hold_timeout(button_id, token)
                ),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._raw_fallback_hold_guards[button_id] = (token, timer)
            timer.start()
        except BaseException as exc:  # noqa: BLE001 - immediately release the key
            current = self._raw_fallback_hold_guards.get(button_id)
            if current is not None and current[0] is token:
                self._raw_fallback_hold_guards.pop(button_id, None)
            self._logger.error(
                "raw fallback hold timer failed to start: button=%s error_type=%s",
                button_id,
                type(exc).__name__,
            )
            return False
        return True

    def _raw_fallback_hold_timeout(self, button_id: str, token: object) -> None:
        with self._input_arbitration_lock:
            guard = self._raw_fallback_hold_guards.get(button_id)
            if guard is None or guard[0] is not token:
                return
            self._raw_fallback_hold_guards.pop(button_id, None)
            if button_id not in self._raw_fallback_buttons_down:
                return
            self._raw_fallback_buttons_down.discard(button_id)
            physical_buttons = self._take_raw_fallback_physical_buttons_locked(
                {button_id}
            )
            self._block_input_until_release(
                {button_id} | physical_buttons
            )
            self._release_raw_fallback_keyups(
                physical_buttons,
                reason="raw_fallback_hold_timeout",
            )
            self._logger.warning(
                "raw Windows key held for %.0fs; forced release until physical up: "
                "button=%s",
                button_gesture.ButtonGestureDispatcher.MAX_HOLD_SECONDS,
                button_id,
            )

    def _on_physical_keyboard_tracking_lost(
        self,
        reason: str,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        """End an unsafe ordinary-key HOLD; future downs fail closed."""

        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(
                _listener,
                _generation,
            ):
                return
            with self._raw_input_lifecycle_lock:
                resolved_generation = (
                    self._raw_input_generation
                    if _generation is None
                    else int(_generation)
                )
                self._raw_input_lost_generation = resolved_generation
                self._schedule_raw_input_recovery_locked()
            self._set_raw_listener_state("unhealthy")
            if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
                host = self._chromecast_runtime.voice_host
                if host is not None:
                    host.cancel_recording()
            with self._voice_shortcut.lock:
                tokens = tuple(self._voice_shortcut.hotkey.modifiers) + (
                    self._voice_shortcut.hotkey.key,
                )
                active_hold = (
                    self._voice_shortcut.controller.active
                    or self._voice_shortcut.pending_tokens is not None
                )
                if (self._remote_profile != remote_selection.CHROMECAST_PROFILE
                        and active_hold and not win32_input.can_begin_tracked_hold(tokens)):
                    self._force_voice_hold_release_locked(
                        "physical keyboard tracking lost"
                    )
            self._release_pending_button_keys()
        self._logger.warning(
            "physical keyboard tracking lost: reason=%s; unsafe held shortcuts disabled",
            reason,
        )

    def _on_voice_key_physicalizer_tracking_lost(
        self,
        physicalizer: Optional[
            voice_key_physicalizer_windows.VoiceKeyPhysicalizer
        ] = None,
        generation: Optional[int] = None,
    ) -> None:
        """Release an active marked right-Alt hold before the hook disappears."""

        with self._voice_key_physicalizer_lifecycle_lock:
            current = self._voice_key_physicalizer
            current_generation = self._voice_key_physicalizer_generation
            resolved_physicalizer = physicalizer or current
            resolved_generation = (
                current_generation if generation is None else int(generation)
            )
            if (
                resolved_physicalizer is None
                or current is not resolved_physicalizer
                or current_generation != resolved_generation
                or self._voice_key_physicalizer_stopping
            ):
                return
            self._voice_key_physicalizer_lost_generation = resolved_generation
            self._voice_key_physicalizer_ready = False
        self._set_runtime_input_state(
            voice_key_physicalizer_state="recovering"
        )
        if self._remote_profile == remote_selection.CHROMECAST_PROFILE:
            host = self._chromecast_runtime.voice_host
            if host is not None:
                wait_for_release = getattr(
                    host,
                    "cancel_recording_and_wait",
                    None,
                )
                if callable(wait_for_release):
                    wait_for_release(2.5)
                else:
                    host.cancel_recording()
            with self._voice_key_physicalizer_lifecycle_lock:
                if (
                    self._voice_key_physicalizer is resolved_physicalizer
                    and self._voice_key_physicalizer_generation
                    == resolved_generation
                    and not self._voice_key_physicalizer_stopping
                ):
                    self._schedule_voice_key_physicalizer_recovery_locked()
            self._logger.warning(
                "voice key physicalizer stopped unexpectedly; "
                "Chromecast voice cancelled and recovery scheduled"
            )
            return
        raw_tracking_available = (
            raw_input_windows.physical_keyboard_tracking_available()
        )
        with self._voice_shortcut.lock:
            tokens = tuple(self._voice_shortcut.hotkey.modifiers) + (
                self._voice_shortcut.hotkey.key,
            )
            try:
                uses_right_alt = (
                    win32_keys.VK_CODES["ralt"]
                    in win32_keys.resolve_vk_codes(tokens)
                )
            except win32_keys.UnknownKeyTokenError:
                uses_right_alt = False
            active_hold = (
                self._voice_shortcut.controller.active
                or self._voice_shortcut.pending_tokens is not None
            )
            if (
                active_hold
                and (
                    not raw_tracking_available
                    or (
                        uses_right_alt
                        and self._configured_voice_hotkey_backend()
                        in {
                            _VOICE_HOTKEY_BACKEND_MARKED,
                            _VOICE_HOTKEY_BACKEND_DOUBAO,
                        }
                    )
                )
            ):
                self._force_voice_hold_release_locked(
                    "voice key physicalizer stopped unexpectedly"
                )
        if not raw_tracking_available:
            self._release_pending_button_keys()
        with self._voice_key_physicalizer_lifecycle_lock:
            if (
                self._voice_key_physicalizer is resolved_physicalizer
                and self._voice_key_physicalizer_generation
                == resolved_generation
                and not self._voice_key_physicalizer_stopping
            ):
                self._schedule_voice_key_physicalizer_recovery_locked()
        self._logger.warning(
            "voice key physicalizer stopped unexpectedly; recovery scheduled"
        )

    def _on_voice_key_physicalizer_health_failure(
        self,
        physicalizer: voice_key_physicalizer_windows.VoiceKeyPhysicalizer,
        generation: int,
        reason: str,
        snapshot: voice_key_physicalizer_windows.PhysicalizerHealthSnapshot,
    ) -> None:
        """Close admission immediately and recover only after owned cleanup."""

        with self._voice_key_physicalizer_lifecycle_lock:
            if (
                self._voice_key_physicalizer is not physicalizer
                or self._voice_key_physicalizer_generation != int(generation)
                or self._voice_key_physicalizer_stopping
            ):
                return
            self._voice_key_physicalizer_ready = False
            self._voice_key_physicalizer_degraded_retry_deadline = (
                time.monotonic()
                + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
            )
            self._schedule_voice_key_physicalizer_recovery_locked()
        self._set_runtime_input_state(
            voice_key_physicalizer_state="recovering"
        )
        try:
            self._diagnostic_trace.emit(
                "voice_key_physicalizer_health",
                status="degraded",
                reason=str(reason),
                app_generation=int(generation),
                **snapshot.trace_fields(),
            )
        except BaseException:
            pass
        self._logger.warning(
            "voice key physicalizer degraded after required acknowledgement "
            "failure; new voice starts blocked until owned cleanup completes: "
            "generation=%s tracker_generation=%s callback_delta=%s marker_delta=%s",
            generation,
            snapshot.generation,
            snapshot.receipt_callback_entry_delta,
            snapshot.receipt_marker_callback_delta,
        )

    def _stop_input_channels(self) -> None:
        """Stop process-lifetime input resources exactly once at worker exit."""

        failures: List[str] = []
        self._stop_physical_key_tristate()
        with self._voice_key_physicalizer_lifecycle_lock:
            self._voice_key_physicalizer_stopping = True
            self._voice_key_physicalizer_generation += 1
            self._voice_key_physicalizer_degraded_retry_deadline = 0.0
            self._cancel_voice_key_physicalizer_retry_locked()
        with self._input_arbitration_lock:
            with self._raw_input_lifecycle_lock:
                self._raw_input_stopping = True
                self._raw_input_generation += 1
                self._cancel_raw_input_retry_locked()
        with self._button_action_lock:
            self._button_input_release_retry_stopping = True
            self._cancel_button_input_release_retry_locked(reset_delay=False)
        with self._voice_shortcut.lock:
            self._voice_shortcut.retry_stopping = True
            self._voice_shortcut.cancel_release_retry(reset_delay=False)

        # Input loss is cancellation, not a click. Clear gesture timers before
        # listener shutdown emits forced releases, and release any Windows key
        # whose original down was deliberately allowed through during fallback.
        self._button_combos.reset()
        self._button_gestures.reset()
        with self._input_arbitration_lock:
            self._cancel_all_raw_fallback_hold_guards_locked()
            tracked_logical_buttons, _tracked_physical_buttons = (
                self._raw_fallback_tracked_button_ids_locked()
            )
            raw_fallback_physical_buttons = (
                self._take_raw_fallback_physical_buttons_locked(
                    set(self._raw_fallback_buttons_down)
                    | tracked_logical_buttons
                )
            )
            self._release_raw_fallback_keyups(
                raw_fallback_physical_buttons,
                reason="input_channels_stopping",
            )

        # Keep dispatch enabled until the listener has emitted its forced
        # release edges. The in-process HID tap owner was removed with that branch;
        # the state string stays because ChromecastRuntime publishes it.
        self._set_runtime_input_state(hid_tap_state="stopped")

        keyboard_release_complete = True
        if not self._release_pending_button_keys():
            keyboard_release_complete = False
            failures.append(
                "ordinary button key safety release did not fully deliver; "
                "keyboard trackers retained"
            )
        if not self._release_pending_button_mouse():
            failures.append(
                "ordinary button mouse safety release did not fully deliver; "
                "state retained"
            )

        with self._voice_shortcut.lock:
            if self._voice_shortcut.pending_tokens is not None:
                if self._voice_shortcut.release_pending():
                    self._voice_shortcut.controller.cancel_pending()
                else:
                    keyboard_release_complete = False
                    failures.append(
                        "voice hotkey final safety release did not fully deliver; "
                        "keyboard trackers retained"
                    )

        if keyboard_release_complete:
            with self._raw_input_operation_lock:
                listener = self._hid_listener
                if listener is not None:
                    try:
                        listener.stop()
                        with self._raw_input_lifecycle_lock:
                            if self._hid_listener is listener:
                                self._hid_listener = None
                                self._raw_fallback_tracking_active = False
                        self._set_runtime_input_state(raw_input_state="stopped")
                    except Exception:
                        self._set_runtime_input_state(
                            raw_input_state="failed_stopping"
                        )
                        self._logger.exception(
                            "cleanup: stopping the Raw Input listener failed"
                        )
                        failures.append(
                            "Raw Input listener did not stop; owner retained"
                        )
                else:
                    self._set_runtime_input_state(raw_input_state="stopped")
        else:
            self._set_runtime_input_state(raw_input_state="retained_for_cleanup")

        with self._voice_shortcut.lock:
            self._accept_input_events = False

        with self._direct_hid_lock:
            self._direct_hid_usages.clear()
        with self._input_arbitration_lock:
            self._direct_hid_interception_ready = False
            self._direct_hid_interception_armed = False
            self._direct_hid_handover_waiting_for_neutral = False
            self._cancel_all_raw_fallback_hold_guards_locked()
            self._raw_fallback_buttons_down.clear()
            self._raw_fallback_physical_buttons_down.clear()
            self._raw_fallback_release_debts.clear()
            self._input_rearm_blocked_buttons.clear()
        self._key_detection_suppressed_buttons.clear()
        self._key_detection_suppression_deadlines.clear()
        with self._ordinary_mic_lock:
            self._ordinary_mic_sources_down.clear()
            self._ordinary_mic_late_sources_down.clear()
            self._ordinary_mic_late_source_deadlines.clear()
            self._ordinary_mic_sources_seen.clear()
            self._ordinary_mic_gesture_started_at = 0.0
            self._ordinary_mic_release_guard_until = 0.0
            self._ordinary_mic_gesture_active = False
        with self._key_detection_mic_lock:
            self._reset_key_detection_mic_gesture_locked()
        self._button_combos.reset()
        self._button_gestures.reset()

        physicalizer_state = "stopped"
        if keyboard_release_complete:
            try:
                self._voice_shortcut.doubao_physicalizer.stop()
            except Exception:
                keyboard_release_complete = False
                physicalizer_state = "failed"
                self._logger.exception(
                    "cleanup: stopping Doubao voice physicalizer failed"
                )
                failures.append(
                    "Doubao voice physicalizer did not stop; owner retained"
                )

        if keyboard_release_complete:
            with self._voice_key_physicalizer_operation_lock:
                with self._voice_key_physicalizer_lifecycle_lock:
                    physicalizer = self._voice_key_physicalizer
                if physicalizer is not None:
                    try:
                        physicalizer.stop()
                    except Exception:
                        physicalizer_state = "failed"
                        self._logger.exception(
                            "cleanup: stopping voice key physicalizer failed"
                        )
                        failures.append(
                            "voice key physicalizer did not stop; owner retained"
                        )
                    else:
                        with self._voice_key_physicalizer_lifecycle_lock:
                            if self._voice_key_physicalizer is physicalizer:
                                self._voice_key_physicalizer = None
        else:
            physicalizer_state = "failed"
        with self._voice_key_physicalizer_lifecycle_lock:
            self._voice_key_physicalizer_ready = False
        self._set_runtime_input_state(
            voice_key_physicalizer_state=physicalizer_state
        )

        if failures:
            raise CleanupIncompleteError(
                "cleanup could not release all owned input resources: "
                + "; ".join(failures)
            )

    # -- disconnect / error callbacks: hand off to the supervisor ----------

    def _primary_button_action(self, button_id: str) -> key_mapping.ButtonAction:
        return key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger.SINGLE_CLICK,
        )

    def _configured_voice_buttons(self) -> List[str]:
        button_bindings = self._bindings.get("bindings", {})
        if not isinstance(button_bindings, dict):
            return []
        result = []
        for button_id in button_bindings:
            action = self._primary_button_action(button_id)
            if button_id == "mic" and key_mapping.is_supported_voice_action(action):
                result.append(button_id)
        return result

    def _voice_mode_for_primary_button(
        self,
        button_id: str,
        action: Optional[key_mapping.ButtonAction] = None,
    ) -> Optional[key_mapping.VoiceTriggerMode]:
        action = action or self._primary_button_action(button_id)
        if button_id != "mic" or not key_mapping.is_supported_voice_action(action):
            return None
        return key_mapping.VoiceTriggerMode.HOLD


    def _configured_voice_hotkey_backend(self) -> str:
        settings = voice_program_manager.normalize_voice_program_settings(
            self._config.get("voice_program")
        )
        if settings["provider"] == voice_program_manager.VOICE_PROGRAM_WETYPE:
            return _VOICE_HOTKEY_BACKEND_WETYPE
        if settings["provider"] == voice_program_manager.VOICE_PROGRAM_DOUBAO_IME:
            return _VOICE_HOTKEY_BACKEND_DOUBAO
        if settings["provider"] == voice_program_manager.VOICE_PROGRAM_CHATTERFLY:
            return _VOICE_HOTKEY_BACKEND_CHATTERFLY
        return _VOICE_HOTKEY_BACKEND_MARKED

    def _ensure_voice_key_physicalizer_for_hotkey(
        self,
        tokens: Tuple[str, ...],
        backend: str,
        *,
        required: bool = False,
    ) -> bool:
        """Prepare the keyboard hook only for backends that send key events.

        ``required`` forces the hook for shortcuts that never touch right Alt
        but still need marked delivery: the physical-key tri-state sends the
        configured voice shortcut itself, so without the hook its marked
        keybd_event is refused and the hold silently does nothing.
        """

        if backend not in {
            _VOICE_HOTKEY_BACKEND_MARKED,
            _VOICE_HOTKEY_BACKEND_DOUBAO,
        }:
            return True
        try:
            uses_right_alt = (
                win32_keys.VK_CODES["ralt"]
                in win32_keys.resolve_vk_codes(tokens)
            )
        except win32_keys.UnknownKeyTokenError:
            return False
        if not uses_right_alt and not required:
            return True
        with self._voice_key_physicalizer_lifecycle_lock:
            ready = self._voice_key_physicalizer_ready
            physicalizer = self._voice_key_physicalizer
            accepts_new_down = bool(
                physicalizer is not None
                and getattr(physicalizer, "accepts_new_down", True)
            )
            if ready and not accepts_new_down:
                self._voice_key_physicalizer_ready = False
                ready = False
                if not self._voice_key_physicalizer_degraded_retry_deadline:
                    self._voice_key_physicalizer_degraded_retry_deadline = (
                        time.monotonic()
                        + _VOICE_KEY_PHYSICALIZER_DEGRADED_RETRY_WINDOW_SECONDS
                    )
                self._schedule_voice_key_physicalizer_recovery_locked()
        if not ready:
            self._start_voice_key_physicalizer()
        with self._voice_key_physicalizer_lifecycle_lock:
            physicalizer = self._voice_key_physicalizer
            return bool(
                self._voice_key_physicalizer_ready
                and physicalizer is not None
                and getattr(physicalizer, "accepts_new_down", True)
            )

    def _finish_voice_mic_gesture(self) -> None:
        """Release the current cross-source mic gesture latch."""

        self._cancel_voice_hold_watchdog_locked()
        self._voice_mic_gesture_active = False
        self._voice_mic_gesture_audio_started = False
        self._voice_mic_gesture_audio_stopped = False
        self._voice_mic_gesture_physical_seen = False
        self._voice_mic_gesture_hid_released = False
        self._voice_mic_gesture_direct_hid_seen = False
        self._voice_mic_gesture_sources_down.clear()

    def _cancel_voice_hold_watchdog_locked(self) -> None:
        timer = self._voice_hold_watchdog_timer
        self._voice_hold_watchdog_timer = None
        self._voice_hold_watchdog_token = None
        if timer is not None:
            cancel = getattr(timer, "cancel", None)
            if callable(cancel):
                try:
                    cancel()
                except BaseException as exc:  # noqa: BLE001 - token is already invalid
                    self._logger.warning(
                        "voice hold safety timer cancel failed: error_type=%s",
                        type(exc).__name__,
                    )


    def _force_voice_hold_release_locked(self, reason: str) -> bool:
        self._voice_pcm_forwarding_enabled = False
        self._voice_pcm_min_arrival_sequence = None
        self._voice_audio_stream_active = False
        self._voice_audio_stop_processed = True
        self._voice_audio_start_fallback_pending = False
        self._voice_mic_gesture_sources_down.clear()
        self._voice_mic_gesture_hid_released = True

        action = self._voice_shortcut.controller.reset()
        released = True
        doubao_cleanup_requested = False
        if action is not None:
            released = self._voice_shortcut.apply(action)
            if not released:
                self._voice_shortcut.controller.restore_pending(action)
        elif self._voice_shortcut.pending_tokens is not None:
            released = self._voice_shortcut.release_pending()

        self._finish_voice_mic_gesture()
        if released and doubao_cleanup_requested:
            self._set_runtime_voice_active(False)
        elif released:
            self._set_runtime_voice_active(False)
            self._log_voice_submission_observation()
            self._finish_voice_diagnostic_attempt(
                "unknown",
                reason="forced_host_release",
            )
            self._apply_pending_voice_settings_if_idle_locked()
        else:
            self._logger.error(
                "%s; reconnect cleanup will retry the host release",
                reason,
            )
        return released

    def _release_hold_voice_on_physical_release_locked(
        self,
        reason: str,
    ) -> bool:
        """Release HOLD shortcuts without depending solely on AUDIO_STOP."""

        action = self._voice_shortcut.controller.on_mic_button_released()
        if action is None:
            self._cancel_voice_hold_watchdog_locked()
            return True
        self._voice_audio_start_fallback_pending = False
        self._voice_pcm_forwarding_enabled = False
        self._voice_pcm_min_arrival_sequence = None
        self._voice_pcm_min_arrival_sequence = None
        if self._voice_shortcut.apply(action):
            self._cancel_voice_hold_watchdog_locked()
            self._logger.info("voice hold hotkey released on %s", reason)
            if not self._voice_audio_stream_active:
                self._set_runtime_voice_active(False)
                # An attempt with audio sending disabled has no sink by
                # design, so silence is expected and not an empty-audio fault.
                self._set_runtime_voice_result(
                    bridge_runtime_status.VOICE_RUNTIME_NOT_TESTED
                    if self._config.get("disable_audio_sending", False)
                    else bridge_runtime_status.VOICE_RUNTIME_AUDIO_EMPTY
                )
                self._log_voice_submission_observation()
                self._finish_voice_diagnostic_attempt(
                    "unknown",
                    reason="physical_release_without_audio_stop",
                    audio_signal=False,
                )
            return True

        self._voice_shortcut.controller.restore_pending(action)
        self._set_runtime_voice_result(
            bridge_runtime_status.VOICE_RUNTIME_HOST_STOP_FAILED
        )
        self._logger.info(
            "voice hold hotkey release failed on %s; state retained, "
            "requesting reconnect",
            reason,
        )
        return False

    def _reset_key_detection_mic_gesture_locked(self) -> None:
        self._key_detection_mic_gesture_active = False
        self._key_detection_mic_gesture_started_at = 0.0
        self._key_detection_mic_release_deadline = None
        self._key_detection_mic_audio_started = False
        self._key_detection_mic_sources_down.clear()

    def _expire_key_detection_mic_gesture_locked(self, now: float) -> None:
        if not self._key_detection_mic_gesture_active:
            return
        release_deadline = self._key_detection_mic_release_deadline
        hard_deadline = (
            self._key_detection_mic_gesture_started_at
            + _KEY_DETECTION_MIC_MAX_SECONDS
        )
        if (release_deadline is not None and now >= release_deadline) or (
            now >= hard_deadline
        ):
            self._reset_key_detection_mic_gesture_locked()

    def _handle_key_detection_mic_event(
        self,
        event_kind: str,
        source: str,
    ) -> Tuple[bool, bool]:
        """Capture or suppress one source from a detected physical mic press.

        A single Chromecast mic gesture is reported by multiple independent paths.
        The key-detection request disappears as soon as the first path claims
        it, so a short in-process latch must keep later paths from entering the
        normal voice state machine. Returns ``(handled, newly_captured)``.
        """

        now = time.monotonic()
        starts_gesture = event_kind in {
            "physical_down",
            "atvv_press",
            "audio_started",
        }
        with self._voice_shortcut.lock:
            # A detection request may appear while a real voice press is still
            # active. Its late HID/F5/audio edges belong to that owned press and
            # must remain available to close the host shortcut. Leave the
            # request pending for the next independent press instead.
            if self._voice_mic_gesture_active:
                return False, False
            with self._key_detection_mic_lock:
                self._expire_key_detection_mic_gesture_locked(now)
                newly_captured = False
                if not self._key_detection_mic_gesture_active:
                    if not starts_gesture:
                        return False, False
                    try:
                        newly_captured = key_detection_bridge.publish_next_button(
                            self._config_root,
                            "mic",
                        )
                    except OSError as exc:
                        self._logger.warning("key detection IPC unavailable: %s", exc)
                        return False, False
                    if not newly_captured:
                        return False, False
                    self._key_detection_mic_gesture_active = True
                    self._key_detection_mic_gesture_started_at = now

                if event_kind == "physical_down":
                    self._key_detection_mic_sources_down.add(source)
                    self._key_detection_mic_release_deadline = None
                elif event_kind == "physical_up":
                    self._key_detection_mic_sources_down.discard(source)
                    if (
                        not self._key_detection_mic_sources_down
                        and not self._key_detection_mic_audio_started
                    ):
                        self._key_detection_mic_release_deadline = (
                            now + _KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS
                        )
                elif event_kind == "audio_started":
                    self._key_detection_mic_audio_started = True
                    self._key_detection_mic_release_deadline = None
                elif event_kind == "audio_stopped":
                    self._key_detection_mic_audio_started = False
                    if not self._key_detection_mic_sources_down:
                        self._key_detection_mic_release_deadline = (
                            now + _KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS
                        )
                elif not self._key_detection_mic_sources_down:
                    # MicButtonPressed has no matching release opcode. Give the
                    # physical/audio paths time to join, then let a future press
                    # through even if neither companion event ever arrives.
                    self._key_detection_mic_release_deadline = (
                        now + _KEY_DETECTION_MIC_RELEASE_GRACE_SECONDS
                    )
                return True, newly_captured

    def _on_raw_button_event(
        self,
        button_id: str,
        is_pressed: bool,
        source: str = "unknown",
        windows_button_id: Optional[str] = None,
        *,
        _listener: Optional[raw_input_windows.RawInputButtonListener] = None,
        _generation: Optional[int] = None,
    ) -> None:
        event_source = {
            "keyboard": "raw_keyboard",
            "hid": "raw_hid",
        }.get(source, "raw_unknown")
        with self._input_arbitration_lock:
            if not self._raw_input_callback_is_current(_listener, _generation):
                return
            self._on_button_event_serialized(
                button_id,
                is_pressed,
                event_source=event_source,
                raw_windows_button_id=windows_button_id,
            )

    # -- HID button events --------------------------------------------------

    @staticmethod
    def _settings_file_mtime_ns(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return -1

    def _voice_settings_idle_locked(self) -> bool:
        chromecast_host = self._chromecast_runtime.voice_host
        return not (
            self._voice_shortcut.controller.active
            or self._voice_mic_gesture_active
            or self._voice_audio_stream_active
            or self._ordinary_mic_gesture_active
            or self._voice_shortcut.pending_tokens is not None
            or (
                chromecast_host is not None
                and getattr(chromecast_host, "_settings_claimed", False)
            )
        )

    def _ordinary_button_mappings_idle(self) -> bool:
        return not (
            self._button_gestures.has_active_gestures()
            or self._button_combos.has_active_combo()
        )

    def _apply_voice_settings_locked(
        self,
        trigger_mode: key_mapping.VoiceTriggerMode,
        voice_hotkey: hotkey.HotkeySpec,
    ) -> None:
        if trigger_mode != key_mapping.VoiceTriggerMode.HOLD:
            raise ValueError("voice settings support hold-to-talk only")
        self._voice_shortcut.controller = voice_controller.VoiceController()
        self._voice_shortcut.hotkey = voice_hotkey
        self._config["voice_trigger_mode"] = trigger_mode.value
        self._config["voice_hotkey"] = voice_hotkey.serialize()
        # The observer presses whatever shortcut is configured, so a changed
        # voice shortcut has to reach it.
        self._sync_physical_key_tristate()
        self._logger.info(
            "settings voice configuration applied: trigger_mode=%s hotkey=%s",
            trigger_mode.value,
            voice_hotkey.serialize(),
        )

    @staticmethod
    def _output_endpoint_of(settings: dict) -> Tuple[str, str]:
        return (
            settings.get("output_endpoint_name") or "",
            settings.get("output_endpoint_host_api") or "",
        )

    def _output_endpoint_switch_pending(self, refreshed_config: dict) -> bool:
        """True when the sink in hand no longer matches the saved endpoint.

        A sink only exists once a voice session has opened one, so a None sink
        needs no action: the next open() reads the new name on its own.
        """

        if self._voice_audio.sink is None:
            return False
        return self._output_endpoint_of(refreshed_config) != self._output_endpoint_of(
            self._config
        )

    def _retire_output_endpoint_locked(self) -> bool:
        """Drop the bound sink so the next open() picks up the new endpoint.

        The writer must already be gone; closing under an active writer would
        pull the PortAudio stream out from under it.
        """

        if self._voice_audio.writer is not None:
            return False
        if not self._voice_audio.stop_writer():
            return False
        try:
            self._voice_audio.close_sink()
        except Exception:
            self._logger.exception("output endpoint switch: sink close failed")
            return False
        return True

    def _apply_pending_voice_settings_if_idle_locked(self) -> None:
        if not self._voice_settings_idle_locked():
            return
        if self._pending_output_endpoint is not None:
            previous = self._output_endpoint_of(self._config)
            target = self._pending_output_endpoint
            if self._retire_output_endpoint_locked():
                self._pending_output_endpoint = None
                self._logger.info(
                    "deferred output endpoint switch applied: from=%s to=%s",
                    previous[0] or "unspecified",
                    target[0] or "unspecified",
                )
        if self._pending_config is not None:
            self._config = self._pending_config
            self._pending_config = None
        if self._pending_voice_settings is not None:
            trigger_mode, voice_hotkey = self._pending_voice_settings
            self._pending_voice_settings = None
            self._apply_voice_settings_locked(trigger_mode, voice_hotkey)
        if (
            self._pending_bindings is not None
        ):
            with self._button_mapping_lock:
                if self._ordinary_button_mappings_idle():
                    self._button_combos.reset()
                    self._bindings = self._pending_bindings
                    self._pending_bindings = None
                    self._removed_voice_bindings = dict(
                        self._bindings.get(
                            config.RUNTIME_REMOVED_VOICE_BINDINGS_KEY, {}
                        )
                    )
                    self._logger.info(
                        "deferred settings mappings applied after active input became idle"
                    )
                    self._diagnostic_trace.emit("voice_configuration_applied", result="recovered")

    def _apply_pending_settings_if_idle(self) -> None:
        with self._voice_shortcut.lock:
            self._apply_pending_voice_settings_if_idle_locked()

    def _reload_settings_if_changed(self) -> None:
        """Apply mapping and voice-setting edits without a bridge restart."""

        current_config_mtime_ns = self._settings_file_mtime_ns(self._config_path)
        current_bindings_mtime_ns = self._settings_file_mtime_ns(self._bindings_path)
        if (
            current_config_mtime_ns == self._config_mtime_ns
            and current_bindings_mtime_ns == self._bindings_mtime_ns
        ):
            return
        try:
            refreshed_config = config.load_config(self._config_path)
            from . import remote_selection
            if remote_selection.active_key(refreshed_config) != remote_selection.active_key(self._config):
                raise remote_selection.SelectionError("设备选择已变化，须先停止旧服务再切换。")
            # Ducking is a live process flag, not bridge state: apply it as
            # soon as the file changes instead of waiting for an idle voice.
            system_playback_mute.set_enabled(
                bool(refreshed_config.get("audio_ducking_enabled", False))
            )
            refreshed_bindings = config.load_key_bindings(self._bindings_path)
            removed_voice_bindings = config.normalize_voice_product_boundary(
                refreshed_config,
                refreshed_bindings,
            )
            trigger_mode = key_mapping.VoiceTriggerMode.HOLD
            refreshed_hotkey_text = _runtime_voice_hotkey_text(refreshed_config)
            voice_hotkey = hotkey.HotkeySpec.parse(refreshed_hotkey_text)
        except Exception as exc:  # noqa: BLE001 - keep the last valid settings
            self._logger.warning("settings reload skipped: %s", exc)
            with self._voice_shortcut.lock:
                self._config_mtime_ns = current_config_mtime_ns
                self._bindings_mtime_ns = current_bindings_mtime_ns
            return

        endpoint_switch_pending = self._output_endpoint_switch_pending(
            refreshed_config
        )

        self._diagnostic_trace.set_enabled(
            bool(refreshed_config.get("diagnostic_trace_enabled", False))
        )
        self._record_device_diagnostic_context()
        with self._voice_shortcut.lock:
            with self._button_mapping_lock:
                refreshed_settings = (trigger_mode, voice_hotkey.serialize())
                current_settings = (
                    self._voice_shortcut.controller.trigger_mode,
                    self._voice_shortcut.hotkey.serialize(),
                )
                if self._voice_settings_idle_locked():
                    self._config = refreshed_config
                    self._pending_config = None
                    self._pending_voice_settings = None
                    if endpoint_switch_pending:
                        # The sink is idle, so it can be dropped right here and
                        # the next voice session opens the saved endpoint.
                        target = self._output_endpoint_of(refreshed_config)
                        if self._retire_output_endpoint_locked():
                            self._pending_output_endpoint = None
                            self._logger.info(
                                "output endpoint switch applied: to=%s",
                                target[0] or "unspecified",
                            )
                        else:
                            self._pending_output_endpoint = target
                    if refreshed_settings != current_settings:
                        self._apply_voice_settings_locked(trigger_mode, voice_hotkey)
                    if self._ordinary_button_mappings_idle():
                        self._button_combos.reset()
                        self._bindings = refreshed_bindings
                        self._removed_voice_bindings = removed_voice_bindings
                        self._pending_bindings = None
                    else:
                        self._pending_bindings = refreshed_bindings
                        self._logger.info(
                            "settings mapping reload deferred until active input is idle"
                        )
                else:
                    self._pending_config = refreshed_config
                    self._pending_bindings = refreshed_bindings
                    self._pending_voice_settings = (trigger_mode, voice_hotkey)
                    if endpoint_switch_pending:
                        # A live session owns the PortAudio stream; retire the
                        # sink once it goes idle instead of mid-playback.
                        self._pending_output_endpoint = self._output_endpoint_of(
                            refreshed_config
                        )
                    self._logger.info(
                        "settings reload deferred until active voice session is idle"
                    )
                    self._diagnostic_trace.emit("voice_configuration_deferred",
                        release_pending=self._voice_shortcut.pending_tokens is not None,
                        reason="active_voice_not_idle", **self._diagnostic_trace.current_context())
                self._config_mtime_ns = current_config_mtime_ns
                self._bindings_mtime_ns = current_bindings_mtime_ns
        # A saved edit can turn the observer on or off, or change its key.
        self._sync_physical_key_tristate()
        if removed_voice_bindings:
            self._logger.warning(
                "legacy voice mappings disabled until user reselects actions: %s",
                sorted(removed_voice_bindings),
            )
        if self._pending_bindings is None:
            self._logger.info("settings reloaded from disk")

    def request_settings_reload_now(self) -> None:
        """Apply saved settings promptly from the bridge event-loop thread."""

        self._event_loop.call_soon_threadsafe(self._reload_settings_if_changed)

    def _handle_ordinary_mic_edge(
        self, event_source: str, is_pressed: bool
    ) -> None:
        """Collapse F5/HID duplicates before ordinary mic gesture dispatch."""

        dispatch = False
        cancel_stale_gesture = False
        now = time.monotonic()
        with self._ordinary_mic_lock:
            expired_late_sources = {
                source
                for source, deadline in self._ordinary_mic_late_source_deadlines.items()
                if now >= deadline
            }
            if expired_late_sources:
                self._ordinary_mic_late_sources_down.difference_update(
                    expired_late_sources
                )
                for source in expired_late_sources:
                    self._ordinary_mic_late_source_deadlines.pop(source, None)

            stale_active_gesture = self._ordinary_mic_gesture_active and (
                now - self._ordinary_mic_gesture_started_at
                >= _ORDINARY_MIC_MAX_SECONDS
                or (
                    is_pressed
                    and now - self._ordinary_mic_gesture_started_at
                    >= _ORDINARY_MIC_SOURCE_STALE_SECONDS
                )
            )
            if stale_active_gesture:
                self._ordinary_mic_sources_down.clear()
                self._ordinary_mic_late_sources_down.clear()
                self._ordinary_mic_late_source_deadlines.clear()
                self._ordinary_mic_sources_seen.clear()
                self._ordinary_mic_gesture_started_at = 0.0
                self._ordinary_mic_release_guard_until = 0.0
                self._ordinary_mic_gesture_active = False
                cancel_stale_gesture = True

            if is_pressed:
                if (
                    event_source in self._ordinary_mic_sources_down
                    or event_source in self._ordinary_mic_late_sources_down
                ):
                    return
                if self._ordinary_mic_sources_down:
                    self._ordinary_mic_sources_down.add(event_source)
                    self._ordinary_mic_sources_seen.add(event_source)
                    return
                now = time.monotonic()
                if (
                    now < self._ordinary_mic_release_guard_until
                    and event_source not in self._ordinary_mic_sources_seen
                ):
                    self._ordinary_mic_late_sources_down.add(event_source)
                    self._ordinary_mic_late_source_deadlines[event_source] = (
                        now + _ORDINARY_MIC_SOURCE_STALE_SECONDS
                    )
                    self._ordinary_mic_sources_seen.add(event_source)
                    self._logger.info(
                        "ordinary mic late duplicate ignored: source=%s",
                        event_source,
                    )
                    return
                self._ordinary_mic_sources_down.add(event_source)
                self._ordinary_mic_sources_seen = {event_source}
                self._ordinary_mic_gesture_started_at = now
                self._ordinary_mic_release_guard_until = 0.0
                dispatch = True
            else:
                if event_source in self._ordinary_mic_late_sources_down:
                    self._ordinary_mic_late_sources_down.discard(event_source)
                    self._ordinary_mic_late_source_deadlines.pop(event_source, None)
                    return
                if event_source not in self._ordinary_mic_sources_down:
                    return
                self._ordinary_mic_sources_down.discard(event_source)
                dispatch = not self._ordinary_mic_sources_down
                if dispatch:
                    self._ordinary_mic_release_guard_until = (
                        now + _ORDINARY_MIC_RELEASE_GUARD_SECONDS
                    )
                    self._ordinary_mic_gesture_started_at = 0.0
            self._ordinary_mic_gesture_active = bool(
                self._ordinary_mic_sources_down
            )
        if cancel_stale_gesture:
            self._button_gestures.reset()
        if not dispatch:
            return
        with self._button_mapping_lock:
            if is_pressed:
                self._button_gestures.press("mic")
            else:
                self._button_gestures.release("mic")

    def _on_button_event(
        self,
        button_id: str,
        is_pressed: bool,
        *,
        event_source: str = "hid",
        raw_windows_button_id: Optional[str] = None,
        hid_usage: Optional[int] = None,
    ) -> None:
        with self._input_arbitration_lock:
            self._on_button_event_serialized(
                button_id,
                is_pressed,
                event_source=event_source,
                raw_windows_button_id=raw_windows_button_id,
                hid_usage=hid_usage,
            )

    def _on_button_event_serialized(
        self,
        button_id: str,
        is_pressed: bool,
        *,
        event_source: str,
        raw_windows_button_id: Optional[str] = None,
        hid_usage: Optional[int] = None,
    ) -> None:
        if not self._accept_input_events:
            return
        gesture_id = (
            self._diagnostic_trace.current_gesture(button_id)
            if event_source.startswith("raw_")
            else None
        )
        if not gesture_id:
            gesture_id = self._diagnostic_trace.begin_gesture(
                button_id,
                event_source,
                bool(is_pressed),
                usage=int(hid_usage) if hid_usage is not None else -1,
            )
        self._diagnostic_trace.set_current_gesture(gesture_id)
        self._diagnostic_trace.emit(
            "logical_button_event",
            gesture_id=gesture_id,
            button_id=str(button_id),
            source=str(event_source),
            edge="down" if is_pressed else "up",
            usage=int(hid_usage) if hid_usage is not None else -1,
        )
        now = time.monotonic()
        with self._input_arbitration_lock:
            if button_id in self._input_rearm_blocked_buttons:
                if not is_pressed:
                    self._input_rearm_blocked_buttons.discard(button_id)
                return
        if event_source == "hid_tap":
            with self._input_arbitration_lock:
                if not self._direct_hid_interception_ready:
                    return
        raw_mapping_bypassed = False
        if event_source.startswith("raw_"):
            with self._input_arbitration_lock:
                if (
                    self._direct_hid_interception_armed
                    or self._direct_hid_interception_ready
                ):
                    return

                # Raw Input can observe Windows' original key event, but it
                # cannot suppress that event. Until the HID tap owns the
                # report, every custom mapping must therefore fail closed.
                raw_mapping_bypassed = True

                if button_id in self._raw_fallback_buttons_down:
                    if not is_pressed:
                        self._cancel_raw_fallback_hold_guard_locked(button_id)
                        self._raw_fallback_buttons_down.discard(button_id)

                else:
                    effective_windows_button = raw_windows_button_id
                    if (
                        effective_windows_button is None
                        and not self._raw_fallback_tracking_active
                    ):
                        effective_windows_button = button_id
                    preserve_windows_original = (
                        effective_windows_button in _RAW_FALLBACK_KEY_TOKENS
                    )
                    if preserve_windows_original:
                        if is_pressed:
                            self._raw_fallback_buttons_down.add(button_id)
                            if not self._arm_raw_fallback_hold_guard_locked(button_id):
                                self._raw_fallback_buttons_down.discard(button_id)
                                physical_buttons = (
                                    self._take_raw_fallback_physical_buttons_locked(
                                        {button_id}
                                    )
                                )
                                self._block_input_until_release(
                                    {button_id} | physical_buttons
                                )
                                self._release_raw_fallback_keyups(
                                    physical_buttons,
                                    reason="raw_fallback_timer_unavailable",
                                )
                            self._logger.warning(
                                "mapping bypassed for non-intercepted input; "
                                "Windows original retained: button=%s source=%s",
                                button_id,
                                event_source,
                            )
                    elif is_pressed:
                        self._logger.warning(
                            "mapping bypassed for non-intercepted input; "
                            "no Windows original available: button=%s source=%s",
                            button_id,
                            event_source,
                        )
        if is_pressed:
            self._record_runtime_button(event_source)
        if button_id == "mic":
            detection_handled, detection_captured = (
                self._handle_key_detection_mic_event(
                    "physical_down" if is_pressed else "physical_up",
                    event_source,
                )
            )
            if detection_handled:
                if detection_captured:
                    self._logger.info(
                        "key detection captured button=mic source=%s; "
                        "voice action suppressed",
                        event_source,
                    )
                return
        if button_id in self._key_detection_suppressed_buttons:
            if not is_pressed:
                self._key_detection_suppressed_buttons.discard(button_id)
                self._key_detection_suppression_deadlines.pop(button_id, None)
                return
            deadline = self._key_detection_suppression_deadlines.get(button_id)
            if deadline is None or now < deadline:
                return
            self._key_detection_suppressed_buttons.discard(button_id)
            self._key_detection_suppression_deadlines.pop(button_id, None)
        detection_captured = False
        if is_pressed and button_id != "mic":
            try:
                detection_captured = key_detection_bridge.publish_next_button(
                    self._config_root,
                    button_id,
                )
            except OSError as exc:
                self._logger.warning("key detection IPC unavailable: %s", exc)
        if detection_captured:
            self._key_detection_suppressed_buttons.add(button_id)
            self._key_detection_suppression_deadlines[button_id] = (
                now + _KEY_DETECTION_SUPPRESSION_MAX_SECONDS
            )
            self._logger.info(
                "key detection captured button=%s; mapped action suppressed",
                button_id,
            )
            return
        if raw_mapping_bypassed:
            return
        if button_id == "mic" and self._ordinary_mic_gesture_active:
            self._handle_ordinary_mic_edge(event_source, is_pressed)
            self._apply_pending_settings_if_idle()
            return
        self._reload_settings_if_changed()

        with self._button_mapping_lock:
            removed_voice_binding = button_id in self._removed_voice_bindings
            ordinary_mic_handled = False
            if removed_voice_binding:
                if not is_pressed:
                    self._button_gestures.release(button_id)
                else:
                    self._logger.warning(
                        "button action suppressed until removed voice mapping is reselected: %s",
                        button_id,
                    )
                primary_action = None
                voice_mode = None
            else:
                primary_action = self._primary_button_action(button_id)
                voice_mode = self._voice_mode_for_primary_button(
                    button_id,
                    primary_action,
                )
                if (getattr(self, "_remote_profile", "") == remote_selection.CHROMECAST_PROFILE
                        and key_mapping.is_voice_action(primary_action)):
                    return  # Voice belongs to the next stage, including remapped ordinary buttons.
                if button_id == "mic" and voice_mode is None:
                    self._handle_ordinary_mic_edge(event_source, is_pressed)
                    ordinary_mic_handled = True
        if removed_voice_binding:
            self._apply_pending_settings_if_idle()
            return
        if ordinary_mic_handled:
            self._apply_pending_settings_if_idle()
            return

        if button_id == "mic":
            if not is_pressed:
                with self._voice_shortcut.lock:
                    source_was_down = (
                        self._voice_mic_gesture_active
                        and event_source in self._voice_mic_gesture_sources_down
                    )
                    if not source_was_down:
                        self._logger.info(
                            "voice physical release ignored without matching down: "
                            "source=%s",
                            event_source,
                        )
                        self._apply_pending_voice_settings_if_idle_locked()
                        return

                    matched_hid_released = event_source in {"hid", "hid_tap"}
                    if matched_hid_released:
                        self._voice_mic_gesture_hid_released = True
                    self._voice_mic_gesture_sources_down.discard(event_source)
                    if (
                        self._voice_shortcut.controller.trigger_mode
                        == key_mapping.VoiceTriggerMode.HOLD
                        and (
                            matched_hid_released
                            or not self._voice_mic_gesture_sources_down
                        )
                    ):
                        if (
                            event_source == "hid_tap"
                            and self._voice_mic_gesture_sources_down
                        ):
                            self._logger.info(
                                "voice hold release accepted from direct HID while "
                                "late duplicate sources remain: %s",
                                sorted(self._voice_mic_gesture_sources_down),
                            )
                        self._release_hold_voice_on_physical_release_locked(
                            "physical mic release",
                        )
                    if (
                        self._voice_mic_gesture_active
                        and not self._voice_mic_gesture_sources_down
                        and (
                            self._voice_mic_gesture_audio_stopped
                            or (
                                not self._voice_mic_gesture_audio_started
                            )
                        )
                    ):
                        self._finish_voice_mic_gesture()
                    self._apply_pending_voice_settings_if_idle_locked()
                return
            # The older remote opened the output endpoint and sent the host
            # shortcut off this raw-key path, before the device reported audio.
            # Chromecast drives voice from its own host/PCM handshake, so a raw
            # mic key press is not a voice trigger here.
            self._logger.info(
                "voice physical trigger ignored: source=%s has no receiver",
                event_source,
            )
            return

        with self._button_mapping_lock:
            modifier = key_mapping.button_combo_modifier(self._bindings)
            configured_combo_buttons = frozenset(
                candidate
                for candidate in key_mapping.COMBO_ACTION_BUTTON_IDS
                if key_mapping.button_combo_action_for(
                    self._bindings, candidate
                ).kind
                != key_mapping.ActionKind.DISABLED
            )
            commands = (
                self._button_combos.press(
                    button_id,
                    modifier=modifier,
                    configured_buttons=configured_combo_buttons,
                )
                if is_pressed
                else self._button_combos.release(button_id)
            )
            self._dispatch_button_combo_commands(commands)
        self._apply_pending_settings_if_idle()

    def _block_input_until_release(self, buttons: set[str]) -> None:
        if not buttons:
            return
        self._input_rearm_blocked_buttons.update(buttons)

    def _dispatch_button_combo_commands(
        self, commands: List[button_combo.ComboCommand]
    ) -> None:
        for command in commands:
            if command.kind == button_combo.ComboCommandKind.FORWARD_PRESS:
                self._button_gestures.press(command.button_id)
            elif command.kind == button_combo.ComboCommandKind.FORWARD_RELEASE:
                self._button_gestures.release(command.button_id)
            elif command.kind == button_combo.ComboCommandKind.TRIGGER:
                self._on_button_combo_trigger(command.button_id)

    def _on_button_combo_trigger(self, button_id: str) -> None:
        action = key_mapping.button_combo_action_for(self._bindings, button_id)
        if action.kind == key_mapping.ActionKind.DISABLED:
            return
        self._logger.info(
            "button combination triggered: modifier=%s button=%s action=%s",
            key_mapping.button_combo_modifier(self._bindings),
            button_id,
            action.kind.value,
        )
        self._apply_button_action(action)

    def _is_mouse_hold_button(self, button_id: str) -> bool:
        return key_mapping.button_action_for(
            self._bindings, button_id, key_mapping.ButtonTrigger.LONG_PRESS
        ).kind in key_mapping.MOUSE_HOLD_BUTTONS

    def _begin_button_mouse_hold(self, owner: object, action: key_mapping.ButtonAction) -> None:
        button = key_mapping.MOUSE_HOLD_BUTTONS[action.kind]
        with self._button_action_lock:
            if not self._accept_input_events or not self._release_pending_button_inputs():
                return
            owners = self._button_mouse_holds.get(button)
            if owners:
                owners.add(owner)
                return
            try:
                win32_input.send_mouse_button_down(button)
            except win32_input.InputCleanupIncompleteError:
                self._button_hold_release_pending.add(button)
                self._schedule_button_input_release_retry_locked()
                self._logger.exception("mouse hold down has pending cleanup: %s", button)
            except (win32_input.MouseButtonInUseError, win32_input.Win32InputUnavailableError, OSError):
                self._logger.info("mouse hold down skipped: %s", button, exc_info=True)
            else:
                self._button_mouse_holds[button] = {owner}

    def _end_button_mouse_hold(self, owner: object) -> None:
        with self._button_action_lock:
            for button, owners in list(self._button_mouse_holds.items()):
                owners.discard(owner)
                if not owners:
                    del self._button_mouse_holds[button]
                    self._button_hold_release_pending.add(button)
            self._release_pending_button_inputs()

    def _release_pending_mouse_holds_locked(self) -> bool:
        for button in tuple(self._button_hold_release_pending):
            try:
                win32_input.send_mouse_button_up(button)
            except (win32_input.InputCleanupIncompleteError, win32_input.Win32InputUnavailableError):
                self._logger.warning("mouse hold release remains pending: %s", button)
                continue
            except OSError:
                # Ordinary OSError means the sender confirmed fallback release.
                self._logger.info("mouse hold release completed through fallback: %s", button)
            self._button_hold_release_pending.remove(button)
        if self._button_hold_release_pending:
            self._schedule_button_input_release_retry_locked()
            return False
        self._finish_button_input_release_if_complete_locked()
        return True


    def _is_button_action_configured(
        self, button_id: str, trigger: button_gesture.ButtonTrigger
    ) -> bool:
        if button_id in self._removed_voice_bindings:
            return False
        action = key_mapping.button_action_for(self._bindings, button_id, trigger)
        if action.kind == key_mapping.ActionKind.DISABLED:
            return False
        if key_mapping.is_voice_action(action):
            return (
                trigger.value == key_mapping.ButtonTrigger.SINGLE_CLICK.value
                and self._voice_mode_for_primary_button(button_id, action) is not None
            )
        if action.kind == key_mapping.ActionKind.KEY_COMBO:
            try:
                win32_keys.resolve_vk_codes(action.keys)
            except win32_keys.UnknownKeyTokenError:
                return False
        if action_executor.is_application_action(action):
            # The action is intentional even if the app is currently not
            # installed.  Do not scan Start Menu/WindowsApps from the Raw
            # Input callback; dispatch will report the missing executable and
            # the configured mapping still correctly owns this physical key.
            return True
        return True

    def _is_button_repeatable(self, button_id: str) -> bool:
        action = key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger.SINGLE_CLICK,
        )
        if button_id not in {
            "up",
            "down",
            "left",
            "right",
            "back",
            "volume_up",
            "volume_down",
        }:
            return False
        return key_mapping.action_allows_repeat(action)

    def _button_repeat_interval(self, button_id: str, repeat_count: int) -> float:
        action = key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger.SINGLE_CLICK,
        )
        if button_id in {"up", "down", "left", "right"} and action.kind in {
            key_mapping.ActionKind.ARROW_UP,
            key_mapping.ActionKind.ARROW_DOWN,
            key_mapping.ActionKind.ARROW_LEFT,
            key_mapping.ActionKind.ARROW_RIGHT,
        }:
            return _DIRECTION_BUTTON_REPEAT_INTERVAL_SECONDS
        if button_id == "back":
            return button_gesture.ButtonGestureDispatcher.BACK_REPEAT_INTERVAL_SECONDS
        return button_gesture.ButtonGestureDispatcher.REPEAT_INTERVAL_SECONDS

    def _on_button_gesture_diagnostic(
        self, event: str, button_id: str, **fields: object
    ) -> None:
        self._diagnostic_trace.emit(
            event,
            gesture_id=self._diagnostic_trace.current_gesture(button_id) or "",
            button_id=str(button_id),
            **fields,
        )

    def _on_button_trigger(
        self, button_id: str, trigger: button_gesture.ButtonTrigger,
        *, hold_owner: Optional[object] = None,
    ) -> None:
        # ButtonGestureDispatcher keeps a callback reservation until this
        # function returns, so settings reload cannot replace the mapping we
        # are reading. Avoid taking _button_mapping_lock here: immediate input
        # enters the dispatcher while already holding it, whereas timer
        # callbacks are serialized by the dispatcher's callback gate.
        if button_id in self._removed_voice_bindings:
            self._logger.warning(
                "delayed button gesture suppressed until removed voice mapping "
                "is reselected: %s",
                button_id,
            )
            return
        action = key_mapping.button_action_for(
            self._bindings,
            button_id,
            key_mapping.ButtonTrigger(trigger.value),
        )
        self._diagnostic_trace.emit(
            "mapping_trigger",
            gesture_id=self._diagnostic_trace.current_gesture(button_id) or "",
            attempt_id=self._diagnostic_trace.current_context()["attempt_id"],
            button_id=str(button_id),
            trigger=str(trigger.value),
            action=str(action.kind.value),
        )
        try:
            if action.kind == key_mapping.ActionKind.KEY_COMBO:
                win32_keys.resolve_vk_codes(action.keys)
        except (
            KeyError,
            TypeError,
            ValueError,
            win32_keys.UnknownKeyTokenError,
        ):
            # A hand-edited or partially corrupted bindings file must disable
            # only the affected button, never escape the Raw Input callback
            # and tear down ordinary-button processing for the whole device.
            self._logger.warning(
                "invalid button binding ignored: button=%s trigger=%s",
                button_id,
                trigger.value,
            )
            return
        if key_mapping.is_voice_action(action):
            self._logger.warning(
                "secondary voice action ignored: button=%s trigger=%s",
                button_id,
                trigger.value,
            )
            return
        if action.kind in key_mapping.MOUSE_HOLD_BUTTONS:
            if hold_owner is not None:
                self._begin_button_mouse_hold(hold_owner, action)
        else:
            self._apply_button_action(action)

    def _apply_button_action(self, action: key_mapping.ButtonAction) -> None:
        with self._button_action_lock:
            self._apply_button_action_locked(action)

    def _apply_button_action_locked(self, action: key_mapping.ButtonAction) -> None:
        if (
            self._button_key_release_pending is not None
            or self._button_mouse_release_pending is not None
            or self._button_hold_release_pending
        ) and not self._release_pending_button_inputs():
            self._logger.info(
                "button action suppressed: an earlier input release is still pending"
            )
            return

        action_name = str(action.kind.value)
        action_success = False
        action_error = ""
        action_error_type = ""
        self._diagnostic_trace.set_current_action(action_name)
        try:
            if action.kind == key_mapping.ActionKind.DISABLED:
                return
            navigation_vk = _BUTTON_ACTION_NAVIGATION_VKS.get(action.kind)
            if action.kind == key_mapping.ActionKind.KEY_COMBO:
                navigation_vk = _BUTTON_NAVIGATION_SINGLE_KEYS.get(tuple(action.keys))
            if navigation_vk is not None and element_navigation_control_windows.route_mapped_navigation_key(navigation_vk):
                # Accepted commands must not also be injected through SendInput.
                pass
            elif action.kind == key_mapping.ActionKind.KEY_COMBO:
                win32_input.send_key_combo_tap(action.keys)
            elif action.kind == key_mapping.ActionKind.ESCAPE:
                win32_input.send_escape()
            elif action.kind == key_mapping.ActionKind.RETURN:
                win32_input.send_return()
            elif action.kind == key_mapping.ActionKind.ARROW_UP:
                win32_input.send_arrow_up()
            elif action.kind == key_mapping.ActionKind.ARROW_DOWN:
                win32_input.send_arrow_down()
            elif action.kind == key_mapping.ActionKind.ARROW_LEFT:
                win32_input.send_arrow_left()
            elif action.kind == key_mapping.ActionKind.ARROW_RIGHT:
                win32_input.send_arrow_right()
            elif action.kind == key_mapping.ActionKind.DELETE_BACKWARD:
                win32_input.send_delete_backward()
            elif action.kind == key_mapping.ActionKind.SHOW_DESKTOP:
                win32_input.send_show_desktop()
            elif action.kind == key_mapping.ActionKind.CONTEXT_MENU:
                win32_input.send_context_menu()
            elif action.kind == key_mapping.ActionKind.APP_SWITCHER:
                win32_input.send_app_switcher()
            elif action.kind == key_mapping.ActionKind.SYSTEM_VOLUME_UP:
                win32_input.send_volume_up()
            elif action.kind == key_mapping.ActionKind.SYSTEM_VOLUME_DOWN:
                win32_input.send_volume_down()
            elif action.kind == key_mapping.ActionKind.SYSTEM_VOLUME_MUTE:
                win32_input.send_volume_mute()
            elif action.kind == key_mapping.ActionKind.PLAY_PAUSE:
                win32_input.send_play_pause()
            elif action.kind in _BUTTON_ACTION_MOUSE_BUTTONS:
                if _BUTTON_ACTION_MOUSE_BUTTONS[action.kind] in self._button_mouse_holds:
                    return
                win32_input.send_mouse_button_click(
                    _BUTTON_ACTION_MOUSE_BUTTONS[action.kind]
                )
            elif action.kind == key_mapping.ActionKind.ELEMENT_NAVIGATION_TOGGLE:
                try:
                    result = (
                        element_navigation_control_windows.toggle_element_navigation()
                    )
                except Exception as exc:
                    action_error = "element_navigation_failed"
                    action_error_type = type(exc).__name__
                    self._logger.exception("element navigation toggle failed unexpectedly")
                    return
                if (
                    result.kind
                    == element_navigation_control_windows.ToggleResultKind.FAILED
                ):
                    action_error = "element_navigation_failed"
                    self._logger.warning(
                        "element navigation toggle failed: %s",
                        result.error or "unknown_error",
                    )
                self._diagnostic_trace.emit(
                    "element_navigation_toggle",
                    **self._diagnostic_trace.current_context(),
                    result=str(result.kind.value),
                    target_hwnd=int(result.target_hwnd),
                )
            elif action.kind == key_mapping.ActionKind.QUICKER_URI:
                action_executor.open_quicker_uri(action)
            elif action_executor.is_application_action(action):
                if not open_configured_application(action):
                    action_error = "application_unavailable"
                    self._logger.warning(
                        "application action unavailable: action=%s", action.kind.value
                    )
            # Voice actions are edge-driven in _on_button_event and never
            # enter this tap-only ordinary action executor.
            action_success = not action_error
        except win32_input.MouseButtonInUseError:
            action_error = "mouse_button_in_use"
            self._logger.info(
                "mouse button action skipped: the physical button is already held"
            )
        except win32_input.Win32InputUnavailableError:
            action_error = "send_input_unavailable"
            self._logger.info("button action skipped: SendInput unavailable here")
        except win32_input.InputCleanupIncompleteError:
            action_error = "input_cleanup_incomplete"
            tokens = self._button_action_key_tokens(action)
            if tokens is not None:
                self._button_key_release_pending = tokens
            mouse_button = _BUTTON_ACTION_MOUSE_BUTTONS.get(action.kind)
            if mouse_button is not None:
                self._button_mouse_release_pending = mouse_button
            self._schedule_button_input_release_retry_locked()
            self._logger.exception(
                "button action failed and safety input-up remains pending"
            )
        except OSError as exc:
            action_error = "delivery_failed"
            action_error_type = type(exc).__name__
            self._logger.exception(
                "button action failed to fully deliver: error_type=%s", action_error_type
            )
        finally:
            self._diagnostic_trace.emit(
                "action_result",
                **self._diagnostic_trace.current_context(),
                success=action_success,
                error=action_error,
                error_type=action_error_type,
            )
            self._diagnostic_trace.set_current_action(None)

    @staticmethod
    def _button_action_key_tokens(
        action: key_mapping.ButtonAction,
    ) -> Optional[Tuple[str, ...]]:
        if action.kind == key_mapping.ActionKind.KEY_COMBO:
            return tuple(action.keys)
        return _BUTTON_ACTION_KEY_TOKENS.get(action.kind)

    def _cancel_button_input_release_retry_locked(
        self,
        *,
        reset_delay: bool = True,
    ) -> None:
        timer = self._button_input_release_retry_timer
        self._button_input_release_retry_timer = None
        self._button_input_release_retry_token = None
        if reset_delay:
            self._button_input_release_retry_delay = (
                _BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS
            )
        if timer is None:
            return
        cancel = getattr(timer, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except BaseException:
                pass

    def _schedule_button_input_release_retry_locked(self) -> None:
        if (
            self._button_input_release_retry_stopping
            or not self._accept_input_events
            or self._button_input_release_retry_timer is not None
            or (
                self._button_key_release_pending is None
                and self._button_mouse_release_pending is None
                and not self._button_hold_release_pending
            )
        ):
            return
        token = object()
        try:
            timer = self._button_input_release_timer_factory(
                self._button_input_release_retry_delay,
                lambda token=token: self._retry_pending_button_inputs(token),
            )
            if isinstance(timer, threading.Thread):
                timer.daemon = True
            self._button_input_release_retry_token = token
            self._button_input_release_retry_timer = timer
            timer.start()
        except BaseException as exc:
            if self._button_input_release_retry_token is token:
                self._button_input_release_retry_token = None
                self._button_input_release_retry_timer = None
            self._logger.error(
                "button input safety-release timer failed: error_type=%s",
                type(exc).__name__,
            )

    def _retry_pending_button_inputs(self, token: object) -> None:
        with self._button_action_lock:
            if self._button_input_release_retry_token is not token:
                return
            self._button_input_release_retry_token = None
            self._button_input_release_retry_timer = None
            if (
                self._button_input_release_retry_stopping
                or not self._accept_input_events
            ):
                return
            self._button_input_release_retry_delay = min(
                _BUTTON_INPUT_RELEASE_RETRY_MAX_SECONDS,
                max(
                    _BUTTON_INPUT_RELEASE_RETRY_INITIAL_SECONDS,
                    self._button_input_release_retry_delay * 2.0,
                ),
            )
            self._release_pending_button_inputs()

    def _finish_button_input_release_if_complete_locked(self) -> None:
        if (
            self._button_key_release_pending is None
            and self._button_mouse_release_pending is None
            and not self._button_hold_release_pending
        ):
            self._cancel_button_input_release_retry_locked()

    def _release_pending_button_keys(self) -> bool:
        with self._button_action_lock:
            tokens = self._button_key_release_pending
            if tokens is None:
                self._finish_button_input_release_if_complete_locked()
                return True
            try:
                win32_input.send_key_combo_up(tokens)
            except win32_input.InputCleanupIncompleteError:
                self._logger.exception("button key safety release remains incomplete")
                self._schedule_button_input_release_retry_locked()
                return False
            except win32_input.Win32InputUnavailableError:
                self._logger.info("button key safety release unavailable")
                self._schedule_button_input_release_retry_locked()
                return False
            except OSError:
                # send_key_combo_up raises ordinary OSError only after its own
                # per-key fallback confirmed every requested key-up.
                self._logger.exception(
                    "button key safety release needed fallback but completed"
                )
            else:
                self._logger.info("button key safety release completed")
            self._button_key_release_pending = None
            self._finish_button_input_release_if_complete_locked()
            return True

    def _release_pending_button_mouse(self) -> bool:
        with self._button_action_lock:
            button = self._button_mouse_release_pending
            if button is None:
                self._finish_button_input_release_if_complete_locked()
                return True
            try:
                win32_input.send_mouse_button_up(button)
            except win32_input.InputCleanupIncompleteError:
                self._logger.exception("button mouse safety release remains incomplete")
                self._schedule_button_input_release_retry_locked()
                return False
            except win32_input.Win32InputUnavailableError:
                self._logger.info("button mouse safety release unavailable")
                self._schedule_button_input_release_retry_locked()
                return False
            except OSError:
                self._logger.exception(
                    "button mouse safety release needed fallback but completed"
                )
            else:
                self._logger.info("button mouse safety release completed")
            self._button_mouse_release_pending = None
            self._finish_button_input_release_if_complete_locked()
            return True

    def _release_pending_button_inputs(self) -> bool:
        with self._button_action_lock:
            key_complete = self._release_pending_button_keys()
            mouse_complete = self._release_pending_button_mouse()
            hold_complete = self._release_pending_mouse_holds_locked()
            return key_complete and mouse_complete and hold_complete

    # -- ATVV control-channel events (mic button + audio start/stop) ------

    def _record_device_diagnostic_context(self) -> None:
        selected = remote_selection.normalize(self._config.get(remote_selection.KEY))
        self._diagnostic_trace.emit(
            "device_context", app_version=__version__,
            selected_ref=selected["active"][:12],
            registered=[{"device_ref": row["key"][:12], "profile": row["profile"]}
                        for row in selected["devices"]],
        )

    def _ensure_voice_diagnostic_attempt(
        self,
        *,
        provider_shortcut_mode: Optional[str] = None,
        effective_hotkey_tokens: Optional[Tuple[str, ...]] = None,
        effective_backend: Optional[str] = None,
    ) -> None:
        if self._voice_attempt_id is not None:
            return
        self._voice_shortcut.ui_confirmation = "unknown"
        self._voice_text_observation = "unknown"
        if not self._diagnostic_trace.enabled:
            # Preserve the active-attempt sentinel without collecting diagnostics.
            self._voice_attempt_id = ""
            return
        backend = effective_backend or self._configured_voice_hotkey_backend()
        tokens = effective_hotkey_tokens or tuple(
            (*self._voice_shortcut.hotkey.modifiers, self._voice_shortcut.hotkey.key)
        )
        shortcut_mode = str(
            provider_shortcut_mode or self._voice_shortcut.controller.trigger_mode.value
        )
        provider = voice_program_manager.normalize_voice_program_settings(
            self._config.get("voice_program")
        )["provider"]
        physicalizer_generation = -1
        physicalizer_installation_epoch = -1
        physicalizer_observed_generation = -1
        physicalizer_observed_installation_epoch = -1
        physicalizer_binding = "not_required"
        physicalizer_observation = "not_required"
        try:
            uses_right_alt = (
                win32_keys.VK_CODES["ralt"]
                in win32_keys.resolve_vk_codes(tokens)
            )
        except win32_keys.UnknownKeyTokenError:
            uses_right_alt = False
        if (
            uses_right_alt and backend in {
                _VOICE_HOTKEY_BACKEND_MARKED,
                _VOICE_HOTKEY_BACKEND_DOUBAO,
            }
        ):
            with self._voice_key_physicalizer_lifecycle_lock:
                physicalizer = self._voice_key_physicalizer
                if physicalizer is not None:
                    physicalizer_observed_generation = int(
                        getattr(physicalizer, "tracker_generation", -1)
                    )
                    physicalizer_observed_installation_epoch = int(
                        getattr(physicalizer, "installation_epoch", -1)
                    )
                    physicalizer_binding = "observed"
                    physicalizer_observation = (
                        "accepting"
                        if bool(getattr(physicalizer, "accepts_new_down", True))
                        else "degraded"
                    )
                else:
                    physicalizer_binding = "unbound"
                    physicalizer_observation = "missing"
        self._voice_attempt_id = self._diagnostic_trace.begin_attempt(
            self._diagnostic_trace.current_gesture("mic"),
            provider=provider,
        )
        self._diagnostic_trace.emit(
            "voice_attempt_context",
            **self._diagnostic_trace.current_context(),
            backend=str(backend),
            remote_recording_mode=str(
                self._config.get("remote_recording_mode", "hold")
            ),
            provider_shortcut_mode=shortcut_mode,
            hotkey_tokens=list(tokens),
            physicalizer_binding=physicalizer_binding,
            physicalizer_generation=physicalizer_generation,
            physicalizer_installation_epoch=physicalizer_installation_epoch,
            physicalizer_observation=physicalizer_observation,
            physicalizer_observed_generation=physicalizer_observed_generation,
            physicalizer_observed_installation_epoch=(
                physicalizer_observed_installation_epoch
            ),
            **diagnostic_trace.foreground_context(),
        )

    def _finish_voice_diagnostic_attempt(
        self, result: str, **fields: object
    ) -> None:
        payload = {
            "host_ui": self._voice_shortcut.ui_confirmation or "unknown",
            "text_state": self._voice_text_observation or "unknown",
        }
        payload.update(fields)
        self._diagnostic_trace.end_attempt(
            self._voice_attempt_id,
            str(result),
            **payload,
        )
        self._voice_attempt_id = None

    def _wait_for_sogou_voice_process(self) -> bool:
        provider_settings = voice_program_manager.normalize_voice_program_settings(
            self._config.get("voice_program")
        )
        if provider_settings["provider"] != voice_program_manager.VOICE_PROGRAM_SOGOU:
            return True
        ready = voice_program_manager.wait_for_sogou_voice_process(timeout=0.6)
        if ready:
            self._logger.info("voice program: Sogou voice process ready")
        else:
            self._logger.warning("voice program: Sogou voice process is not running")
        return ready

    def _capture_voice_focus_before(self) -> None:
        provider_settings = voice_program_manager.normalize_voice_program_settings(
            self._config.get("voice_program")
        )
        self._voice_focus_provider = str(provider_settings["provider"])
        self._voice_focus_submit_method = (
            "wetype_hotkey"
            if self._configured_voice_hotkey_backend() == _VOICE_HOTKEY_BACKEND_WETYPE
            else "hotkey_hold"
        )
        snapshot = voice_interaction_diagnostics_windows.capture_focus_snapshot()
        self._voice_focus_before = snapshot
        self._diagnostic_trace.emit(
            "voice_focus_snapshot",
            **self._diagnostic_trace.current_context(),
            phase="before",
            provider=self._voice_focus_provider,
            submit_method=self._voice_focus_submit_method,
            supported=bool(snapshot.supported),
            foreground_pid=int(snapshot.foreground_pid),
            foreground_class=snapshot.foreground_class or "unknown",
            focus_handle=int(snapshot.focus_handle),
            focus_class=snapshot.focus_class or "unknown",
            text_length=(
                int(snapshot.text_length)
                if snapshot.text_length is not None
                else -1
            ),
            diagnostic=snapshot.error or "captured",
            voice_ui="unknown",
            foreground_executable=snapshot.foreground_executable,
            process_query_status=snapshot.process_query_status,
            foreground_thread_id=snapshot.foreground_thread_id,
            foreground_handle=snapshot.foreground_handle,
            foreground_stable=snapshot.foreground_stable,
            keyboard_layout=snapshot.keyboard_layout,
        )
        self._logger.info(
            "voice interaction start: provider=%s method=%s foreground_pid=%s "
            "foreground_class=%s focus_class=%s text_length=%s diagnostic=%s program=%s keyboard_layout=%s",
            self._voice_focus_provider,
            self._voice_focus_submit_method,
            snapshot.foreground_pid,
            snapshot.foreground_class or "unknown",
            snapshot.focus_class or "unknown",
            snapshot.text_length if snapshot.text_length is not None else "unavailable",
            snapshot.error or "captured",
            snapshot.foreground_executable or "unknown",
            snapshot.keyboard_layout,
        )

    def _log_voice_submission_observation(self):
        before = self._voice_focus_before
        if before is None:
            return None
        after = voice_interaction_diagnostics_windows.capture_focus_snapshot()
        observation = voice_interaction_diagnostics_windows.compare_submission(
            before,
            after,
        )
        self._voice_text_observation = str(observation.text_state)
        self._diagnostic_trace.emit(
            "voice_submission_observation",
            **self._diagnostic_trace.current_context(),
            provider=self._voice_focus_provider or "unknown",
            submit_method=self._voice_focus_submit_method or "unknown",
            supported=bool(after.supported),
            foreground_pid=int(after.foreground_pid),
            foreground_class=after.foreground_class or "unknown",
            focus_handle=int(after.focus_handle),
            focus_class=after.focus_class or "unknown",
            text_length=(
                int(after.text_length) if after.text_length is not None else -1
            ),
            focus_state=str(observation.focus_state),
            text_state=str(observation.text_state),
            text_delta=(
                int(observation.text_delta)
                if observation.text_delta is not None
                else -1
            ),
            diagnostic=after.error or "captured",
            voice_ui=self._voice_shortcut.ui_confirmation or "unknown",
            foreground_executable=after.foreground_executable,
            process_query_status=after.process_query_status,
            foreground_thread_id=after.foreground_thread_id,
            foreground_handle=after.foreground_handle,
            foreground_stable=after.foreground_stable,
            keyboard_layout=after.keyboard_layout,
            keyboard_layout_change=(
                "changed" if before.keyboard_layout != after.keyboard_layout else "same"
            ) if before.keyboard_layout and after.keyboard_layout and before.foreground_stable and after.foreground_stable else "unknown",
        )
        self._logger.info(
            "voice interaction result: provider=%s method=%s focus=%s "
            "text_length=%s delta=%s; host control completion alone does not prove "
            "text insertion",
            self._voice_focus_provider or "unknown",
            self._voice_focus_submit_method or "unknown",
            observation.focus_state,
            observation.text_state,
            (
                observation.text_delta
                if observation.text_delta is not None
                else "unavailable"
            ),
        )
        self._voice_focus_before = None
        self._voice_focus_provider = ""
        self._voice_focus_submit_method = ""
        return observation


    def _prepare_doubao_voice_physicalizer(
        self,
        tokens: Tuple[str, ...],
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        try:
            vk_codes = tuple(win32_keys.resolve_vk_codes(tokens))
        except win32_keys.UnknownKeyTokenError:
            self._logger.exception(
                "Doubao voice physicalizer rejected an unknown hotkey token"
            )
            return False
        # ActivateProfile can finish before ImeService.exe is created. Only
        # the asynchronous attempt may wait here; synchronous callers
        # retain their non-waiting behavior. Never retry a hook/permission/
        # version failure, or resend a shortcut while waiting for the host.
        deadline = time.monotonic() + _DOUBAO_PROCESS_READY_TIMEOUT_SECONDS
        waiting_logged = False
        while cancel_event is None or not cancel_event.is_set():
            if self._voice_shortcut.doubao_physicalizer.start(vk_codes):
                self._logger.info(
                    "Doubao voice physicalizer ready for configured shortcut"
                )
                return True
            if (
                cancel_event is None
                or self._voice_shortcut.doubao_physicalizer.status != "unavailable"
                or self._voice_shortcut.doubao_physicalizer.error != "ImeService.exe is not running"
            ):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not waiting_logged:
                self._logger.info("Waiting for Doubao process after profile activation")
                waiting_logged = True
            if cancel_event.wait(min(_DOUBAO_HOST_READY_POLL_SECONDS, remaining)):
                return False
            if time.monotonic() >= deadline:
                break
        self._logger.warning(
            "Doubao voice physicalizer unavailable: status=%s error=%s",
            self._voice_shortcut.doubao_physicalizer.status,
            self._voice_shortcut.doubao_physicalizer.error or "unknown",
        )
        return False

    def _start_doubao_voice_physicalizer_eager(self) -> None:
        """Attach Frida to the Doubao IME at bridge start, not on first press.

        ``doubao_physicalizer.start`` performs the ~0.6s ``frida.attach`` into
        ImeService.exe.  It is otherwise only invoked from the hotkey path
        (``apply`` -> ``prepare_doubao``), so the first hold pays that cost.
        Kick the attach off here on a daemon thread so it is ready before the
        operator presses anything.  The key-path call remains as an idempotent
        fallback when this has not finished yet.
        """

        if sys.platform != "win32":
            return
        if self._configured_voice_hotkey_backend() != _VOICE_HOTKEY_BACKEND_DOUBAO:
            return
        hotkey = self._voice_shortcut.hotkey
        tokens = tuple(hotkey.modifiers) + (hotkey.key,)
        try:
            vk_codes = tuple(win32_keys.resolve_vk_codes(tokens))
        except win32_keys.UnknownKeyTokenError:
            self._logger.warning(
                "eager Doubao physicalizer skipped: unresolvable hotkey tokens"
            )
            return
        if not vk_codes:
            return
        worker = threading.Thread(
            target=self._run_doubao_voice_physicalizer_eager,
            args=(vk_codes,),
            name="doubao-physicalizer-eager",
            daemon=True,
        )
        worker.start()

    def _run_doubao_voice_physicalizer_eager(self, vk_codes: Tuple[int, ...]) -> None:
        physicalizer = self._voice_shortcut.doubao_physicalizer
        deadline = time.monotonic() + _DOUBAO_EAGER_START_WINDOW_SECONDS
        while not physicalizer.start(vk_codes):
            # Keep retrying only while ImeService.exe has not started yet.  Any
            # other outcome (frida missing, wrong build, permission denied) is
            # permanent, so stop and leave the hotkey path to retry on demand.
            if (
                physicalizer.status != "unavailable"
                or physicalizer.error != "ImeService.exe is not running"
            ):
                self._logger.warning(
                    "eager Doubao physicalizer stopped: status=%s error=%s",
                    physicalizer.status,
                    physicalizer.error or "unknown",
                )
                return
            if time.monotonic() >= deadline:
                self._logger.info(
                    "eager Doubao physicalizer: ImeService not ready within %.0fs; "
                    "falling back to hotkey-triggered start",
                    _DOUBAO_EAGER_START_WINDOW_SECONDS,
                )
                return
            time.sleep(_DOUBAO_EAGER_POLL_SECONDS)
        self._logger.info("eager Doubao physicalizer ready at bridge start")


async def _run(
    *,
    app_factory=None,
    tray_factory=None,
    settings_launcher=None,
    show_notification_icon: bool = True,
    on_runtime_ready=None,
    on_reconnect_ready=None,
    on_settings_reload_ready=None,
    launch_voice_program_on_start: bool = True,
) -> None:
    tray_factory = tray_factory or bridge_tray_windows.BridgeTray
    settings_launcher = settings_launcher or bridge_launcher.launch_settings
    app = (
        ChromecastApp(launch_voice_program_on_start=launch_voice_program_on_start)
        if app_factory is None
        else app_factory()
    )
    loop = asyncio.get_running_loop()
    tray_exit_requested = threading.Event()
    run_task = asyncio.create_task(app.run())

    def open_settings() -> None:
        result = settings_launcher()
        if result.started:
            app._logger.info("notification area: settings opened; pid=%s", result.pid)
        else:
            app._logger.warning(
                "notification area: settings launch failed: %s",
                result.error or "unknown_error",
            )

    def request_exit() -> None:
        try:
            app._logger.info("bridge cancellation requested: source=app_callback")
        except Exception:
            pass
        tray_exit_requested.set()
        loop.call_soon_threadsafe(run_task.cancel)

    def request_tray_exit() -> None:
        try:
            app._logger.info("bridge exit intent: reason=tray_or_legacy_control")
        except Exception:
            pass
        request_exit()

    if on_runtime_ready is not None:
        on_runtime_ready(request_exit)
    if on_reconnect_ready is not None:
        on_reconnect_ready(app.request_connection_retry_now)
    if on_settings_reload_ready is not None:
        on_settings_reload_ready(app.request_settings_reload_now)

    tray = None
    try:
        if show_notification_icon:
            try:
                tray = tray_factory(
                    on_open_settings=open_settings,
                    on_exit_requested=request_tray_exit,
                    status_handler=lambda message: app._logger.info(
                        "notification area: %s", message
                    ),
                )
                if tray.start():
                    app._logger.info("notification area: bridge control icon started")
                else:
                    app._logger.warning(
                        "notification area unavailable: %s",
                        tray.startup_error or "unknown_error",
                    )
            except Exception:
                app._logger.exception("notification area failed to initialize")

        try:
            await run_task
        except asyncio.CancelledError:
            if not tray_exit_requested.is_set():
                raise
            app._logger.info("notification area: graceful bridge exit requested")
    finally:
        try:
            if tray is not None and not tray.stop():
                app._logger.warning("notification area thread did not stop cleanly")
        except Exception as exc:
            app._logger.warning(
                "notification area stop failed: error_type=%s",
                type(exc).__name__,
            )
        finally:
            try:
                await app.stop()
            finally:
                clear_runtime_status = getattr(app, "clear_runtime_status", None)
                if callable(clear_runtime_status):
                    clear_runtime_status()


def main(
    *,
    show_notification_icon: bool = True,
    on_runtime_ready=None,
    on_reconnect_ready=None,
    on_settings_reload_ready=None,
    launch_voice_program_on_start: bool = True,
) -> None:
    asyncio.run(
        _run(
            show_notification_icon=show_notification_icon,
            on_runtime_ready=on_runtime_ready,
            on_reconnect_ready=on_reconnect_ready,
            on_settings_reload_ready=on_settings_reload_ready,
            launch_voice_program_on_start=launch_voice_program_on_start,
        )
    )


if __name__ == "__main__":
    main()
