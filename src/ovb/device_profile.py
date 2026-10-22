"""Static identity facts for the Google Chromecast Voice Remote.

Kept consistent with ``config/chromecast-remote.json`` (vendorId 6353
= 0x18D1, productId 37968 = 0x9450) so the client identifies the intended
physical product. The literals stay here rather than being loaded from the
catalog because ``resources`` imports this module while ``device_catalog``
imports ``resources``; reading the JSON from here would close an import cycle.

The VID/PID are confirmed against this machine's paired device, whose
BTHLEDevice hardware id reads ``Dev_VID&0118d1_PID&9450_REV&0110``.
"""

from __future__ import annotations

DISPLAY_NAME = "谷歌 Chromecast 遥控器"

# Bluetooth advertised/paired device names this client will match against.
# Matching is exact (case/whitespace-insensitive) - never a fuzzy substring -
# per the project rule against broad vendor matching.
BLUETOOTH_NAMES = frozenset({"chromecast remote"})

HID_VENDOR_ID = 0x18D1
HID_PRODUCT_ID = 0x9450

# HID Keyboard-page (Usage Page 0x07) usage IDs reported by the remote's HID
# interface. The microphone button is normally expected to be signaled over
# the ATVV GATT control channel, but real Windows hardware also reports it as
# Keyboard F5 (usage 0x003E), so it is included here as a host-trigger
# fallback. One button ("back") sits
# outside the standard translated usage range on Windows; see
# ovb.frida_compat for the documented optional compatibility
# gap.
BUTTON_USAGE_IDS = {
    0x003E: "mic",
    0x00F1: "back",
    0x0028: "ok",
    0x0035: "tv",
    0x004A: "home",
    0x004F: "right",
    0x0050: "left",
    0x0051: "down",
    0x0052: "up",
    0x0065: "menu",
    0x0066: "power",
    0x007F: "volume_mute",
    0x0080: "volume_up",
    0x0081: "volume_down",
}

# All logical buttons this client knows about.
ALL_BUTTON_IDS = frozenset(set(BUTTON_USAGE_IDS.values()) | {"mic"})
