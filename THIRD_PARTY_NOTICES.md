# Third-party notices

This repository is licensed as `GPL-3.0-only`. The components listed below are
distributed with, or required by, the Windows client. Each listed item keeps its
own license; nothing here relicenses them. Full license texts are kept under
`THIRD_PARTY_LICENSES/`. Corresponding-source information for Qt for Python is
recorded in `THIRD_PARTY_SOURCE.md`. Asset permissions that are not software
licenses are tracked in `ASSET_LICENSES.md`.

Versions below are the ones this branch is actually built and tested against
(Python 3.14.7, Windows 10), read from the installed distributions on the
maintainer's machine on 2026-10-22.

## Python runtime

| Component | Version | License |
| --- | --- | --- |
| CPython | 3.14.7 | PSF License Agreement (PSF-2.0) |

CPython itself is not distributed in this repository; it is listed because the
Windows portable distribution bundles it and the license text travels with it.

## Python dependencies

| Component | Version | License |
| --- | --- | --- |
| PySide6-Essentials | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| shiboken6 | 6.12.0 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| numpy | 2.5.1 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| sounddevice | 0.5.6 | MIT |
| frida | 17.24.0 | wxWindows Library Licence, Version 3.1 |
| comtypes | 1.4.17 | MIT |
| uiautomation | 2.0.29 | Apache-2.0 |
| pefile | 2024.8.26 | MIT |

Versions are pinned exactly in `requirements.txt`. `numpy` is present because
`sounddevice.OutputStream` hands over NumPy arrays, not because of any numeric
algorithm in this project.

## Bundled third-party binary

| Component | Version | License | Where |
| --- | --- | --- | --- |
| Frida Gadget (`frida-gadget-17.15.3-windows-x86_64.dll.xz`) | 17.15.3 | wxWindows Library Licence, Version 3.1 | `src/ovb/frida_assets/` |

- Upstream release: <https://github.com/frida/frida/releases/download/17.15.3/frida-gadget-17.15.3-windows-x86_64.dll.xz>
- SHA-256 of the archive as shipped: `b566d70189b6d551ad8f4e0bea24de08a3d4c0f559bb35b2bdb67d45182240c2`
- License text: `THIRD_PARTY_LICENSES/Frida-17.15.3/COPYING.txt`

The archive is verified against this digest by `src/ovb/frida_hid_tap_runtime.py`
before use. It is only loaded to recover the three buttons Windows does not
translate (`back`, `volume_up`, `volume_down`); users who do not need those
buttons never load it.

## Qt

Qt for Python (PySide6) is LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only. This
project relies on the LGPL-3.0-only option, which makes the Qt and PySide6
corresponding source a delivery obligation for binary distributions. See
`THIRD_PARTY_SOURCE.md` for the exact source archives and digests.

## Not software licenses

The remote photo and the application icon are covered by `ASSET_LICENSES.md`,
not by any license in this file.
