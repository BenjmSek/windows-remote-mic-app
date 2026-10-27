"""Atomic, session-scoped runtime status shared by bridge and settings.

The in-process bridge worker is the sole writer and the settings controller
is read-only. Each Windows logon session owns a separate status file, matching
the Local\\ bridge mutex scope, so two sessions of the same user cannot delete
or display each other's state. The file contains no device identity, address,
HID path, or voice content. It distinguishes a live process that is waiting,
connecting, waiting to retry after a connection failure, or connected. A PID
guard prevents an exiting old process from deleting a newer process's status
during a restart race.
"""

from __future__ import annotations

import json
import hashlib
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional


SCHEMA_VERSION = 3
PREVIOUS_SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
# Transient bridge state is scoped under runtime/ so config/ keeps only durable
# user settings. The filename stays identical; only its parent directory moves.
RUNTIME_SUBDIRECTORY = "runtime"
STATUS_FILENAME = "bridge-runtime-status.json"
_SCOPED_STATUS_FILENAME_TEMPLATE = "bridge-runtime-status-{scope}.json"
_STATUS_SCOPE_UNSET = object()
_default_status_scope: object | str = _STATUS_SCOPE_UNSET
_default_status_scope_lock = threading.Lock()

# Memory backend switch. Off by default so tests and any caller that wants a
# durable copy keep the historical file behaviour; ``__main__`` turns it on
# for real launches, which is what keeps ``config/runtime`` from existing at
# all. When the switch is on but the section cannot be reached, the file
# path still serves as a working fallback rather than losing the channel.
_SHARED_BACKEND_ENABLED = False
_SHARED_BACKEND_SCOPE: Optional[str] = None
_SHARED_BACKEND_LOCK = threading.Lock()
_BACKEND_UNAVAILABLE = object()

FAILED_RAW_INPUT_STATES = frozenset(
    {
        "unavailable",
        "no_device",
        "ambiguous",
        "failed",
        "failed_running",
        "failed_stopping",
        "stopped",
    }
)
FAILED_HID_TAP_STATES = frozenset(
    {
        "disabled_non_windows",
        "unavailable_gadget_not_verified",
        "unhealthy",
        "failed",
        "failed_stopping",
        "stopped",
    }
)
CHROMECAST_PROFILE = "chromecast-remote"
VOICE_RUNTIME_NOT_TESTED = "not_tested"
VOICE_RUNTIME_ACTIVE = "active"
VOICE_RUNTIME_MIC_CONFIRMED = "mic_confirmed"
VOICE_RUNTIME_RECEIVING_AUDIO = "receiving_audio"
VOICE_RUNTIME_FINISHING = "finishing"
VOICE_RUNTIME_SUCCESS = "success"
VOICE_RUNTIME_OUTPUT_OPEN_FAILED = "output_open_failed"
VOICE_RUNTIME_HOST_START_FAILED = "host_start_failed"
VOICE_RUNTIME_HOST_UNCONFIRMED = "host_unconfirmed"
VOICE_RUNTIME_AUDIO_EMPTY = "audio_empty"
VOICE_RUNTIME_HOST_STOP_FAILED = "host_stop_failed"


class BridgeConnectionState(Enum):
    WAITING_FOR_DEVICE = "waiting_for_device"
    CONNECTING = "connecting"
    RETRY_WAIT = "retry_wait"
    CONNECTED = "connected"


@dataclass(frozen=True)
class BridgeRuntimeIdentity:
    app_version: str
    runtime_kind: str
    package_name: str
    runtime_id: str


@dataclass(frozen=True)
class BridgeRuntimeStatus:
    schema: int
    state: BridgeConnectionState
    pid: int
    updated_at: float
    app_version: str = ""
    runtime_kind: str = ""
    package_name: str = ""
    runtime_id: str = ""
    raw_input_state: str = "unknown"
    hid_tap_state: str = "unknown"
    voice_key_physicalizer_state: str = "unknown"
    last_button_at: Optional[float] = None
    last_button_source: str = ""
    voice_active: bool = False
    voice_runtime_state: str = VOICE_RUNTIME_NOT_TESTED
    voice_runtime_provider: str = ""
    voice_runtime_updated_at: Optional[float] = None
    battery_level: Optional[int] = None


def current_runtime_identity(
    app_version: str,
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    source_root: Optional[Path] = None,
) -> BridgeRuntimeIdentity:
    """Return a privacy-safe identity shared by settings and bridge.

    The digest distinguishes source runs and separate frozen package folders
    without writing a full local path into the status file.
    """

    resolved_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    runtime_kind = "frozen" if resolved_frozen else "source"
    if resolved_frozen:
        runtime_root = Path(executable or sys.executable).resolve().parent
        package_name = runtime_root.name
    else:
        runtime_root = (
            Path(source_root).resolve()
            if source_root is not None
            else Path(__file__).resolve().parents[2]
        )
        package_name = "source-tree"
    identity_source = "\n".join(
        (str(app_version), runtime_kind, os.path.normcase(str(runtime_root)))
    )
    runtime_id = hashlib.sha256(identity_source.encode("utf-8")).hexdigest()[:16]
    return BridgeRuntimeIdentity(
        app_version=str(app_version),
        runtime_kind=runtime_kind,
        package_name=package_name,
        runtime_id=runtime_id,
    )


def _resolve_default_status_scope() -> str:
    global _default_status_scope

    cached = _default_status_scope
    if isinstance(cached, str):
        return cached
    with _default_status_scope_lock:
        cached = _default_status_scope
        if isinstance(cached, str):
            return cached
        if sys.platform != "win32":
            resolved_scope = ""
        else:
            from . import single_instance

            try:
                resolved_scope = (
                    f"s{single_instance.current_process_session_id()}"
                )
            except (
                single_instance.SingleInstanceUnavailableError,
                OSError,
                ValueError,
            ):
                # The in-process bridge and UI share this process fallback.
                # Caching prevents later query recovery from changing paths.
                resolved_scope = f"p{os.getpid()}"
        _default_status_scope = resolved_scope
        return resolved_scope


def _status_scope(session_id: Optional[int]) -> str:
    if session_id is None:
        return _resolve_default_status_scope()
    if isinstance(session_id, bool):
        raise ValueError("session_id must be a non-negative integer")
    resolved = int(session_id)
    if resolved < 0:
        raise ValueError("session_id must be non-negative")
    return f"s{resolved}"


def enable_shared_backend(scope: Optional[str] = None) -> bool:
    """Route transient status through a memory section instead of a file.

    Returns False on non-Windows platforms, where the file backend stays in
    charge. ``scope`` pins the section name; omitting it falls back to the
    per-logon-session scope the file names already used, so two sessions of
    the same user still cannot see each other's bridge.
    """

    global _SHARED_BACKEND_ENABLED, _SHARED_BACKEND_SCOPE

    if sys.platform != "win32":
        return False
    with _SHARED_BACKEND_LOCK:
        _SHARED_BACKEND_SCOPE = scope
        _SHARED_BACKEND_ENABLED = True
    return True


def disable_shared_backend() -> None:
    global _SHARED_BACKEND_ENABLED, _SHARED_BACKEND_SCOPE

    with _SHARED_BACKEND_LOCK:
        _SHARED_BACKEND_ENABLED = False
        _SHARED_BACKEND_SCOPE = None


def shared_backend_active() -> bool:
    return _SHARED_BACKEND_ENABLED


def _shared_scope(session_id: Optional[int]) -> str:
    if _SHARED_BACKEND_SCOPE:
        return _SHARED_BACKEND_SCOPE
    return _status_scope(session_id)


def _shared_module():
    try:
        from . import runtime_shared_windows
    except Exception:  # noqa: BLE001 - missing bindings must not break status
        return None
    return runtime_shared_windows


def _shared_write(payload: bytes, session_id: Optional[int]) -> object:
    if not _SHARED_BACKEND_ENABLED:
        return _BACKEND_UNAVAILABLE
    module = _shared_module()
    if module is None:
        return _BACKEND_UNAVAILABLE
    try:
        from . import hid_elevation_windows

        try:
            sid = hid_elevation_windows.canonical_user_sid(
                hid_elevation_windows.current_user_sid()
            )
        except Exception:  # noqa: BLE001 - default DACL still works same-IL
            sid = ""
        module.write_slot(
            _shared_scope(session_id),
            module.SLOT_STATUS,
            payload,
            sid=sid,
        )
        return True
    except Exception:  # noqa: BLE001 - fall back to the file backend
        return _BACKEND_UNAVAILABLE


def _shared_read(session_id: Optional[int]) -> object:
    if not _SHARED_BACKEND_ENABLED:
        return _BACKEND_UNAVAILABLE
    module = _shared_module()
    if module is None:
        return _BACKEND_UNAVAILABLE
    try:
        return module.read_slot(_shared_scope(session_id), module.SLOT_STATUS)
    except Exception:  # noqa: BLE001 - fall back to the file backend
        return _BACKEND_UNAVAILABLE


def _status_payload(status: BridgeRuntimeStatus) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "state": status.state.value,
        "pid": status.pid,
        "updated_at": status.updated_at,
        "app_version": status.app_version,
        "runtime_kind": status.runtime_kind,
        "package_name": status.package_name,
        "runtime_id": status.runtime_id,
        "raw_input_state": status.raw_input_state,
        "hid_tap_state": status.hid_tap_state,
        "voice_key_physicalizer_state": status.voice_key_physicalizer_state,
        "last_button_at": status.last_button_at,
        "last_button_source": status.last_button_source,
        "voice_active": status.voice_active,
        "voice_runtime_state": status.voice_runtime_state,
        "voice_runtime_provider": status.voice_runtime_provider,
        "voice_runtime_updated_at": status.voice_runtime_updated_at,
        "battery_level": status.battery_level,
    }


def status_path(
    config_root: Path,
    *,
    session_id: Optional[int] = None,
) -> Path:
    scope = _status_scope(session_id)
    filename = (
        _SCOPED_STATUS_FILENAME_TEMPLATE.format(scope=scope)
        if scope
        else STATUS_FILENAME
    )
    path = Path(config_root) / RUNTIME_SUBDIRECTORY / filename
    if _SHARED_BACKEND_ENABLED:
        # The path is only reported to callers that ask for it; creating the
        # directory here would defeat the point of the memory backend.
        return path
    # Bounded runtime state, not durable settings. Creating the scoped
    # directory here keeps every caller - production and tests alike - from
    # having to know that the parent is not the config root itself.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # A read-only root still yields a usable path for callers.
    return path


def runtime_identity_matches(
    status: BridgeRuntimeStatus,
    identity: BridgeRuntimeIdentity,
) -> Optional[bool]:
    if status.schema < SCHEMA_VERSION or not status.runtime_id:
        return None
    return status.runtime_id == identity.runtime_id


def input_channels_failed(status: BridgeRuntimeStatus) -> bool:
    return (
        status.raw_input_state in FAILED_RAW_INPUT_STATES
        and status.hid_tap_state in FAILED_HID_TAP_STATES
    )


def input_channels_ready(status: BridgeRuntimeStatus) -> bool:
    """Whether the running bridge has a usable button path.

    Only the Chromecast receiver's own ``chromecast_ready`` state counts. The
    generic ``ready`` and the HID tap's ``attached_waiting_for_hid_io`` are
    deliberately not accepted: no shipped code publishes either, so accepting
    them would let a bridge whose receiver never confirmed still report ready.
    """

    if status.schema < SCHEMA_VERSION:
        return False
    return status.raw_input_state == "chromecast_ready"


def button_receiver_usable(
    profile: str,
    *,
    raw_input_state: str,
) -> bool:
    """Return whether the selected profile's complete button path is usable.

    Chromecast owns its own receiver and drives readiness off the Raw Input
    state alone; the first report still passes through the normal
    report/source validation in ``app.py``, so this is not a setup action.
    """

    return profile == CHROMECAST_PROFILE and raw_input_state == "chromecast_ready"


def publish_status(
    config_root: Path,
    state: BridgeConnectionState,
    *,
    pid: Optional[int] = None,
    identity: Optional[BridgeRuntimeIdentity] = None,
    raw_input_state: str = "unknown",
    hid_tap_state: str = "unknown",
    voice_key_physicalizer_state: str = "unknown",
    last_button_at: Optional[float] = None,
    last_button_source: str = "",
    voice_active: bool = False,
    voice_runtime_state: str = VOICE_RUNTIME_NOT_TESTED,
    voice_runtime_provider: str = "",
    voice_runtime_updated_at: Optional[float] = None,
    battery_level: Optional[int] = None,
    session_id: Optional[int] = None,
    now: Callable[[], float] = time.time,
) -> BridgeRuntimeStatus:
    resolved_pid = os.getpid() if pid is None else int(pid)
    if resolved_pid <= 0:
        raise ValueError("bridge runtime status requires a positive PID")
    resolved_identity = identity or BridgeRuntimeIdentity("", "", "", "")
    if last_button_at is not None:
        last_button_at = float(last_button_at)
    if voice_runtime_updated_at is not None:
        voice_runtime_updated_at = float(voice_runtime_updated_at)
    if battery_level is not None:
        if (
            not isinstance(battery_level, int)
            or isinstance(battery_level, bool)
            or not 0 <= battery_level <= 100
        ):
            raise ValueError("battery_level must be an integer from 0 to 100")
    status = BridgeRuntimeStatus(
        schema=SCHEMA_VERSION,
        state=BridgeConnectionState(state),
        pid=resolved_pid,
        updated_at=float(now()),
        app_version=resolved_identity.app_version,
        runtime_kind=resolved_identity.runtime_kind,
        package_name=resolved_identity.package_name,
        runtime_id=resolved_identity.runtime_id,
        raw_input_state=str(raw_input_state),
        hid_tap_state=str(hid_tap_state),
        voice_key_physicalizer_state=str(voice_key_physicalizer_state),
        last_button_at=last_button_at,
        last_button_source=str(last_button_source),
        voice_active=bool(voice_active),
        voice_runtime_state=str(voice_runtime_state),
        voice_runtime_provider=str(voice_runtime_provider),
        voice_runtime_updated_at=voice_runtime_updated_at,
        battery_level=battery_level,
    )
    payload = json.dumps(
        _status_payload(status), ensure_ascii=True, sort_keys=True
    ).encode("utf-8")
    if _shared_write(payload, session_id) is True:
        return status
    path = status_path(config_root, session_id=session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(payload.decode("utf-8"), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return status


def _read_status_file(path: Path) -> Optional[BridgeRuntimeStatus]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return _decode_status(payload)


def _decode_status(payload) -> Optional[BridgeRuntimeStatus]:
    if not isinstance(payload, dict):
        return None
    schema = payload.get("schema")
    if schema not in {
        LEGACY_SCHEMA_VERSION,
        PREVIOUS_SCHEMA_VERSION,
        SCHEMA_VERSION,
    }:
        return None
    try:
        state = BridgeConnectionState(payload.get("state"))
    except (TypeError, ValueError):
        return None
    pid = payload.get("pid")
    updated_at = payload.get("updated_at")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool):
        return None
    if schema == LEGACY_SCHEMA_VERSION:
        return BridgeRuntimeStatus(
            schema=LEGACY_SCHEMA_VERSION,
            state=state,
            pid=pid,
            updated_at=float(updated_at),
        )

    string_fields = {
        name: payload.get(
            name,
            "unknown" if name == "voice_key_physicalizer_state" else "",
        )
        for name in (
            "app_version",
            "runtime_kind",
            "package_name",
            "runtime_id",
            "raw_input_state",
            "hid_tap_state",
            "voice_key_physicalizer_state",
            "last_button_source",
            "voice_runtime_state",
            "voice_runtime_provider",
        )
    }
    if "voice_runtime_state" not in payload:
        string_fields["voice_runtime_state"] = VOICE_RUNTIME_NOT_TESTED
    if not all(isinstance(value, str) for value in string_fields.values()):
        return None
    last_button_at = payload.get("last_button_at")
    if last_button_at is not None and (
        not isinstance(last_button_at, (int, float))
        or isinstance(last_button_at, bool)
    ):
        return None
    voice_active = payload.get("voice_active", False)
    if not isinstance(voice_active, bool):
        return None
    voice_runtime_updated_at = payload.get("voice_runtime_updated_at")
    if voice_runtime_updated_at is not None and (
        not isinstance(voice_runtime_updated_at, (int, float))
        or isinstance(voice_runtime_updated_at, bool)
    ):
        return None
    battery_level = payload.get("battery_level")
    if (
        battery_level is not None
        and (
            not isinstance(battery_level, int)
            or isinstance(battery_level, bool)
            or not 0 <= battery_level <= 100
        )
    ):
        battery_level = None
    return BridgeRuntimeStatus(
        schema=schema,
        state=state,
        pid=pid,
        updated_at=float(updated_at),
        app_version=string_fields["app_version"],
        runtime_kind=string_fields["runtime_kind"],
        package_name=string_fields["package_name"],
        runtime_id=string_fields["runtime_id"],
        raw_input_state=string_fields["raw_input_state"],
        hid_tap_state=string_fields["hid_tap_state"],
        voice_key_physicalizer_state=(
            string_fields["voice_key_physicalizer_state"]
        ),
        last_button_at=(
            None if last_button_at is None else float(last_button_at)
        ),
        last_button_source=string_fields["last_button_source"],
        voice_active=voice_active,
        voice_runtime_state=string_fields["voice_runtime_state"],
        voice_runtime_provider=string_fields["voice_runtime_provider"],
        voice_runtime_updated_at=(
            None
            if voice_runtime_updated_at is None
            else float(voice_runtime_updated_at)
        ),
        battery_level=battery_level,
    )


def read_status(
    config_root: Path,
    *,
    session_id: Optional[int] = None,
) -> Optional[BridgeRuntimeStatus]:
    shared = _shared_read(session_id)
    if shared is not _BACKEND_UNAVAILABLE:
        if shared is None:
            # The section is the whole state: an absent or empty slot means
            # no live bridge, and a leftover file must not resurrect it.
            return None
        try:
            return _decode_status(
                json.loads(shared.decode("utf-8"))
            )
        except (UnicodeError, json.JSONDecodeError):
            return None
    return _read_status_file(status_path(config_root, session_id=session_id))


def _restore_claimed_status(claimed: Path, path: Path) -> bool:
    """Restore a claimed file only when no newer writer owns ``path``."""

    try:
        if sys.platform == "win32":
            # Windows rename fails instead of replacing an existing target.
            os.rename(claimed, path)
        else:
            # POSIX rename replaces the target, so use a no-replace hard link.
            os.link(claimed, path)
            claimed.unlink()
    except FileExistsError:
        return False
    return True


def _clear_shared_status(module, session_id: Optional[int], pid: Optional[int]) -> bool:
    scope = _shared_scope(session_id)
    if not module.section_exists(scope):
        # No section means no bridge and no state: nothing to clean up. This
        # is the normal path for a reader clearing after a bridge exit.
        return False
    payload = module.read_slot(scope, module.SLOT_STATUS)
    if payload is None:
        return False
    try:
        current = _decode_status(json.loads(payload.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError):
        return False
    if current is None:
        return False
    if pid is not None and current.pid != int(pid):
        return False
    try:
        module.write_slot(scope, module.SLOT_STATUS, b"")
    except Exception:  # noqa: BLE001 - a reader above our integrity level
        return False
    return True


def clear_status(
    config_root: Path,
    *,
    pid: Optional[int] = None,
    session_id: Optional[int] = None,
) -> bool:
    """Remove one atomically claimed status generation.

    Claiming the current path before checking its PID prevents an exiting old
    bridge from deleting a replacement status published by a newer bridge.
    """

    if _SHARED_BACKEND_ENABLED:
        module = _shared_module()
        if module is not None:
            try:
                return _clear_shared_status(module, session_id, pid)
            except Exception:  # noqa: BLE001 - fall back to the file backend
                pass
    path = status_path(config_root, session_id=session_id)
    expected_pid = None if pid is None else int(pid)
    claimed = path.with_name(f".{path.name}.{uuid.uuid4().hex}.clear")
    try:
        os.replace(path, claimed)
    except FileNotFoundError:
        return False

    remove_claimed = True
    try:
        if expected_pid is not None:
            current = _read_status_file(claimed)
            if current is None or current.pid != expected_pid:
                try:
                    _restore_claimed_status(claimed, path)
                except OSError:
                    # Keep the claimed file for diagnosis instead of deleting
                    # the only complete copy after a failed safe restore.
                    remove_claimed = False
                    raise
                return False
        return True
    finally:
        if remove_claimed:
            try:
                claimed.unlink()
            except FileNotFoundError:
                pass
