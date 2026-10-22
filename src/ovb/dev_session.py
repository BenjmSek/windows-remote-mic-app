"""Source-launch markers and the portable (self-contained) data root.

Two independent markers exist:

* ``--remote-mic-dev-session`` marks a launch that came from the source
  workspace rather than a packaged build.
* ``--isolated-test`` additionally redirects every writable path into a
  scratch tree so tests can never touch a real user's data.

When either is active, all writable state moves next to the source tree
instead of ``%LOCALAPPDATA%`` - see :func:`portable_root`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, List


DEV_SESSION_FLAG = "--remote-mic-dev-session"
DEV_SESSION_ENV = "REMOTE_MIC_DEV_SESSION"
ISOLATED_FLAG = "--isolated-test"
ISOLATED_ENV = "REMOTE_MIC_ISOLATED_TEST"

# Workspace root: src/ovb/dev_session.py -> parents[2] is the checkout.
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


def is_isolated() -> bool:
    return os.environ.get(ISOLATED_ENV) == "1"


def is_active() -> bool:
    return os.environ.get(DEV_SESSION_ENV) == "1"


def portable_root() -> Path:
    """The data root for a source/portable launch, or ``None`` if packaged.

    Returns ``None`` for a frozen (PyInstaller) build, which keeps writing
    under ``%LOCALAPPDATA%``: a normal install has no writable copy of the
    program directory, and moving installed state there would break upgrades.

    An isolated test run outranks the portable root - its whole point is a
    scratch tree no real session ever reads - so it is checked first.
    """
    # isolated_root() fails closed on a frozen build, so it must run before
    # the frozen check below; that refusal is the point of the isolated flag.
    if is_isolated():
        return isolated_root()
    if getattr(sys, "frozen", False):
        return None
    if is_active():
        return WORKSPACE_ROOT / "config"
    return None


def portable_log_root() -> Path:
    """The log directory for a source/portable launch, or ``None`` if packaged.

    Separate from :func:`portable_root` because logs are written to the
    workspace's own ``log`` directory, not under the settings directory, so
    a developer sees them without digging into the runtime tree.
    """
    if is_isolated():
        return isolated_root() / "logs"
    if getattr(sys, "frozen", False):
        return None
    if is_active():
        return WORKSPACE_ROOT / "log"
    return None


def isolated_root() -> Path:
    """Source-only fixed data root; never redirect OS application discovery."""
    if getattr(sys, "frozen", False):
        raise ValueError("隔离测试入口仅限源码运行。")
    root = WORKSPACE_ROOT / ".build" / "isolated-runtime"
    if root.resolve() != root:
        raise ValueError("隔离测试目录被重定向，拒绝启动。")
    return root


def consume_marker(arguments: Iterable[str]) -> List[str]:
    """Remove the private CLI marker and enable inheritance for children."""

    source = list(arguments)
    if ISOLATED_FLAG in source:
        isolated_root()  # Validate before enabling inheritance.
        os.environ[ISOLATED_ENV] = "1"
        source = [argument for argument in source if argument != ISOLATED_FLAG]
    if is_isolated():
        isolated_root()
    cleaned = [argument for argument in source if argument != DEV_SESSION_FLAG]
    if len(cleaned) != len(source):
        os.environ[DEV_SESSION_ENV] = "1"
    return cleaned


def mark_command(command: Iterable[str]) -> List[str]:
    marked = list(command)
    if is_isolated() and ISOLATED_FLAG not in marked:
        marked.append(ISOLATED_FLAG)
    if is_active() and DEV_SESSION_FLAG not in marked:
        marked.append(DEV_SESSION_FLAG)
    return marked
