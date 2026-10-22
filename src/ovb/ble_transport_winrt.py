# -*- coding: utf-8 -*-
"""The one place that knows how to import the optional ``winrt-Windows.*``
packages, and the single error type for "they are not installed".

Windows-only. Importing this module never fails without the optional
``winrt-Windows.*`` packages installed; only calling ``_import_winrt()``
does, with a clear ``WinRTUnavailableError`` instead of a raw
``ModuleNotFoundError`` from inside an awaited coroutine.

Why the projections are imported explicitly  the WinRT calls a
caller makes with these classes return a
``Windows.Foundation.IAsyncOperation``, and iterating the resulting
``DeviceInformationCollection`` uses a
``Windows.Foundation.Collections`` ``IIterator``. Both projections are
exact ``3.2.1`` pins in requirements.txt and in the PyInstaller hidden
imports, so importing them here, up front, turns a missing pin into this
module's own ``WinRTUnavailableError`` rather than a raw
``ModuleNotFoundError`` surfacing deep inside a caller (see
requirements.txt's comment for the exact red evidence this
closes).

Standard Battery Service GATT UUIDs. Nothing in the app subscribes to
battery level any more, but ``tests/fakes/fake_winrt.py`` reads them from
this module so the fake projection and the real one stay in sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


class WinRTUnavailableError(Exception):
    """Raised when the optional winrt Bluetooth packages are not installed."""


@dataclass(frozen=True)
class WinRTModules:
    """Everything this module needs from the winrt packages, grouped so a
    caller can be handed - or a test can inject - an object implementing the
    same call shape instead of requiring the real packages to be installed.
    """

    bluetooth_le_device: Any
    bluetooth_connection_status: Any
    gatt_communication_status: Any
    cccd_value: Any
    device_information: Any
    data_writer_factory: Callable[[], Any]
    data_reader_factory: Callable[[Any], Any]
    bluetooth_cache_mode: Any


def _import_winrt() -> WinRTModules:
    try:
        from winrt.windows.devices.bluetooth import (
            BluetoothCacheMode,
            BluetoothConnectionStatus,
            BluetoothLEDevice,
        )
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattClientCharacteristicConfigurationDescriptorValue,
            GattCommunicationStatus,
        )
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.storage.streams import DataReader, DataWriter

        # not referenced by name anywhere below - discovered only
        # when a real WinRT call is awaited (DeviceInformation.find_all_
        # async_aqs_filter() returns a Windows.Foundation.IAsyncOperation;
        # iterating its result uses a Windows.Foundation.Collections
        # IIterator) - so importing them here, up front, turns a missing
        # projection into this function's clean WinRTUnavailableError
        # instead of a raw ModuleNotFoundError surfacing deep inside an
        # awaited coroutine (see requirements.txt's comment for the
        # exact red evidence this closes).
        import winrt.windows.foundation  # noqa: F401
        import winrt.windows.foundation.collections  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only on Windows
        raise WinRTUnavailableError(
            "winrt Bluetooth packages are not installed; in a Windows virtual "
            "environment run `pip install winrt-Windows.Devices.Bluetooth "
            "winrt-Windows.Devices.Bluetooth.GenericAttributeProfile "
            "winrt-Windows.Devices.Enumeration`"
        ) from exc
    return WinRTModules(
        bluetooth_le_device=BluetoothLEDevice,
        bluetooth_connection_status=BluetoothConnectionStatus,
        gatt_communication_status=GattCommunicationStatus,
        cccd_value=GattClientCharacteristicConfigurationDescriptorValue,
        device_information=DeviceInformation,
        data_writer_factory=DataWriter,
        data_reader_factory=DataReader.from_buffer,
        bluetooth_cache_mode=BluetoothCacheMode,
    )
