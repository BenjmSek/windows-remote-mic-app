"""Locates the optional Chromecast product photo for the settings window.

The second remote's product photo was removed with that profile on 2026-10-19.
The remaining photo lives once at the repository root
(Resources/Chromecast-remote-photo.png) and is not duplicated here; this only
searches a couple of plausible relative locations (packaged build vs. source
checkout) and returns None if it can't be found, so the settings UI degrades
to a text-only button list rather than fabricating or stretching an image.

Frozen (PyInstaller one-dir) layout: the spec's ``datas`` entry collects
the photo under a ``Resources`` folder inside the COLLECT output, which
PyInstaller's bootloader exposes at runtime via ``sys._MEIPASS`` -
documented as "the path to the folder from which the frozen application is
run", i.e. wherever the bootloader actually placed the bundled ``datas``,
regardless of the exact on-disk layout. That is NOT necessarily the same
directory as the executable file itself in a modern one-dir build: since
PyInstaller 6's default one-dir layout, most bundled content (including
anything listed in ``datas``) is placed in the configured one-dir contents
subdirectory (``程序文件`` in current packages)
next to the executable, not loose alongside it, and ``sys._MEIPASS`` points
at that contents directory rather than the top-level dist folder the
``.exe`` itself lives in. Checking ``Path(sys._MEIPASS) / "Resources" /
...`` first (below) is correct regardless of which exact subdirectory
``_MEIPASS`` resolves to, precisely because it is always wherever the
bootloader put ``datas`` - the code does not need to know or assume the
internal layout, only trust ``sys._MEIPASS`` itself. The previous version
of this docstring incorrectly claimed ``sys._MEIPASS`` "equals the
directory containing the frozen executable itself"; that claim is wrong
for this layout and has been removed, without changing the lookup logic
itself, which was already correct. The previous *code* (before)
only checked a ``Resources`` path relative to the process's current working
directory, which is NOT guaranteed to be the executable's own directory
either (it depends on how the user/shortcut launched it) - a real gap
between "the photo was bundled" and "the running app can find it".
``sys._MEIPASS`` is checked first, and only when actually set (i.e. only in
a frozen build); the source-checkout and CWD-relative fallbacks below still
cover the unfrozen/dev case exactly as before.
"""

from __future__ import annotations

import sys
import importlib.machinery
from pathlib import Path
from typing import Iterator, Optional

# Per-profile photos, one per shipped catalog entry. There is deliberately no
# "fallback to some other product's photo" behaviour: before the second
# profile was stripped, an unknown profile fell back to its photo, which
# silently rendered the wrong remote next to Chromecast settings.
_PHOTO_FILENAMES = {"chromecast-remote": "Chromecast-remote-photo.png"}
# The shipped device profile is static data that travels with the checkout, so
# it sits in the same ``config`` directory as the settings files rather than
# in a separate ``device-profiles`` tree - see ``device_catalog`` for why the
# loader skips the settings files that share that directory.
_DEVICE_PROFILES_DIRECTORY = "config"
_APP_ICON_DIRECTORY = "app_icons"
_APP_ICON_FILENAMES = {
    "off": "remote-mic-unavailable.svg",
    "waiting": "remote-mic-unavailable.svg",
    "connected": "remote-mic-connected.svg",
}


def _source_resource_root() -> Path:
    module_path = Path(__file__).resolve()
    if any(str(module_path).endswith(s) for s in importlib.machinery.EXTENSION_SUFFIXES):
        # Unfrozen native-stage tests use the build's copied resources. Frozen
        # execution still exclusively uses _MEIPASS in the callers below.
        return module_path.parents[2]
    return module_path.parents[2]


def _candidate_paths(profile: str = "chromecast-remote") -> Iterator[Path]:
    filename = _PHOTO_FILENAMES.get(profile)
    if filename is None:
        # Retired (or otherwise unshipped) profile: no photo of this product
        # exists, so yield nothing and let the caller show a text-only layout.
        return
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        # Packaged (frozen) build: PyInstaller's bootloader sets this before
        # any application code runs, so it's always current - checked
        # dynamically here (not cached at import time) so tests can
        # simulate a frozen bundle by monkeypatching sys._MEIPASS.
        yield Path(frozen_root) / "Resources" / filename
    # Unfrozen packaged build (e.g. launched with CWD already set to the
    # dist folder): PyInstaller spec copies the photo next to the exe.
    yield Path("Resources") / filename
    # Source checkout: src/ovb/resources.py -> repo root.
    yield _source_resource_root() / "Resources" / filename


def find_remote_photo(profile: str = "chromecast-remote") -> Optional[Path]:
    for candidate in _candidate_paths(profile):
        if candidate.is_file():
            return candidate
    return None


def _device_profile_directory_candidates() -> Iterator[Path]:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        yield Path(frozen_root) / _DEVICE_PROFILES_DIRECTORY
        return
    # Source checkout: src/ovb/resources.py -> repo root.
    yield _source_resource_root() / _DEVICE_PROFILES_DIRECTORY


def find_device_profiles_directory() -> Optional[Path]:
    """Returns only a real shared-catalog directory; never a CWD fallback."""

    for candidate in _device_profile_directory_candidates():
        if candidate.is_dir():
            return candidate
    return None


def _app_icon_directory_candidates() -> Iterator[Path]:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        yield Path(frozen_root) / _APP_ICON_DIRECTORY
        return
    yield Path(__file__).resolve().parent / "assets" / "icons"


def find_app_icon(state: str = "waiting") -> Optional[Path]:
    filename = _APP_ICON_FILENAMES.get(str(state), _APP_ICON_FILENAMES["waiting"])
    for directory in _app_icon_directory_candidates():
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None
