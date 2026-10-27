import ctypes
import threading
import unittest
from unittest import mock

from ovb import doubao_input_profile_windows as directed
from ovb import wetype_control_windows as controls


TARGET = directed.InputTarget(10, 20, 30, 11, 20, 31)
PROFILE = controls._InputProfileSnapshot(1, 0x804, b'a' * 16, b'b' * 16)
OTHER = controls._InputProfileSnapshot(1, 0x804, b'c' * 16, b'd' * 16)


class Proxy:
    def __init__(self):
        self.now = 0.
        self.active = OTHER
        self.context_value = TARGET.focus_thread_id, TARGET.focus_hwnd
        self.requests = []
        self.confirm = True
        self.closed = False
        self.after_request = lambda: None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def pump(self, seconds):
        self.now += seconds

    def context(self):
        return self.context_value

    def active_profile(self):
        return self.active

    def activate(self, target):
        self.requests.append(target)
        if self.confirm:
            self.active = target
        self.after_request()


class ProxyActivateTests(unittest.TestCase):
    """``_ActiveContextProxy.activate`` must pass hkl through.

    A keyboard-layout profile has an all-zero clsid and profile GUID; hkl is
    its only identity. Passing NULL made such a switch unidentifiable, so
    restoring to an English layout could never converge.
    """

    def test_activate_passes_the_profile_hkl(self):
        captured = {}
        layout = controls._InputProfileSnapshot(
            2, 1033, bytes(16), bytes(16), hkl=0x04090409
        )

        class Recorder:
            def __init__(self, pointer):
                self.pointer = pointer

            def __call__(self, _proxy, langid, hkl, clsid, guid):
                captured["langid"] = int(langid)
                # ``_method`` is mocked, so ctypes argtypes never convert the
                # HKL: it arrives as the raw 8-byte little-endian buffer.
                raw = bytes(hkl) if not isinstance(hkl, int) else None
                captured["hkl"] = (
                    int.from_bytes(raw[: ctypes.sizeof(ctypes.c_void_p)], "little")
                    if raw is not None
                    else int(hkl)
                )
                return 0

        proxy = object.__new__(directed._ActiveContextProxy)
        proxy._proxy = ctypes.c_void_p(1)
        with mock.patch.object(directed, "_method", return_value=Recorder(None)):
            proxy.activate(layout)
        self.assertEqual(captured["langid"], 1033)
        self.assertEqual(captured["hkl"], 0x04090409)


class RestoreTimeoutTests(unittest.TestCase):
    """A deferred restore gets a wider budget than the press path."""

    def setUp(self):
        self.proxy = Proxy()
        self.proxy.confirm = False
        self.trace = mock.Mock()
        for patcher in (
            mock.patch.object(directed, '_ActiveContextProxy', return_value=self.proxy),
            mock.patch.object(directed, 'current_target', return_value=TARGET),
            mock.patch.object(directed.time, 'monotonic', side_effect=lambda: self.proxy.now),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_restore_budget_exceeds_the_press_budget(self):
        self.assertGreater(
            directed._RESTORE_TIMEOUT_SECONDS, directed._SWITCH_TIMEOUT_SECONDS
        )
        with self.assertRaises(TimeoutError):
            directed.activate_for_target(
                PROFILE,
                cancelled=lambda: False,
                trace=self.trace,
                timeout_seconds=directed._RESTORE_TIMEOUT_SECONDS,
            )
        self.assertGreaterEqual(
            self.proxy.now, directed._RESTORE_TIMEOUT_SECONDS
        )


class TargetSwitchTests(unittest.TestCase):
    def setUp(self):
        self.proxy = Proxy()
        self.cancel = threading.Event()
        self.trace = mock.Mock()
        for patcher in (
            mock.patch.object(directed, '_ActiveContextProxy', return_value=self.proxy),
            mock.patch.object(directed, 'current_target', return_value=TARGET),
            mock.patch.object(directed.time, 'monotonic', side_effect=lambda: self.proxy.now),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_switch(self):
        return directed.activate_for_target(PROFILE, cancelled=self.cancel.is_set, trace=self.trace)

    def test_switches_once_and_confirms_actual_child_thread(self):
        self.assertNotEqual(TARGET.thread_id, TARGET.focus_thread_id)
        self.assertEqual(self.run_switch(), (True, TARGET))
        self.assertEqual(self.proxy.requests, [PROFILE])
        self.assertGreaterEqual(self.proxy.now, directed._COLD_PROFILE_STABLE_SECONDS)
        self.assertTrue(self.proxy.closed)

    def test_already_selected_sends_no_switch_and_no_cold_delay(self):
        self.proxy.active = PROFILE
        self.assertEqual(self.run_switch(), (False, TARGET))
        self.assertEqual(self.proxy.requests, [])
        self.assertLess(self.proxy.now, directed._COLD_PROFILE_STABLE_SECONDS)

    def test_context_interruption_restarts_profile_stability_wait(self):
        def context():
            if .04 <= self.proxy.now < .09:
                return 999, 999
            return TARGET.focus_thread_id, TARGET.focus_hwnd

        self.proxy.context = context
        self.assertEqual(self.run_switch(), (True, TARGET))
        self.assertGreaterEqual(self.proxy.now, .09 + directed._COLD_PROFILE_STABLE_SECONDS)
        self.assertEqual(self.proxy.requests, [PROFILE])

    def test_unconfirmed_profile_times_out_without_retry_or_rollback(self):
        self.proxy.confirm = False
        with self.assertRaises(TimeoutError):
            self.run_switch()
        self.assertEqual(self.proxy.requests, [PROFILE])
        self.assertTrue(self.proxy.closed)

    def test_wrong_system_target_never_receives_switch(self):
        self.proxy.context_value = (999, 999)
        with self.assertRaises(TimeoutError):
            self.run_switch()
        self.assertEqual(self.proxy.requests, [])
        self.assertIn('input_profile_target_wait', [c.args[0] for c in self.trace.call_args_list])

    def test_cancel_before_request_never_switches(self):
        self.cancel.set()
        with self.assertRaises(TimeoutError):
            self.run_switch()
        self.assertEqual(self.proxy.requests, [])

    def test_cancel_after_request_does_not_switch_back(self):
        self.proxy.after_request = self.cancel.set
        with self.assertRaises(TimeoutError):
            self.run_switch()
        self.assertEqual(self.proxy.requests, [PROFILE])

    def test_focus_change_after_request_discards_preparation(self):
        with mock.patch.object(directed, 'target_is_current', side_effect=lambda _: not self.proxy.requests):
            with self.assertRaisesRegex(OSError, 'target changed'):
                self.run_switch()
        self.assertEqual(self.proxy.requests, [PROFILE])

    def test_interface_failure_is_propagated_without_global_fallback(self):
        with mock.patch.object(directed, '_ActiveContextProxy', side_effect=OSError('unsupported')):
            with self.assertRaisesRegex(OSError, 'unsupported'):
                self.run_switch()
        self.assertEqual(self.proxy.requests, [])

    def test_no_active_profile_can_be_selected(self):
        self.proxy.active = None
        self.assertEqual(self.run_switch(), (True, TARGET))

    def test_s_false_is_not_success(self):
        with self.assertRaises(OSError):
            directed._require_ok(1, 'activate')

    def test_dispatch_rejects_focus_changed_after_successful_preparation(self):
        press = mock.Mock()
        control = controls.DoubaoVoiceControl(activate_profile=lambda: True,
            run_sta=lambda callback: callback(), press_keys=press)
        prepared = control.prepare(['ralt', 'space'])
        prepared = controls.PreparedDoubaoVoiceStart(prepared.generation, prepared.keys, input_target=TARGET)
        with mock.patch.object(directed, 'target_is_current', return_value=False):
            self.assertFalse(control.dispatch_prepared(prepared))
        press.assert_not_called()
        self.assertFalse(control.cleanup_pending)

    def test_joined_selection_keeps_input_target_for_dispatch_check(self):
        source = controls._StaOperation()
        source.deadline = self.proxy.now + 1
        source.input_target = TARGET
        source.set_result(True, True)
        source.mark_settled()
        joined = controls._SelectionWait(source)
        self.assertTrue(joined.result(.5))
        self.assertEqual(joined.input_target, TARGET)


if __name__ == '__main__':
    unittest.main()
