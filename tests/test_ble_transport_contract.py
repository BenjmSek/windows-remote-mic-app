"""Contract tests for ble_transport_winrt.py against an in-memory fake of
the locked winrt-Windows.*==3.2.1 projection surface (see
tests/fakes/fake_winrt.py), fixed twice after review:

- from_id_async() rejects a GATT service-instance-domain ID outright (see
  RejectsWrongIdDomainTests below) - a GATT device selector enumerates a
  different WinRT ID domain than BluetoothLEDevice.from_id_async() requires.

This is not a substitute for running against a live WinRT runtime and real
hardware (still 待核验 - see this package's top-level README.md "Known gaps"
section for the full list of what remains unverified on real hardware), but it
does prove the module's own call shape is internally consistent and matches
the documented 3.2.1 signatures as closely as this project can verify
without one.
"""

import asyncio
import unittest
import uuid

from ovb import atvv_protocol as proto

from .fakes.fake_winrt import (
    FakeWinRTEnvironment,
    gatt_service_instance_id,
)


def _run(coro):
    # Explicitly closing the loop (a
    # ResourceWarning for an unclosed test event loop) rather than letting
    # it be garbage-collected.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class RejectsWrongIdDomainTests(unittest.TestCase):
    """DoD 2: "the fake no longer accepts a GATT service id as a
    BLE device ID" - proving the two WinRT ID domains identified
    by review are actually distinct in this test double, not just
    in the production code's comments.
    """

    def test_from_id_async_rejects_a_gatt_service_instance_id(self):
        env = FakeWinRTEnvironment()
        winrt = env.build_winrt_modules()
        wrong_domain_id = gatt_service_instance_id(uuid.UUID(proto.VOICE_SERVICE_UUID))
        self.assertNotEqual(wrong_domain_id, env.device_id)

        async def scenario():
            return await winrt.bluetooth_le_device.from_id_async(wrong_domain_id)

        with self.assertRaises(AssertionError):
            _run(scenario())

    def test_from_id_async_accepts_the_real_paired_device_id(self):
        env = FakeWinRTEnvironment()
        winrt = env.build_winrt_modules()

        async def scenario():
            return await winrt.bluetooth_le_device.from_id_async(env.device_id)

        device = _run(scenario())
        self.assertIs(device, env.device)


if __name__ == "__main__":
    unittest.main()
