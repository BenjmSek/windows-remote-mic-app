# Asset licenses

This file covers files that are **not** covered by a software license: photos and
icon art. Software licenses are in `THIRD_PARTY_NOTICES.md`.

## Remote photo

| Field | Value |
| --- | --- |
| File | `Resources/Chromecast-remote-photo.png` |
| SHA-256 | `72f0350e9cafd91f2d13c7aa8618fbd5e6c89f27effdb46d74ededdb093f9712` |
| Size | 284,603 bytes |
| Status | Permission granted, redistributed unchanged |

The photo is a photograph of a Google Chromecast voice remote. Its owner
authorized public redistribution of this unchanged image with Remote Mic on
2026-09-24. That authorization was recorded by the upstream project
[`ZSTDJan/windows-remote-mic-app`](https://github.com/ZSTDJan/windows-remote-mic-app)
in its own `ASSET_LICENSES.md`, and the SHA-256 above matches the digest
recorded there byte for byte — this branch ships the same, unmodified file.

The permission covers redistribution of this image with this project. It does
not transfer any trademark right in Google or Chromecast branding, and it does
not authorize anyone to reuse the photo for other purposes. If the permission is
ever withdrawn, remove the file and the two `_PHOTO_FILENAMES` entries in
`src/ovb/resources.py`; the device page then renders as text only.

## Application icon

| File | SHA-256 | Size |
| --- | --- | --- |
| `src/ovb/assets/icons/remote-mic.ico` | `e937bbe408e5de2c6a4d9c34f8d5a8834fc7bb979d2f40d8697fca9019d12c43` | 17,198 |
| `src/ovb/assets/icons/remote-mic-connected.svg` | `61c6688992d3beaa2aff43cae9bd49bc5a4776b39c8945d90b2b12e6697ecefc` | 320 |
| `src/ovb/assets/icons/remote-mic-unavailable.svg` | `24b7ab46bdb7d58fe85ce2c2e921449fe1bf80cf50a0aebb76d2c2c577dfd3de` | 437 |

These are original artwork for this project and are covered by the repository's
`GPL-3.0-only` license. `COPYRIGHT.md` names the contributors. The brand blue
`#4AA8FF` in the two SVG state icons is asserted by
`tests/test_application_icon.py`; changing it turns that test red.

## Not shipped

The upstream project's `RC003-remote-photo.png` (Xiaomi remote) is **not**
included in this branch — the Xiaomi remote path is not part of this fork.
