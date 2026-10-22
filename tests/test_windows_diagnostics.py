import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Sequence
from unittest import mock

from ovb import (
    audio_output,
    audio_playback,
    raw_input_windows,
    single_instance,
    windows_diagnostics as diag,
)


class OsVersionCheckTests(unittest.TestCase):
    def test_non_windows_probe_reports_unsupported(self):
        result = diag.check_os_version(probe=lambda: None)
        self.assertEqual(result.status, diag.CheckStatus.UNSUPPORTED)
        self.assertEqual(result.group, diag.CheckGroup.ORDINARY_BUTTONS)

    def test_32bit_fails(self):
        info = diag.WindowsVersionInfo(major=10, minor=0, build=19045, is_64bit=False)
        result = diag.check_os_version(probe=lambda: info)
        self.assertEqual(result.status, diag.CheckStatus.FAIL)

    def test_build_below_1809_fails(self):
        info = diag.WindowsVersionInfo(major=10, minor=0, build=17134, is_64bit=True)
        result = diag.check_os_version(probe=lambda: info)
        self.assertEqual(result.status, diag.CheckStatus.FAIL)

    def test_build_at_or_above_1809_64bit_passes(self):
        info = diag.WindowsVersionInfo(major=10, minor=0, build=19045, is_64bit=True)
        result = diag.check_os_version(probe=lambda: info)
        self.assertEqual(result.status, diag.CheckStatus.PASS)
        self.assertEqual(result.detail, "Windows 19045（64 位）")

    def test_exact_minimum_build_passes(self):
        info = diag.WindowsVersionInfo(
            major=10, minor=0, build=diag.MIN_SUPPORTED_BUILD, is_64bit=True
        )
        result = diag.check_os_version(probe=lambda: info)
        self.assertEqual(result.status, diag.CheckStatus.PASS)


class RawInputCheckTests(unittest.TestCase):
    def test_zero_matches_fails(self):
        result = diag.check_raw_input(enumerate_paths=lambda: [])
        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertEqual(result.result_code, "no_device")

    def test_exactly_one_match_passes(self):
        result = diag.check_raw_input(enumerate_paths=lambda: ["\\\\?\\HID#VID_2717&PID_32B8#..."])
        self.assertEqual(result.status, diag.CheckStatus.PASS)
        self.assertEqual(result.detail, "谷歌 Chromecast 遥控器 按键设备已找到")
        self.assertEqual(result.result_code, "ready")

    def test_ambiguous_matches_fails(self):
        result = diag.check_raw_input(enumerate_paths=lambda: ["path1", "path2"])
        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertEqual(result.result_code, "ambiguous")

    def test_never_reports_the_device_path_itself(self):
        secret_path = "\\\\?\\HID#VID_2717&PID_32B8#SUPERSECRETSERIAL"
        for paths in ([secret_path], [secret_path, secret_path + "x"]):
            result = diag.check_raw_input(enumerate_paths=lambda p=paths: p)
            self.assertNotIn(secret_path, result.detail)

    def test_unavailable_off_windows_is_unsupported(self):
        def _raise():
            raise raw_input_windows.RawInputUnavailableError("Raw Input is only available on Windows")

        import sys
        import unittest.mock as mock

        with mock.patch.object(sys, "platform", "darwin"):
            result = diag.check_raw_input(enumerate_paths=_raise)
        self.assertEqual(result.status, diag.CheckStatus.UNSUPPORTED)

    def test_real_failure_on_windows_is_a_fail_not_unsupported(self):
        def _raise():
            raise raw_input_windows.RawInputUnavailableError("GetRawInputDeviceList failed: 1")

        import sys
        import unittest.mock as mock

        with mock.patch.object(sys, "platform", "win32"):
            result = diag.check_raw_input(enumerate_paths=_raise)
        self.assertEqual(result.status, diag.CheckStatus.FAIL)

    def test_real_failure_message_never_echoes_the_raw_exception_text(self):
        # A RawInputUnavailableError raised on
        # a real Windows failure path used to have its str(exc) interpolated
        # directly into the CheckResult detail - a backend/injected
        # exception is capable of carrying a real Raw Input device path.
        # Inject sentinels standing in for exactly that and prove neither
        # ever reaches the user-facing detail text, while the result stays
        # a real, actionable FAIL.
        secret_hid_path = "\\\\?\\HID#VID_2717&PID_32B8#SUPERSECRETSERIAL#{deadbeef-0000}"
        # Deliberately avoids "\Users\" (this project's own boundary-scan
        # personal-path regex, tests/test_boundary_scan_replay.py/
        # build/check-public-boundary.ps1, flags any `<drive>:\Users\...`
        # literal in source, including test fixtures) - a ProgramData-style
        # path still stands in for "a local filesystem path" just as well.
        secret_local_path = r"C:\ProgramData\RemoteMic\Chromecast\SecretDeviceCache.db"

        def _raise():
            raise raw_input_windows.RawInputUnavailableError(
                f"GetRawInputDeviceList failed for {secret_hid_path} "
                f"(config at {secret_local_path})"
            )

        import sys
        import unittest.mock as mock

        with mock.patch.object(sys, "platform", "win32"):
            result = diag.check_raw_input(enumerate_paths=_raise)

        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertNotIn(secret_hid_path, result.detail)
        self.assertNotIn(secret_local_path, result.detail)
        self.assertNotIn("SecretDeviceCache", result.detail)
        self.assertNotIn("SUPERSECRETSERIAL", result.detail)
        self.assertTrue(result.detail.strip())
        self.assertIn("Raw Input", result.detail)




class VbCableEndpointsCheckTests(unittest.TestCase):
    def test_both_present_passes(self):
        result = diag.check_vb_cable_endpoints(
            list_playback=lambda: [audio_output.AudioEndpoint(name="CABLE Input")],
            list_recording=lambda: [audio_output.AudioEndpoint(name="CABLE Output")],
        )
        self.assertEqual(result.status, diag.CheckStatus.PASS)
        self.assertEqual(result.group, diag.CheckGroup.OPTIONAL_DRIVER)

    def test_neither_present_fails_but_is_optional(self):
        result = diag.check_vb_cable_endpoints(
            list_playback=lambda: [audio_output.AudioEndpoint(name="Speakers")],
            list_recording=lambda: [audio_output.AudioEndpoint(name="Microphone")],
        )
        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertIn("不影响按键", result.detail)

    def test_only_playback_present_fails_with_specific_missing_note(self):
        result = diag.check_vb_cable_endpoints(
            list_playback=lambda: [audio_output.AudioEndpoint(name="CABLE Input")],
            list_recording=lambda: [],
        )
        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertIn("CABLE Output", result.detail)

    def test_enumeration_failure_is_unsupported(self):
        def _raise():
            raise audio_output.AudioOutputUnavailableError("no sounddevice")

        result = diag.check_vb_cable_endpoints(list_playback=_raise, list_recording=lambda: [])
        self.assertEqual(result.status, diag.CheckStatus.UNSUPPORTED)


class VbCableLoopbackCheckTests(unittest.TestCase):
    @staticmethod
    def _playback():
        return [
            audio_output.AudioEndpoint("CABLE Input", "Windows WASAPI")
        ]

    @staticmethod
    def _recording():
        return [
            audio_output.AudioEndpoint("CABLE Output", "Windows WASAPI")
        ]

    @staticmethod
    def _probe_result(*, detected=True, overflow=False, underflow=False):
        return audio_playback.CableLoopbackProbeResult(
            detected=detected,
            correlation=0.99 if detected else 0.10,
            baseline_rms=0.0,
            signal_rms=0.1 if detected else 0.0,
            roundtrip_latency_ms=12.5 if detected else None,
            input_overflowed=overflow,
            output_underflowed=underflow,
            sample_rate_hz=48000,
        )

    def test_matching_signal_passes_without_claiming_dictation_works(self):
        result = diag.check_vb_cable_loopback(
            "CABLE Input",
            "Windows WASAPI",
            list_playback=self._playback,
            list_recording=self._recording,
            probe=lambda *_args, **_kwargs: self._probe_result(),
        )

        self.assertEqual(result.status, diag.CheckStatus.PASS)
        self.assertIn("CABLE Input → CABLE Output", result.detail)
        self.assertIn("不代表语音识别", result.detail)

    def test_non_cable_saved_output_fails_without_running_probe(self):
        probe = mock.Mock()
        result = diag.check_vb_cable_loopback(
            "Speakers",
            "Windows WASAPI",
            list_playback=lambda: [
                audio_output.AudioEndpoint("Speakers", "Windows WASAPI")
            ],
            list_recording=self._recording,
            probe=probe,
        )

        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertIn("不是 CABLE Input", result.detail)
        probe.assert_not_called()

    def test_recording_endpoint_must_share_the_selected_host_api(self):
        result = diag.check_vb_cable_loopback(
            "CABLE Input",
            "Windows WASAPI",
            list_playback=self._playback,
            list_recording=lambda: [
                audio_output.AudioEndpoint("CABLE Output", "MME")
            ],
            probe=mock.Mock(),
        )

        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertIn("同一音频接口", result.detail)

    def test_silent_capture_fails(self):
        result = diag.check_vb_cable_loopback(
            "CABLE Input",
            "Windows WASAPI",
            list_playback=self._playback,
            list_recording=self._recording,
            probe=lambda *_args, **_kwargs: self._probe_result(detected=False),
        )

        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertIn("未收到", result.detail)

    def test_overflow_makes_an_otherwise_matching_capture_invalid(self):
        result = diag.check_vb_cable_loopback(
            "CABLE Input",
            "Windows WASAPI",
            list_playback=self._playback,
            list_recording=self._recording,
            probe=lambda *_args, **_kwargs: self._probe_result(overflow=True),
        )

        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertIn("无效", result.detail)

    def test_automatic_diagnostics_never_runs_the_active_loopback(self):
        with mock.patch.object(diag, "check_vb_cable_loopback") as loopback:
            report = diag.run_diagnostics(cancel_event=threading.Event())

        self.assertEqual(len(report.checks), 4)
        loopback.assert_not_called()

    def test_isolated_check_returns_the_validated_child_result(self):
        expected = diag.CheckResult(
            "vb_cable_loopback",
            "VB-CABLE 本地通道",
            diag.CheckGroup.OPTIONAL_DRIVER,
            diag.CheckStatus.PASS,
            "测试信号已到达。",
        )
        with mock.patch.object(
            diag, "_run_vb_cable_loopback_in_tempdir", return_value=expected
        ):
            result = diag.check_vb_cable_loopback_isolated(
                "CABLE Input", "Windows WASAPI"
            )

        self.assertEqual(result, expected)

    def test_isolated_hard_timeout_is_an_honest_fail(self):
        with mock.patch.object(
            diag,
            "_run_vb_cable_loopback_in_tempdir",
            side_effect=diag.VbCableLoopbackCancelledError("timed out"),
        ):
            result = diag.check_vb_cable_loopback_isolated(
                "CABLE Input", "Windows WASAPI"
            )

        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertIn("超时", result.detail)
        self.assertIn("已停止测试进程", result.detail)

    def test_isolated_shutdown_cancellation_is_not_reported_as_a_device_failure(self):
        cancel_event = threading.Event()
        cancel_event.set()
        with mock.patch.object(
            diag,
            "_run_vb_cable_loopback_in_tempdir",
            side_effect=diag.VbCableLoopbackCancelledError("cancelled"),
        ):
            result = diag.check_vb_cable_loopback_isolated(
                "CABLE Input",
                "Windows WASAPI",
                cancel_event=cancel_event,
            )

        self.assertEqual(result.status, diag.CheckStatus.UNSUPPORTED)
        self.assertIn("取消", result.detail)

    def test_isolated_unconfirmed_child_shutdown_is_distinct(self):
        with mock.patch.object(
            diag,
            "_run_vb_cable_loopback_in_tempdir",
            side_effect=diag.VbCableLoopbackSubprocessShutdownUnconfirmedError(
                "unconfirmed"
            ),
        ):
            result = diag.check_vb_cable_loopback_isolated(
                "CABLE Input", "Windows WASAPI"
            )

        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertIn("未能确认", result.detail)

    def test_isolated_bridge_race_fails_before_sending_audio(self):
        with mock.patch.object(
            diag,
            "_run_vb_cable_loopback_in_tempdir",
            side_effect=single_instance.DuplicateInstanceError("busy"),
        ):
            result = diag.check_vb_cable_loopback_isolated(
                "CABLE Input", "Windows WASAPI"
            )

        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertIn("未发送测试信号", result.detail)

    def test_isolated_mutex_failure_never_claims_a_valid_result(self):
        with mock.patch.object(
            diag,
            "_run_vb_cable_loopback_in_tempdir",
            side_effect=single_instance.MutexCleanupError("cleanup failed"),
        ):
            result = diag.check_vb_cable_loopback_isolated(
                "CABLE Input", "Windows WASAPI"
            )

        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertIn("结果无效", result.detail)


class OutputEndpointResolutionCheckTests(unittest.TestCase):
    def test_resolves_to_cable_input_passes_with_positive_note(self):
        result = diag.check_output_endpoint_resolution(
            "CABLE Input", "",
            list_playback=lambda: [audio_output.AudioEndpoint(name="CABLE Input")],
            preflight=lambda _name, _host_api: None,
        )
        self.assertEqual(result.status, diag.CheckStatus.PASS)
        self.assertIn("CABLE Input", result.detail)

    def test_resolves_to_non_cable_endpoint_passes_when_it_can_play(self):
        # A real, present, non-CABLE endpoint is a legitimate selection for
        # this check, which only answers whether audio can leave through the
        # saved endpoint. The preflight is what makes that true, so it has to
        # have run - being non-CABLE alone must not turn the check red. The
        # CABLE-Input question is the loopback check's job.
        probe = mock.Mock()
        result = diag.check_output_endpoint_resolution(
            "Speakers", "",
            list_playback=lambda: [audio_output.AudioEndpoint(name="Speakers")],
            preflight=probe,
        )
        self.assertEqual(result.status, diag.CheckStatus.PASS)
        self.assertIn("Speakers", result.detail)
        self.assertIn("非 CABLE Input", result.detail)
        probe.assert_called_once()

    def test_empty_selection_fails(self):
        result = diag.check_output_endpoint_resolution(
            "", "", list_playback=lambda: [audio_output.AudioEndpoint(name="Speakers")]
        )
        self.assertEqual(result.status, diag.CheckStatus.FAIL)

    def test_missing_endpoint_fails_closed(self):
        result = diag.check_output_endpoint_resolution(
            "Gone Device", "", list_playback=lambda: []
        )
        self.assertEqual(result.status, diag.CheckStatus.FAIL)

    def test_enumeration_failure_is_unsupported(self):
        def _raise():
            raise audio_output.AudioOutputUnavailableError("no sounddevice")

        result = diag.check_output_endpoint_resolution("x", "", list_playback=_raise)
        self.assertEqual(result.status, diag.CheckStatus.UNSUPPORTED)

    def test_present_endpoint_that_cannot_open_fails(self):
        result = diag.check_output_endpoint_resolution(
            "CABLE Input",
            "Windows WASAPI",
            list_playback=lambda: [
                audio_output.AudioEndpoint("CABLE Input", "Windows WASAPI")
            ],
            preflight=lambda _name, _host_api: (_ for _ in ()).throw(
                RuntimeError("private endpoint detail")
            ),
        )
        self.assertEqual(result.status, diag.CheckStatus.FAIL)
        self.assertIn("无法播放", result.detail)
        self.assertNotIn("private endpoint detail", result.detail)


class RunDiagnosticsOrchestrationTests(unittest.TestCase):
    def test_returns_all_four_checks_with_stable_ids(self):
        report = diag.run_diagnostics()
        ids = {check.check_id for check in report.checks}
        # There is no Bluetooth-pairing check: it was permanently red for the
        # Chromecast remote (see run_diagnostics()).
        self.assertEqual(
            ids,
            {
                "os_version",
                "raw_input",
                "vb_cable_endpoints",
                "output_endpoint",
            },
        )

    def test_get_looks_up_by_id(self):
        report = diag.run_diagnostics()
        self.assertIsNotNone(report.get("output_endpoint"))
        self.assertIsNone(report.get("nonexistent"))

    def test_source_environment_checks_degrade_to_unsupported_not_pass(self):
        # This test suite's own host is not Windows (or, even if it somehow
        # were, has no Chromecast/VB-CABLE attached) - the real, uninjected
        # checks must never claim PASS for something they cannot observe.
        report = diag.run_diagnostics()
        allowed_statuses = (diag.CheckStatus.UNSUPPORTED, diag.CheckStatus.FAIL)
        # This test runs on the developer's machine. On Windows a real Chromecast
        # may be connected, in which case PASS is the correct result rather
        # than evidence of a false positive.
        if sys.platform == "win32":
            allowed_statuses += (diag.CheckStatus.PASS,)
        for check_id in ("raw_input",):
            result = report.get(check_id)
            self.assertIn(
                result.status,
                allowed_statuses,
                f"{check_id} unexpectedly reported {result.status} on a non-real-hardware host",
            )

    def test_output_endpoint_check_uses_the_isolated_preflight_and_cancel_event(self):
        cancel_event = threading.Event()
        captured = []

        def _isolated_preflight(name, host_api, *, cancel_event=None, timeout=None):
            captured.append((name, host_api, cancel_event, timeout))

        def _output_check(saved_name, saved_host_api, *, preflight, **_kwargs):
            preflight(saved_name, saved_host_api)
            return diag.CheckResult(
                "output_endpoint",
                "语音输出端点",
                diag.CheckGroup.VOICE_BRIDGE,
                diag.CheckStatus.PASS,
                "isolated",
            )

        simple_result = diag.CheckResult(
            "placeholder",
            "placeholder",
            diag.CheckGroup.ORDINARY_BUTTONS,
            diag.CheckStatus.PASS,
            "ok",
        )
        with mock.patch.object(
            diag,
            "preflight_output_endpoint_isolated",
            side_effect=_isolated_preflight,
        ), mock.patch.object(
            diag, "check_output_endpoint_resolution", side_effect=_output_check
        ), mock.patch.object(
            diag, "check_os_version", return_value=simple_result
        ), mock.patch.object(
            diag, "check_raw_input", return_value=simple_result
        ), mock.patch.object(
            diag, "check_vb_cable_endpoints", return_value=simple_result
        ):
            diag.run_diagnostics(
                saved_output_name="CABLE Input",
                saved_output_host_api="Windows WASAPI",
                cancel_event=cancel_event,
            )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][:2], ("CABLE Input", "Windows WASAPI"))
        self.assertIs(captured[0][2], cancel_event)
        self.assertIsNone(captured[0][3])


class RunDiagnosticsIsolationTests(unittest.TestCase):
    """An unexpected exception from any ONE check
    function must never abort run_diagnostics() or leave the other four
    checks missing - it becomes only that check's own honest FAIL result.
    """

    def test_unexpected_exception_in_one_check_isolates_to_that_check_only(self):
        import unittest.mock as mock

        from ovb import windows_diagnostics as module

        with mock.patch.object(
            module, "check_raw_input", side_effect=RuntimeError("boom")
        ):
            report = module.run_diagnostics()

        self.assertEqual(len(report.checks), 4)
        failed = report.get("raw_input")
        self.assertEqual(failed.status, diag.CheckStatus.FAIL)
        self.assertEqual(failed.group, diag.CheckGroup.ORDINARY_BUTTONS)
        self.assertNotIn("boom", failed.detail)
        # The other four checks still render their own real result -
        # nothing else was aborted or left missing.
        other_ids = {"os_version", "vb_cable_endpoints", "output_endpoint"}
        self.assertEqual({c.check_id for c in report.checks} - {"raw_input"}, other_ids)
        for check_id in other_ids:
            result = report.get(check_id)
            self.assertIsNotNone(result)
            self.assertIsInstance(result.status, diag.CheckStatus)

    def test_unexpected_exception_never_leaks_into_the_detail_text(self):
        import unittest.mock as mock

        from ovb import windows_diagnostics as module

        secret = "\\\\?\\HID#VID_2717&PID_32B8#SUPERSECRETSERIAL"

        with mock.patch.object(
            module, "check_raw_input", side_effect=RuntimeError(secret)
        ):
            report = module.run_diagnostics()

        self.assertNotIn(secret, report.get("raw_input").detail)
        self.assertEqual(report.get("raw_input").status, diag.CheckStatus.FAIL)

    def test_multiple_simultaneous_unexpected_failures_each_isolate_independently(self):
        import unittest.mock as mock

        from ovb import windows_diagnostics as module

        with mock.patch.object(module, "check_os_version", side_effect=RuntimeError("a")):
            with mock.patch.object(module, "check_vb_cable_endpoints", side_effect=RuntimeError("b")):
                report = module.run_diagnostics()

        self.assertEqual(len(report.checks), 4)
        self.assertEqual(report.get("os_version").status, diag.CheckStatus.FAIL)
        self.assertEqual(report.get("vb_cable_endpoints").status, diag.CheckStatus.FAIL)
        # Untouched checks are unaffected.
        self.assertIsNotNone(report.get("raw_input"))




class BuildVbCableLoopbackSubprocessCommandTests(unittest.TestCase):
    def test_source_mode_passes_both_private_file_paths(self):
        command = diag.build_vb_cable_loopback_subprocess_command(
            "/tmp/request.json",
            "/tmp/result.json",
            frozen=False,
            executable="/usr/bin/python3",
        )
        self.assertEqual(
            command,
            [
                "/usr/bin/python3",
                "-m",
                "ovb",
                diag.VB_CABLE_LOOPBACK_SUBPROCESS_FLAG,
                "/tmp/request.json",
                "/tmp/result.json",
            ],
        )

    def test_frozen_mode_reinvokes_the_exe_directly(self):
        command = diag.build_vb_cable_loopback_subprocess_command(
            "request.json",
            "result.json",
            frozen=True,
            executable="无线麦Mod win版 1.0.51-candidate.51.exe",
        )
        self.assertEqual(
            command,
            [
                "无线麦Mod win版 1.0.51-candidate.51.exe",
                diag.VB_CABLE_LOOPBACK_SUBPROCESS_FLAG,
                "request.json",
                "result.json",
            ],
        )


class BuildOutputEndpointPreflightSubprocessCommandTests(unittest.TestCase):
    def test_source_mode_passes_both_private_file_paths(self):
        command = diag.build_output_endpoint_preflight_subprocess_command(
            "/tmp/request.json",
            "/tmp/result.json",
            frozen=False,
            executable="/usr/bin/python3",
        )
        self.assertEqual(
            command,
            [
                "/usr/bin/python3",
                "-m",
                "ovb",
                diag.OUTPUT_ENDPOINT_PREFLIGHT_SUBPROCESS_FLAG,
                "/tmp/request.json",
                "/tmp/result.json",
            ],
        )

    def test_frozen_mode_reinvokes_the_exe_directly(self):
        command = diag.build_output_endpoint_preflight_subprocess_command(
            "request.json",
            "result.json",
            frozen=True,
            executable="无线麦Mod win版 1.0.51-candidate.51.exe",
        )
        self.assertEqual(
            command,
            [
                "无线麦Mod win版 1.0.51-candidate.51.exe",
                diag.OUTPUT_ENDPOINT_PREFLIGHT_SUBPROCESS_FLAG,
                "request.json",
                "result.json",
            ],
        )


class SanitizeVerdictPayloadTests(unittest.TestCase):
    """The strict-allow-list parser is the ONLY place
    raw, untrusted result-file content is allowed to influence this
    process - every test here proves something OUTSIDE the exact expected
    shape becomes ``ERROR``, never a fabricated success or an unbounded
    allocation.
    """

    def test_accepts_every_real_non_ambiguous_verdict(self):
        for verdict in ("single_match", "no_candidate", "winrt_unavailable", "error"):
            with self.subTest(verdict=verdict):
                result = diag._sanitize_verdict_payload({"verdict": verdict})
                self.assertEqual(result.verdict.value, verdict)
                self.assertIsNone(result.count)

    def test_accepts_a_well_formed_ambiguous_verdict(self):
        result = diag._sanitize_verdict_payload({"verdict": "ambiguous", "count": 3})
        self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.AMBIGUOUS)
        self.assertEqual(result.count, 3)

    def test_rejects_non_dict_content(self):
        for bad in (None, "single_match", 42, ["single_match"]):
            with self.subTest(bad=bad):
                result = diag._sanitize_verdict_payload(bad)
                self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.ERROR)

    def test_rejects_an_unknown_verdict_string(self):
        result = diag._sanitize_verdict_payload({"verdict": "totally_made_up"})
        self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.ERROR)

    def test_rejects_extra_keys_on_a_simple_verdict(self):
        result = diag._sanitize_verdict_payload(
            {"verdict": "single_match", "extra": "should not be here"}
        )
        self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.ERROR)

    def test_rejects_extra_keys_on_an_ambiguous_verdict(self):
        result = diag._sanitize_verdict_payload(
            {"verdict": "ambiguous", "count": 3, "device_name": "should never appear"}
        )
        self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.ERROR)

    def test_rejects_ambiguous_missing_count(self):
        result = diag._sanitize_verdict_payload({"verdict": "ambiguous"})
        self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.ERROR)

    def test_rejects_non_integer_count(self):
        for bad_count in ("3", 3.0, None, [3]):
            with self.subTest(bad_count=bad_count):
                result = diag._sanitize_verdict_payload({"verdict": "ambiguous", "count": bad_count})
                self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.ERROR)

    def test_rejects_a_bool_count_despite_bool_being_an_int_subclass(self):
        result = diag._sanitize_verdict_payload({"verdict": "ambiguous", "count": True})
        self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.ERROR)

    def test_rejects_count_below_two(self):
        for bad_count in (-1, 0, 1):
            with self.subTest(bad_count=bad_count):
                result = diag._sanitize_verdict_payload({"verdict": "ambiguous", "count": bad_count})
                self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.ERROR)

    def test_rejects_an_implausibly_large_count(self):
        result = diag._sanitize_verdict_payload(
            {"verdict": "ambiguous", "count": diag._MAX_PLAUSIBLE_AMBIGUOUS_COUNT + 1}
        )
        self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.ERROR)

    def test_accepts_the_maximum_plausible_count(self):
        result = diag._sanitize_verdict_payload(
            {"verdict": "ambiguous", "count": diag._MAX_PLAUSIBLE_AMBIGUOUS_COUNT}
        )
        self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.AMBIGUOUS)
        self.assertEqual(result.count, diag._MAX_PLAUSIBLE_AMBIGUOUS_COUNT)


class ReadSubprocessVerdictTests(unittest.TestCase):
    """Parent-side file read+parse, exercised against a REAL temp file (not
    a mock) - nonzero exit codes and missing/malformed files must never be
    trusted, even if a stray file happens to exist.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write(self, content: str) -> str:
        path = os.path.join(self._tmpdir, "verdict.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def test_nonzero_returncode_is_error_even_if_the_file_has_valid_content(self):
        path = self._write(json.dumps({"verdict": "single_match"}))
        result = diag._read_subprocess_verdict(path, returncode=1)
        self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.ERROR)

    def test_missing_file_is_error(self):
        missing_path = os.path.join(self._tmpdir, "does-not-exist.json")
        result = diag._read_subprocess_verdict(missing_path, returncode=0)
        self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.ERROR)

    def test_malformed_json_is_error(self):
        path = self._write("{not valid json")
        result = diag._read_subprocess_verdict(path, returncode=0)
        self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.ERROR)

    def test_valid_content_with_zero_returncode_is_accepted(self):
        path = self._write(json.dumps({"verdict": "no_candidate"}))
        result = diag._read_subprocess_verdict(path, returncode=0)
        self.assertEqual(result.verdict, diag.BleDiagnosticsVerdict.NO_CANDIDATE)




class VbCableLoopbackSubprocessEntrypointTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._request_path = os.path.join(self._tmpdir, "request.json")
        self._result_path = os.path.join(self._tmpdir, "result.json")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_request(self, payload=None):
        value = payload or {
            "saved_output_name": "CABLE Input",
            "saved_output_host_api": "Windows WASAPI",
        }
        with open(self._request_path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)

    def test_invalid_request_fails_closed_before_touching_audio(self):
        self._write_request({"unexpected": "value"})
        with mock.patch.object(diag, "check_vb_cable_loopback") as direct_check:
            exit_code = diag.run_vb_cable_loopback_subprocess_entrypoint(
                self._request_path, self._result_path
            )

        self.assertEqual(exit_code, 1)
        self.assertFalse(os.path.exists(self._result_path))
        direct_check.assert_not_called()

    def test_valid_request_writes_only_the_sanitized_check_result(self):
        self._write_request()
        child_result = diag.CheckResult(
            "vb_cable_loopback",
            "VB-CABLE 本地通道",
            diag.CheckGroup.OPTIONAL_DRIVER,
            diag.CheckStatus.PASS,
            "测试信号已到达，不代表输入法已经识别文字。",
        )
        with mock.patch.object(
            diag, "check_vb_cable_loopback", return_value=child_result
        ) as direct_check:
            exit_code = diag.run_vb_cable_loopback_subprocess_entrypoint(
                self._request_path, self._result_path
            )

        self.assertEqual(exit_code, 0)
        direct_check.assert_called_once_with("CABLE Input", "Windows WASAPI")
        with open(self._result_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(
            payload,
            {
                "status": "pass",
                "detail": "测试信号已到达，不代表输入法已经识别文字。",
            },
        )


class OutputEndpointPreflightSubprocessEntrypointTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._request_path = os.path.join(self._tmpdir, "request.json")
        self._result_path = os.path.join(self._tmpdir, "result.json")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_request(self, payload=None):
        value = payload or {
            "saved_output_name": "CABLE Input",
            "saved_output_host_api": "Windows WASAPI",
        }
        with open(self._request_path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)

    def _read_result(self):
        with open(self._result_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_invalid_request_fails_closed_before_touching_audio(self):
        self._write_request({"unexpected": "value"})
        with mock.patch.object(
            audio_playback, "preflight_output_endpoint"
        ) as direct_check:
            exit_code = diag.run_output_endpoint_preflight_subprocess_entrypoint(
                self._request_path, self._result_path
            )

        self.assertEqual(exit_code, 1)
        self.assertFalse(os.path.exists(self._result_path))
        direct_check.assert_not_called()

    def test_success_writes_only_the_ok_verdict(self):
        self._write_request()
        with mock.patch.object(
            audio_playback, "preflight_output_endpoint"
        ) as direct_check:
            exit_code = diag.run_output_endpoint_preflight_subprocess_entrypoint(
                self._request_path, self._result_path
            )

        self.assertEqual(exit_code, 0)
        direct_check.assert_called_once_with("CABLE Input", "Windows WASAPI")
        self.assertEqual(self._read_result(), {"verdict": "ok"})

    def test_failure_writes_only_the_unavailable_verdict(self):
        self._write_request()
        with mock.patch.object(
            audio_playback,
            "preflight_output_endpoint",
            side_effect=RuntimeError("private endpoint detail"),
        ):
            exit_code = diag.run_output_endpoint_preflight_subprocess_entrypoint(
                self._request_path, self._result_path
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(self._read_result(), {"verdict": "unavailable"})
        with open(self._result_path, "r", encoding="utf-8") as handle:
            raw_text = handle.read()
        self.assertNotIn("private endpoint detail", raw_text)

    def test_result_write_failure_returns_nonzero(self):
        self._write_request()
        with mock.patch.object(
            audio_playback,
            "preflight_output_endpoint",
        ), mock.patch.object(
            diag,
            "_write_verdict_atomically",
            side_effect=OSError("disk unavailable"),
        ):
            exit_code = diag.run_output_endpoint_preflight_subprocess_entrypoint(
                self._request_path,
                self._result_path,
            )

        self.assertEqual(exit_code, 1)

    def test_missing_required_path_fails_before_touching_audio(self):
        self._write_request()
        with mock.patch.object(
            audio_playback,
            "preflight_output_endpoint",
        ) as direct_check:
            self.assertEqual(
                diag.run_output_endpoint_preflight_subprocess_entrypoint(
                    self._request_path,
                    None,
                ),
                1,
            )
            self.assertEqual(
                diag.run_output_endpoint_preflight_subprocess_entrypoint(
                    None,
                    self._result_path,
                ),
                1,
            )

        direct_check.assert_not_called()


class VbCableLoopbackResultValidationTests(unittest.TestCase):
    def test_accepts_a_strict_well_formed_result(self):
        result = diag._sanitize_vb_cable_loopback_result(
            {"status": "pass", "detail": "测试通过。"}
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, diag.CheckStatus.PASS)

    def test_rejects_extra_keys_or_untrusted_status(self):
        self.assertIsNone(
            diag._sanitize_vb_cable_loopback_result(
                {"status": "pass", "detail": "测试通过。", "device": "secret"}
            )
        )
        self.assertIsNone(
            diag._sanitize_vb_cable_loopback_result(
                {"status": "manual", "detail": "not allowed"}
            )
        )


class _FakeProc:
    """A minimal ``Popen``-like double for exercising
    ``_attempt_termination_step()``'s own process-control exception
    handling in isolation from any real OS process/timing - ``terminate()``/
    ``kill()``/``wait()`` each raising ``PermissionError``, and ``poll()``
    also raising after
    ``wait()`` already did) - real subprocesses cannot be made to raise
    these deterministically, so a real ``Popen`` is the wrong tool for
    these specific tests (see ``RunBleDiagnosticsSubprocessTests`` below
    for the REAL-process coverage of the happy/race/escalation paths this
    class does not attempt to duplicate).
    """

    def __init__(
        self,
        *,
        terminate_raises=None,
        kill_raises=None,
        wait_raises=None,
        poll_raises=None,
        poll_returns=None,
    ):
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.poll_calls = 0
        self._terminate_raises = terminate_raises
        self._kill_raises = kill_raises
        self._wait_raises = wait_raises
        self._poll_raises = poll_raises
        self._poll_returns = poll_returns

    def terminate(self):
        self.terminate_calls += 1
        if self._terminate_raises is not None:
            raise self._terminate_raises

    def kill(self):
        self.kill_calls += 1
        if self._kill_raises is not None:
            raise self._kill_raises

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self._wait_raises is not None:
            raise self._wait_raises

    def poll(self):
        self.poll_calls += 1
        if self._poll_raises is not None:
            raise self._poll_raises
        return self._poll_returns


class AttemptTerminationStepTests(unittest.TestCase):
    """Deterministic, process-free coverage of
    ``_attempt_termination_step()``'s own exception handling - every
    process-control call (``terminate()``/``kill()``/``wait()``/
    ``poll()``) can independently raise an ``OSError``
    (``PermissionError`` was the exact case Codex's own probe reproduced),
    and none of them may ever escape this function as a raw,
    undifferentiated ``OSError`` - only a real ``wait()``/``poll()``
    observation of the process having actually exited may ever return
    True; anything this function cannot confirm returns False.
    """

    def test_terminate_raising_still_confirms_via_a_successful_wait(self):
        # A race: terminate() failed (e.g. PermissionError), but the
        # process happens to have already exited on its own by the time
        # wait() is called - wait() succeeding is a REAL confirmation and
        # must still return True, not be treated as a terminate() failure.
        proc = _FakeProc(terminate_raises=PermissionError("denied"))
        result = diag._attempt_termination_step(proc, proc.terminate, 1.0)
        self.assertTrue(result)
        self.assertEqual(proc.wait_calls, 1)

    def test_wait_raising_falls_back_to_a_successful_poll_confirmation(self):
        # wait() itself fails, but poll() independently observes the
        # process has a real exit status - still a genuine confirmation.
        proc = _FakeProc(wait_raises=PermissionError("denied"), poll_returns=0)
        result = diag._attempt_termination_step(proc, proc.terminate, 1.0)
        self.assertTrue(result)
        self.assertEqual(proc.poll_calls, 1)

    def test_wait_raising_and_poll_showing_still_running_returns_false(self):
        proc = _FakeProc(wait_raises=PermissionError("denied"), poll_returns=None)
        result = diag._attempt_termination_step(proc, proc.terminate, 1.0)
        self.assertFalse(result)

    def test_wait_raising_and_poll_also_raising_returns_false_not_raise(self):
        # poll() is not trusted to
        # never raise either - a second raw OSError from the poll()
        # fallback must never escape this function; it is just another
        # "cannot confirm" outcome.
        proc = _FakeProc(
            wait_raises=PermissionError("wait denied"),
            poll_raises=OSError("poll denied too"),
        )
        result = diag._attempt_termination_step(proc, proc.terminate, 1.0)
        self.assertFalse(result)

    def test_kill_raising_still_confirms_via_a_successful_wait(self):
        proc = _FakeProc(kill_raises=PermissionError("denied"))
        result = diag._attempt_termination_step(proc, proc.kill, 1.0)
        self.assertTrue(result)

    def test_timeout_expired_returns_false_without_touching_poll(self):
        proc = _FakeProc(wait_raises=subprocess.TimeoutExpired(cmd="x", timeout=1.0))
        result = diag._attempt_termination_step(proc, proc.terminate, 1.0)
        self.assertFalse(result)
        self.assertEqual(proc.poll_calls, 0)

    def test_every_failure_path_stays_bounded_no_retry_loop(self):
        # None of the OSError fallback branches may loop/sleep - each is a
        # single wait() plus at most one poll() call, never retried.
        proc = _FakeProc(
            wait_raises=PermissionError("denied"), poll_raises=OSError("denied too")
        )
        started = time.monotonic()
        diag._attempt_termination_step(proc, proc.terminate, 1.0)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertEqual(proc.wait_calls, 1)
        self.assertEqual(proc.poll_calls, 1)

    def test_full_escalation_confirms_via_wait_after_terminate_and_kill_both_raise(self):
        # Full _terminate_and_confirm_exit() escalation: terminate() raises,
        # its wait() times out (still running), kill() ALSO raises, but the
        # kill-step wait() succeeds - a real race-confirmed death after the
        # forceful step, even though both signal calls themselves failed.
        proc = _FakeProc(
            terminate_raises=PermissionError("denied"),
            kill_raises=PermissionError("denied too"),
        )
        # First wait() (after terminate) times out; second wait() (after
        # kill) succeeds - simulate via a stateful override.
        wait_calls = {"n": 0}

        def _wait(timeout=None):
            wait_calls["n"] += 1
            if wait_calls["n"] == 1:
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
            return None

        proc.wait = _wait
        result = diag._terminate_and_confirm_exit(proc, terminate_wait=0.1, kill_wait=0.1)
        self.assertTrue(result)
        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1)


class RunVbCableLoopbackSubprocessTests(unittest.TestCase):
    def test_tempdir_runner_holds_bridge_exclusion_while_child_runs(self):
        events = []

        class Guard:
            def __enter__(self):
                events.append("enter")
                return self

            def __exit__(self, exc_type, exc, tb):
                events.append("exit")

        expected = diag.CheckResult(
            "vb_cable_loopback",
            "VB-CABLE 本地通道",
            diag.CheckGroup.OPTIONAL_DRIVER,
            diag.CheckStatus.PASS,
            "isolated child completed",
        )

        def _run_child(_command, **_kwargs):
            events.append("child")
            return expected

        with mock.patch.object(
            diag,
            "build_vb_cable_loopback_subprocess_command",
            return_value=["child"],
        ), mock.patch.object(
            diag,
            "_run_vb_cable_loopback_subprocess",
            side_effect=_run_child,
        ):
            result = diag._run_vb_cable_loopback_in_tempdir(
                "CABLE Input",
                "Windows WASAPI",
                cancel_event=threading.Event(),
                timeout=1.0,
                bridge_guard_factory=Guard,
            )

        self.assertEqual(result, expected)
        self.assertEqual(events, ["enter", "child", "exit"])

    def test_cancel_event_already_set_never_spawns(self):
        cancel_event = threading.Event()
        cancel_event.set()

        def _unexpected_popen(*_args, **_kwargs):
            raise AssertionError("cancelled test must not spawn")

        with self.assertRaises(diag.VbCableLoopbackCancelledError):
            diag._run_vb_cable_loopback_subprocess(
                ["irrelevant"],
                result_path="irrelevant",
                cancel_event=cancel_event,
                timeout=1.0,
                popen=_unexpected_popen,
            )

    def test_total_timeout_terminates_and_confirms_the_child(self):
        proc = _FakeProc(poll_returns=None)
        with self.assertRaises(diag.VbCableLoopbackCancelledError):
            diag._run_vb_cable_loopback_subprocess(
                ["irrelevant"],
                result_path="irrelevant",
                cancel_event=threading.Event(),
                timeout=0.0,
                popen=lambda *_args, **_kwargs: proc,
            )

        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 0)
        self.assertEqual(proc.wait_calls, 1)

    def test_unconfirmed_termination_is_reported_distinctly(self):
        proc = _FakeProc(
            poll_returns=None,
            wait_raises=subprocess.TimeoutExpired(cmd="x", timeout=0.01),
        )
        with self.assertRaises(
            diag.VbCableLoopbackSubprocessShutdownUnconfirmedError
        ):
            diag._run_vb_cable_loopback_subprocess(
                ["irrelevant"],
                result_path="irrelevant",
                cancel_event=threading.Event(),
                timeout=0.0,
                terminate_wait=0.01,
                kill_wait=0.01,
                popen=lambda *_args, **_kwargs: proc,
            )

        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1)

    def test_real_child_result_is_read_only_after_the_child_exits(self):
        tmpdir = tempfile.mkdtemp()
        try:
            result_path = os.path.join(tmpdir, "result.json")
            payload = json.dumps(
                {"status": "fail", "detail": "isolated child completed"}
            )
            script = (
                "import pathlib, sys\n"
                f"pathlib.Path(sys.argv[1]).write_text({payload!r}, encoding='utf-8')\n"
            )

            result = diag._run_vb_cable_loopback_subprocess(
                [sys.executable, "-c", script, result_path],
                result_path=result_path,
                cancel_event=threading.Event(),
                timeout=5.0,
            )

            self.assertIsNotNone(result)
            self.assertEqual(result.status, diag.CheckStatus.FAIL)
            self.assertEqual(result.detail, "isolated child completed")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_real_native_phase_hang_is_terminated_and_confirmed_dead(self):
        process_holder = {}

        def _spawning_popen(_cmd, **kwargs):
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                **kwargs,
            )
            process_holder["proc"] = proc
            return proc

        started = time.monotonic()
        try:
            with self.assertRaises(diag.VbCableLoopbackCancelledError):
                diag._run_vb_cable_loopback_subprocess(
                    ["replaced by _spawning_popen"],
                    result_path="irrelevant",
                    cancel_event=threading.Event(),
                    timeout=0.2,
                    poll_interval=0.05,
                    terminate_wait=1.0,
                    kill_wait=1.0,
                    popen=_spawning_popen,
                )
        finally:
            proc = process_holder.get("proc")
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)

        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)
        self.assertIsNotNone(process_holder["proc"].poll())


class RunOutputEndpointPreflightSubprocessTests(unittest.TestCase):
    def test_cancel_event_already_set_never_spawns(self):
        cancel_event = threading.Event()
        cancel_event.set()

        def _unexpected_popen(*_args, **_kwargs):
            raise AssertionError("cancelled preflight must not spawn")

        with self.assertRaises(diag.OutputEndpointPreflightCancelledError):
            diag._run_output_endpoint_preflight_subprocess(
                ["irrelevant"],
                result_path="irrelevant",
                cancel_event=cancel_event,
                timeout=1.0,
                popen=_unexpected_popen,
            )

    def test_total_timeout_terminates_and_confirms_the_child(self):
        proc = _FakeProc(poll_returns=None)
        with self.assertRaises(diag.OutputEndpointPreflightCancelledError):
            diag._run_output_endpoint_preflight_subprocess(
                ["irrelevant"],
                result_path="irrelevant",
                cancel_event=threading.Event(),
                timeout=0.0,
                popen=lambda *_args, **_kwargs: proc,
            )

        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 0)
        self.assertEqual(proc.wait_calls, 1)

    def test_unconfirmed_termination_is_reported_distinctly(self):
        proc = _FakeProc(
            poll_returns=None,
            wait_raises=subprocess.TimeoutExpired(cmd="x", timeout=0.01),
        )
        with self.assertRaises(
            diag.OutputEndpointPreflightShutdownUnconfirmedError
        ):
            diag._run_output_endpoint_preflight_subprocess(
                ["irrelevant"],
                result_path="irrelevant",
                cancel_event=threading.Event(),
                timeout=0.0,
                terminate_wait=0.01,
                kill_wait=0.01,
                popen=lambda *_args, **_kwargs: proc,
            )

        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1)

    def test_poll_failure_terminates_the_child_before_reporting_failure(self):
        proc = _FakeProc(poll_raises=OSError("poll failed"))

        with self.assertRaises(RuntimeError):
            diag._run_output_endpoint_preflight_subprocess(
                ["irrelevant"],
                result_path="irrelevant",
                cancel_event=threading.Event(),
                timeout=1.0,
                popen=lambda *_args, **_kwargs: proc,
            )

        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 0)
        self.assertEqual(proc.wait_calls, 1)

    def test_poll_failure_reports_unconfirmed_shutdown_distinctly(self):
        proc = _FakeProc(
            poll_raises=OSError("poll failed"),
            wait_raises=subprocess.TimeoutExpired(cmd="x", timeout=0.01),
        )

        with self.assertRaises(
            diag.OutputEndpointPreflightShutdownUnconfirmedError
        ):
            diag._run_output_endpoint_preflight_subprocess(
                ["irrelevant"],
                result_path="irrelevant",
                cancel_event=threading.Event(),
                timeout=1.0,
                terminate_wait=0.01,
                kill_wait=0.01,
                popen=lambda *_args, **_kwargs: proc,
            )

        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1)

    def test_parent_accepts_only_the_strict_ok_or_unavailable_verdicts(self):
        tmpdir = tempfile.mkdtemp()
        try:
            result_path = os.path.join(tmpdir, "result.json")
            proc = _FakeProc(poll_returns=0)

            with open(result_path, "w", encoding="utf-8") as handle:
                json.dump({"verdict": "ok"}, handle)
            self.assertIs(
                diag._run_output_endpoint_preflight_subprocess(
                    ["irrelevant"],
                    result_path=result_path,
                    cancel_event=threading.Event(),
                    timeout=1.0,
                    popen=lambda *_args, **_kwargs: proc,
                ),
                True,
            )

            with open(result_path, "w", encoding="utf-8") as handle:
                json.dump({"verdict": "unavailable"}, handle)
            self.assertIs(
                diag._read_output_endpoint_preflight_result(result_path, 0),
                False,
            )

            with open(result_path, "w", encoding="utf-8") as handle:
                json.dump({"verdict": "ok", "detail": "untrusted"}, handle)
            self.assertIsNone(
                diag._read_output_endpoint_preflight_result(result_path, 0)
            )

            for payload, returncode in (
                ({"verdict": "ok"}, 1),
                ({"verdict": "unknown"}, 0),
                ({"verdict": "ok", "padding": "x" * 300}, 0),
            ):
                with self.subTest(payload=payload, returncode=returncode):
                    with open(result_path, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle)
                    self.assertIsNone(
                        diag._read_output_endpoint_preflight_result(
                            result_path,
                            returncode,
                        )
                    )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_temp_directory_is_removed_after_the_child_result(self):
        created_dirs = []
        real_mkdtemp = tempfile.mkdtemp

        def _tracking_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created_dirs.append(path)
            return path

        with mock.patch.object(tempfile, "mkdtemp", _tracking_mkdtemp), mock.patch.object(
            diag,
            "build_output_endpoint_preflight_subprocess_command",
            return_value=["child"],
        ), mock.patch.object(
            diag,
            "_run_output_endpoint_preflight_subprocess",
            return_value=True,
        ):
            result = diag._run_output_endpoint_preflight_in_tempdir(
                "CABLE Input",
                "Windows WASAPI",
                cancel_event=threading.Event(),
                timeout=1.0,
            )

        self.assertIs(result, True)
        self.assertEqual(len(created_dirs), 1)
        self.assertFalse(os.path.exists(created_dirs[0]))

    def test_temp_directory_cleanup_failure_is_reported(self):
        created_dirs = []
        real_mkdtemp = tempfile.mkdtemp

        def _tracking_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created_dirs.append(path)
            return path

        try:
            with mock.patch.object(
                tempfile,
                "mkdtemp",
                _tracking_mkdtemp,
            ), mock.patch.object(
                diag,
                "build_output_endpoint_preflight_subprocess_command",
                return_value=["child"],
            ), mock.patch.object(
                diag,
                "_run_output_endpoint_preflight_subprocess",
                return_value=True,
            ), mock.patch.object(
                diag.shutil,
                "rmtree",
                side_effect=OSError("cleanup failed"),
            ):
                with self.assertRaisesRegex(OSError, "cleanup failed"):
                    diag._run_output_endpoint_preflight_in_tempdir(
                        "CABLE Input",
                        "Windows WASAPI",
                        cancel_event=threading.Event(),
                        timeout=1.0,
                    )
        finally:
            for path in created_dirs:
                shutil.rmtree(path, ignore_errors=True)

    def test_unconfirmed_child_shutdown_remains_primary_if_cleanup_also_fails(self):
        created_dirs = []
        real_mkdtemp = tempfile.mkdtemp
        shutdown_error = diag.OutputEndpointPreflightShutdownUnconfirmedError(
            "child still running"
        )
        cleanup_error = OSError("cleanup failed")

        def _tracking_mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            created_dirs.append(path)
            return path

        try:
            with mock.patch.object(
                tempfile,
                "mkdtemp",
                _tracking_mkdtemp,
            ), mock.patch.object(
                diag,
                "build_output_endpoint_preflight_subprocess_command",
                return_value=["child"],
            ), mock.patch.object(
                diag,
                "_run_output_endpoint_preflight_subprocess",
                side_effect=shutdown_error,
            ), mock.patch.object(
                diag.shutil,
                "rmtree",
                side_effect=cleanup_error,
            ):
                with self.assertRaises(
                    diag.OutputEndpointPreflightShutdownUnconfirmedError
                ) as ctx:
                    diag._run_output_endpoint_preflight_in_tempdir(
                        "CABLE Input",
                        "Windows WASAPI",
                        cancel_event=threading.Event(),
                        timeout=1.0,
                    )
        finally:
            for path in created_dirs:
                shutil.rmtree(path, ignore_errors=True)

        self.assertIs(ctx.exception, shutdown_error)
        self.assertIs(ctx.exception.__cause__, cleanup_error)

    def test_public_wrapper_turns_every_non_success_into_a_sanitized_error(self):
        with mock.patch.object(
            diag, "_run_output_endpoint_preflight_in_tempdir", return_value=True
        ):
            diag.preflight_output_endpoint_isolated("name", "host")

        with mock.patch.object(
            diag, "_run_output_endpoint_preflight_in_tempdir", return_value=False
        ):
            with self.assertRaises(audio_output.AudioOutputUnavailableError) as ctx:
                diag.preflight_output_endpoint_isolated("name", "host")
        self.assertIn("unavailable", str(ctx.exception))

        with mock.patch.object(
            diag,
            "_run_output_endpoint_preflight_in_tempdir",
            side_effect=RuntimeError("private endpoint detail"),
        ):
            with self.assertRaises(audio_output.AudioOutputUnavailableError) as ctx:
                diag.preflight_output_endpoint_isolated("name", "host")
        self.assertNotIn("private endpoint detail", str(ctx.exception))


def _spawn_ovb(*args: str) -> "list[str]":
    env = dict(os.environ)
    repo_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if os.environ.get("OVB_NATIVE_STAGE"):
        repo_src = os.path.join(os.environ["OVB_NATIVE_STAGE"], "src")
    env["PYTHONPATH"] = repo_src
    from tests.source_contract import entry_command
    return entry_command(*args), env


class RunBleDiagnosticsSubprocessTests(unittest.TestCase):
    """Exercises ``_run_ble_diagnostics_subprocess()``
    against REAL OS child processes (never mocked ``Popen`` internals for
    the termination logic itself) - this is the layer the independent
    review's own red probe (an uncooperative-cancel child still hanging
    after 2 seconds) targeted, so it is proven here against a genuine
    process, not merely asserted.
    """

    def test_cancel_event_already_set_never_spawns_a_process_at_all(self):
        spawn_calls = []

        def _counting_popen(cmd, **kwargs):
            spawn_calls.append(cmd)
            raise AssertionError("must never spawn once cancel_event is already set")

        cancel_event = threading.Event()
        cancel_event.set()
        with self.assertRaises(diag.BleDiscoveryCancelledError):
            diag._run_ble_diagnostics_subprocess(
                ["irrelevant"],
                result_path="/irrelevant",
                cancel_event=cancel_event,
                timeout=5.0,
                popen=_counting_popen,
            )
        self.assertEqual(spawn_calls, [])

    def test_poll_failure_terminates_and_confirms_the_child_before_raising(self):
        proc = _FakeProc(poll_raises=OSError("status query failed"))

        with self.assertRaisesRegex(RuntimeError, "child was terminated"):
            diag._run_ble_diagnostics_subprocess(
                ["irrelevant"],
                result_path="/irrelevant",
                cancel_event=threading.Event(),
                timeout=5.0,
                popen=lambda _cmd, **_kwargs: proc,
            )

        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.wait_calls, 1)

    def test_poll_failure_with_unconfirmed_shutdown_raises_the_owner_error(self):
        proc = _FakeProc(
            poll_raises=OSError("status query failed"),
            wait_raises=subprocess.TimeoutExpired(cmd="x", timeout=0.1),
        )

        with self.assertRaises(diag.BleDiscoverySubprocessShutdownUnconfirmedError):
            diag._run_ble_diagnostics_subprocess(
                ["irrelevant"],
                result_path="/irrelevant",
                cancel_event=threading.Event(),
                timeout=5.0,
                terminate_wait=0.1,
                kill_wait=0.1,
                popen=lambda _cmd, **_kwargs: proc,
            )

        self.assertEqual(proc.terminate_calls, 1)
        self.assertEqual(proc.kill_calls, 1)

    def test_a_hanging_child_is_terminated_and_confirmed_dead_within_bound(self):
        # A genuine child process that never exits on its own (no
        # cooperative-cancel handling of any kind) - proves the parent's
        # terminate()+wait() escalation
        # actually confirms a REAL OS process's death, not merely that
        # Python-level bookkeeping believes it did.
        script = "import time\ntime.sleep(120)\n"
        process_holder = {}

        def _spawning_popen(cmd, **kwargs):
            proc = subprocess.Popen([sys.executable, "-c", script], **kwargs)
            process_holder["proc"] = proc
            return proc

        cancel_event = threading.Event()
        started = time.monotonic()
        with self.assertRaises(diag.BleDiscoveryCancelledError):
            diag._run_ble_diagnostics_subprocess(
                ["irrelevant - replaced by _spawning_popen above"],
                result_path="/irrelevant",
                cancel_event=cancel_event,
                timeout=0.2,
                poll_interval=0.05,
                terminate_wait=1.0,
                kill_wait=1.0,
                popen=_spawning_popen,
            )
        elapsed = time.monotonic() - started

        self.assertLess(
            elapsed, 5.0, "termination must be bounded, not left to the 120s child sleep"
        )
        proc = process_holder["proc"]
        # Independently confirm the OS process is REALLY gone (not just
        # that _run_ble_diagnostics_subprocess() believes so) - poll()
        # after a confirmed wait() must report a real exit status.
        self.assertIsNotNone(proc.poll(), "the child process must actually be dead")

    @unittest.skipIf(sys.platform == "win32", "SIGTERM-ignoring is a POSIX-only scenario")
    def test_a_child_that_ignores_sigterm_is_escalated_to_sigkill_and_confirmed_dead(self):
        # The "uncooperative cancel" scenario - a child that survives a
        # first cancellation attempt and is still alive 2 seconds later -
        # only reproducible
        # on POSIX, where a process can choose to ignore SIGTERM (Popen.
        # terminate()) but never SIGKILL (Popen.kill()). On Windows both
        # calls map to the same TerminateProcess() (see _terminate_and_
        # confirm_exit()'s own docstring), so there is nothing to ignore -
        # this is exactly why _terminate_and_confirm_exit() (exercised
        # directly here) always escalates rather than trusting terminate()
        # alone.
        script = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(120)\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", script])
        try:
            time.sleep(0.2)  # let the child install its SIGTERM handler
            started = time.monotonic()
            confirmed_dead = diag._terminate_and_confirm_exit(
                proc, terminate_wait=0.5, kill_wait=2.0
            )
            elapsed = time.monotonic() - started
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)

        self.assertTrue(
            confirmed_dead, "kill() must have been confirmed even though terminate() was ignored"
        )
        self.assertGreater(
            elapsed, 0.5, "must actually have waited out terminate_wait before escalating to kill()"
        )
        self.assertLess(elapsed, 3.0, "the whole escalation must still be bounded")
        self.assertIsNotNone(proc.poll(), "the child process must actually be dead")

    @unittest.skipIf(sys.platform == "win32", "SIGTERM-ignoring is a POSIX-only scenario")
    def test_run_ble_diagnostics_subprocess_kills_an_uncooperative_child_end_to_end(self):
        # Same scenario, but driven through the full
        # _run_ble_diagnostics_subprocess() orchestration (poll loop +
        # cancel_event + escalation), exercising the orchestration more
        # completely than the lower-level test above.
        script = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(120)\n"
        )

        def _spawning_popen(cmd, **kwargs):
            return subprocess.Popen([sys.executable, "-c", script], **kwargs)

        cancel_event = threading.Event()
        threading.Timer(0.1, cancel_event.set).start()

        started = time.monotonic()
        with self.assertRaises(diag.BleDiscoveryCancelledError):
            diag._run_ble_diagnostics_subprocess(
                ["irrelevant - _spawning_popen ignores this"],
                result_path="/irrelevant",
                cancel_event=cancel_event,
                timeout=30.0,
                poll_interval=0.05,
                terminate_wait=1.0,
                kill_wait=2.0,
                popen=_spawning_popen,
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5.0, "the whole kill sequence must still be bounded")




class RunDiagnosticsStopsAfterCancellationTests(unittest.TestCase):
    """Once cancel_event becomes set, run_diagnostics()
    must not continue running checks after whichever one just completed -
    continuing would only prolong worker shutdown for a report that is about
    to be discarded unemitted anyway.
    """

    def test_no_cancel_event_still_runs_all_four_checks(self):
        report = diag.run_diagnostics()
        self.assertEqual(len(report.checks), 4)

    def test_stops_immediately_after_the_check_during_which_cancellation_was_observed(self):
        cancel_event = threading.Event()

        def _fake_check_vb_cable(*_args, **_kwargs):
            # Simulates cancel_event becoming set WHILE the VB-CABLE check was
            # running (e.g. the settings window started closing mid-check) -
            # the real probe is never reached since this replaces the check.
            cancel_event.set()
            return diag.CheckResult(
                "vb_cable_endpoints",
                "VB-CABLE 虚拟音频端点（可选）",
                diag.CheckGroup.VOICE_BRIDGE,
                diag.CheckStatus.FAIL,
                "cancelled",
            )

        with mock.patch.object(diag, "check_vb_cable_endpoints", side_effect=_fake_check_vb_cable):
            report = diag.run_diagnostics(cancel_event=cancel_event)

        ids = [c.check_id for c in report.checks]
        self.assertEqual(ids, ["os_version", "raw_input", "vb_cable_endpoints"])

    def test_cancel_event_already_set_before_the_first_check_stops_after_just_one(self):
        cancel_event = threading.Event()
        cancel_event.set()

        report = diag.run_diagnostics(cancel_event=cancel_event)

        self.assertEqual([c.check_id for c in report.checks], ["os_version"])


if __name__ == "__main__":
    unittest.main()
