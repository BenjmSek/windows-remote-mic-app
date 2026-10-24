"""Stable product identity and Windows portable-layout naming.

The public presentation name may change without changing the portable
distribution's directory layout.  Keep the visible Windows filenames derived
here so this package and the code that locates a frozen copy agree.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePath


DISPLAY_NAME = "无线麦Mod"
WINDOWS_EDITION_NAME = "win版"
WINDOWS_PRODUCT_NAME = f"{DISPLAY_NAME} {WINDOWS_EDITION_NAME}"
WINDOWS_RUNTIME_DIRECTORY_NAME = "程序文件"

_VERSION_PATTERN = re.compile(
    r"(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<suffix>[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*))?"
)
_WINDOWS_EXECUTABLE_PATTERN = re.compile(
    rf"{re.escape(WINDOWS_PRODUCT_NAME)} (?P<version>.+)\.exe",
    re.IGNORECASE,
)
_WINDOWS_VERSION_COMPONENT_MAX = 65_535


def validate_version(version: str) -> str:
    """Return one supported VERSION value, rejecting path-like input."""

    if not isinstance(version, str):
        raise TypeError("version must be a string")
    normalized = version.strip()
    if normalized != version or not _VERSION_PATTERN.fullmatch(normalized):
        raise ValueError(f"unsupported application version: {version!r}")
    if PurePath(normalized).name != normalized or any(
        character in normalized for character in ("/", "\\", "\0")
    ):
        raise ValueError(f"invalid application version: {version!r}")
    return normalized


def windows_presentation_label(version: str) -> str:
    return f"{WINDOWS_PRODUCT_NAME} {validate_version(version)}"


def windows_window_title() -> str:
    """The settings-window title: product name without the version suffix.

    Kept separate from windows_presentation_label() on purpose - that one
    also builds the executable file name, which must keep its version.
    """

    return WINDOWS_PRODUCT_NAME


def windows_executable_name(version: str) -> str:
    return f"{windows_presentation_label(version)}.exe"


def windows_fixed_file_version(version: str) -> tuple[int, int, int, int]:
    """Map the semantic version to the numeric Windows fixed-file tuple."""

    match = _VERSION_PATTERN.fullmatch(validate_version(version))
    assert match is not None
    values = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
    if any(value > _WINDOWS_VERSION_COMPONENT_MAX for value in values):
        raise ValueError("Windows version component exceeds 65535")
    return (*values, 0)


def windows_version_is_prerelease(version: str) -> bool:
    match = _VERSION_PATTERN.fullmatch(validate_version(version))
    assert match is not None
    return bool(match.group("suffix"))


def recognized_windows_executable_layout(name: str) -> str | None:
    """Return ``current`` only for an exact known basename."""

    if not isinstance(name, str) or not name or Path(name).name != name:
        return None
    match = _WINDOWS_EXECUTABLE_PATTERN.fullmatch(name)
    if match is None:
        return None
    try:
        validate_version(match.group("version"))
    except (TypeError, ValueError):
        return None
    return "current"


def is_recognized_windows_executable_name(name: str) -> bool:
    return recognized_windows_executable_layout(name) is not None


def windows_runtime_relative_directory_for_executable(name: str) -> Path:
    layout = recognized_windows_executable_layout(name)
    if layout == "current":
        return Path(WINDOWS_RUNTIME_DIRECTORY_NAME)
    raise ValueError(f"unrecognized Windows executable name: {name!r}")


