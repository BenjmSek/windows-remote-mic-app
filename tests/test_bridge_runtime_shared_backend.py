"""Memory-backed bridge status channel.

The channel replaced a JSON file under ``config/runtime``. These tests pin the
two properties that made the swap worthwhile - nothing is written to disk, and
an absent section means "no bridge" rather than "stale file from last run" -
plus the PID guard ``clear_status`` still honours.
"""

from __future__ import annotations

import ctypes as C
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


from ovb import bridge_runtime_status as status
from ovb import runtime_shared_windows as shared


WINDOWS_ONLY = unittest.skipUnless(
    sys.platform == "win32", "session-local section objects are Windows-only"
)


class _SharedBackendCase(unittest.TestCase):
    def setUp(self):
        self.scope = f"test-{uuid.uuid4().hex[:12]}"
        self.root = Path(tempfile.mkdtemp(prefix="ovb-status-"))
        shared.release_writers()
        self.assertTrue(status.enable_shared_backend(self.scope))

    def tearDown(self):
        shared.release_writers()
        status.disable_shared_backend()

    def publish(self, pid=4242, **overrides):
        kwargs = {
            "raw_input_state": "chromecast_ready",
            "hid_tap_state": "attached",
            "battery_level": 88,
        }
        kwargs.update(overrides)
        return status.publish_status(
            self.root,
            status.BridgeConnectionState.CONNECTED,
            pid=pid,
            **kwargs,
        )

    def runtime_directory(self) -> Path:
        return self.root / "runtime"


@WINDOWS_ONLY
class PublishAndReadTests(_SharedBackendCase):
    def test_round_trip_creates_no_runtime_directory(self):
        self.publish()

        restored = status.read_status(self.root)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.pid, 4242)
        self.assertEqual(restored.state, status.BridgeConnectionState.CONNECTED)
        self.assertEqual(restored.raw_input_state, "chromecast_ready")
        self.assertEqual(restored.battery_level, 88)
        self.assertFalse(
            self.runtime_directory().exists(),
            "the memory backend must not leave config/runtime behind",
        )

    def test_status_path_still_reports_a_named_location(self):
        path = status.status_path(self.root)

        self.assertEqual(path.name, "bridge-runtime-status-s1.json")
        self.assertFalse(path.parent.exists())

    def test_absent_section_reads_as_no_bridge(self):
        self.assertIsNone(status.read_status(self.root))

    def test_stale_file_does_not_resurrect_status(self):
        stale = status.status_path(self.root)
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(
            json.dumps(
                {
                    "schema": status.SCHEMA_VERSION,
                    "state": "connected",
                    "pid": 9999,
                    "updated_at": 1.0,
                }
            ),
            encoding="utf-8",
        )

        self.assertIsNone(
            status.read_status(self.root),
            "a leftover file must not be mistaken for a live bridge",
        )

    def test_releasing_the_writer_drops_the_section(self):
        self.publish()
        self.assertTrue(shared.section_exists(self.scope))

        shared.release_writers()

        self.assertFalse(shared.section_exists(self.scope))
        self.assertIsNone(status.read_status(self.root))

    def test_torn_write_is_discarded(self):
        self.publish()
        holder = shared.writer(self.scope)
        offset = shared.HEADER_BYTES
        # Odd sequence marks the slot as mid-rewrite.
        C.c_uint32.from_address(holder.view + offset).value = 7

        self.assertIsNone(status.read_status(self.root))


@WINDOWS_ONLY
class ClearStatusTests(_SharedBackendCase):
    def test_matching_pid_clears_the_slot(self):
        self.publish(pid=4242)

        self.assertTrue(status.clear_status(self.root, pid=4242))
        self.assertIsNone(status.read_status(self.root))

    def test_mismatched_pid_leaves_the_slot_alone(self):
        self.publish(pid=4242)

        self.assertFalse(status.clear_status(self.root, pid=1))
        restored = status.read_status(self.root)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.pid, 4242)

    def test_clearing_without_a_section_reports_nothing_to_do(self):
        self.assertFalse(status.clear_status(self.root, pid=4242))

    def test_second_clear_is_a_no_op(self):
        self.publish(pid=4242)
        self.assertTrue(status.clear_status(self.root, pid=4242))

        self.assertFalse(status.clear_status(self.root, pid=4242))


@WINDOWS_ONLY
class CrossProcessTests(_SharedBackendCase):
    def test_another_process_reads_the_same_section(self):
        self.publish(pid=7777, battery_level=55)
        source = str(Path(__file__).resolve().parents[1] / "src")
        script = (
            "import sys\n"
            f"sys.path.insert(0, r'{source}')\n"
            "from pathlib import Path\n"
            "from ovb import bridge_runtime_status as status\n"
            f"status.enable_shared_backend('{self.scope}')\n"
            f"restored = status.read_status(Path(r'{self.root}'))\n"
            "print('none' if restored is None else restored.pid)\n"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = source

        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "7777")
        self.assertFalse(self.runtime_directory().exists())


class FileBackendFallbackTests(unittest.TestCase):
    def setUp(self):
        shared.release_writers()
        status.disable_shared_backend()
        self.root = Path(tempfile.mkdtemp(prefix="ovb-status-file-"))

    def tearDown(self):
        shared.release_writers()
        status.disable_shared_backend()

    def test_disabled_backend_keeps_writing_the_file(self):
        status.publish_status(
            self.root,
            status.BridgeConnectionState.WAITING_FOR_DEVICE,
            pid=1234,
        )

        path = status.status_path(self.root)
        self.assertTrue(path.is_file())
        restored = status.read_status(self.root)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.pid, 1234)
        self.assertEqual(restored.state, status.BridgeConnectionState.WAITING_FOR_DEVICE)

    def test_disabled_backend_still_clears_by_pid(self):
        status.publish_status(
            self.root,
            status.BridgeConnectionState.CONNECTED,
            pid=1234,
        )

        self.assertFalse(status.clear_status(self.root, pid=99))
        self.assertTrue(status.clear_status(self.root, pid=1234))
        self.assertIsNone(status.read_status(self.root))


if __name__ == "__main__":
    unittest.main()
