# Corresponding source for Qt for Python

Qt for Python (PySide6) is licensed `LGPL-3.0-only OR GPL-2.0-only OR
GPL-3.0-only`. This project relies on the LGPL-3.0-only option. LGPL-3.0
section 4 requires that a binary distribution ship the Corresponding Source for
the LGPL-licensed libraries it contains, or a written offer valid for at least
three years to supply it on request.

This file records exactly what to ship. The digests below are the values
published by Qt next to each archive, not values computed by this project; the
sidecar URL is given so anyone can re-verify them.

## Qt 6.12.0

| Field | Value |
| --- | --- |
| Archive | `qt-everywhere-src-6.12.0.tar.xz` |
| URL | <https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz> |
| SHA-256 | `98ff4f44bac6ec3e1e62ee2a4316ae0e3d15badb015d753cc0268a20db52f165` |
| Digest source | <https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz.sha256> |

## PySide6 6.12.0

| Field | Value |
| --- | --- |
| Archive | `pyside-setup-everywhere-src-6.12.0.tar.xz` |
| URL | <https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz> |
| SHA-256 | `099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751` |
| Digest source | <https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz.sha256> |

## What this means in practice

- **Source-only checkout (this repository as published): no obligation
  arises.** No Qt or PySide6 binary is distributed here; `requirements.txt`
  only names the wheels, and the user installs them from PyPI.
- **If you publish a Windows installer or portable archive that bundles the
  PySide6/Qt DLLs, the obligation does arise.** Ship the two archives above, or
  ship a written offer naming them and stating that you will supply them on
  request for at least three years.

The version pair above is what this branch is built and tested against. If you
change the pinned `PySide6-Essentials` version, update both tables to the
matching Qt release — the Qt version and the PySide6 version must correspond.
