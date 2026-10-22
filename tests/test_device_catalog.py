import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ovb import audio_output, device_catalog, resources


class DeviceCatalogTests(unittest.TestCase):
    def test_default_catalog_is_loaded_from_the_repository_json(self):
        # Chromecast is the only profile this build loads.
        self.assertIsNone(device_catalog.CATALOG_ERROR)
        self.assertEqual(
            [profile.device_id for profile in device_catalog.DEVICE_PROFILES],
            [device_catalog.CHROMECAST_ID],
        )
        chromecast = device_catalog.profile_for(device_catalog.CHROMECAST_ID)
        self.assertEqual(chromecast.display_name, "谷歌 Chromecast 遥控器")
        self.assertEqual(
            device_catalog.DEFAULT_DISPLAY_NAME, "谷歌 Chromecast 遥控器"
        )

    def test_old_or_unknown_config_defaults_to_the_shipped_profile(self):
        self.assertEqual(
            device_catalog.normalize_device_id(None), device_catalog.CHROMECAST_ID
        )
        self.assertEqual(
            device_catalog.normalize_device_id("unknown"), device_catalog.CHROMECAST_ID
        )



    def test_missing_directory_fails_closed(self):
        missing = Path(tempfile.gettempdir()) / "ovb-catalog-does-not-exist"
        with self.assertRaisesRegex(
            device_catalog.DeviceCatalogLoadError, "directory is unavailable"
        ):
            device_catalog.load_device_catalog(missing)

    def test_missing_field_unknown_status_and_duplicate_id_fail_closed(self):
        source_directory = resources.find_device_profiles_directory()
        self.assertIsNotNone(source_directory)
        # Any valid shipped profile works as the negative-case fixture; the
        # test only needs one that the loader accepts, then corrupts it.
        source_file = source_directory / (device_catalog.DEFAULT_ID + ".json")
        source_profile = json.loads(source_file.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            missing = dict(source_profile)
            missing.pop("displayName")
            (directory / "missing.json").write_text(json.dumps(missing), encoding="utf-8")
            with self.assertRaisesRegex(
                device_catalog.DeviceCatalogLoadError, "missing required field displayName"
            ):
                device_catalog.load_device_catalog(directory)

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            unknown = json.loads(json.dumps(source_profile))
            unknown["platforms"][0]["status"] = "beta"
            (directory / "unknown.json").write_text(json.dumps(unknown), encoding="utf-8")
            with self.assertRaisesRegex(
                device_catalog.DeviceCatalogLoadError, "unknown value 'beta'"
            ):
                device_catalog.load_device_catalog(directory)

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            non_windows = json.loads(json.dumps(source_profile))
            non_windows["platforms"][0]["platform"] = "macos"
            (directory / "non-windows.json").write_text(
                json.dumps(non_windows), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                device_catalog.DeviceCatalogLoadError, "platform is unknown"
            ):
                device_catalog.load_device_catalog(directory)

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            encoded = json.dumps(source_profile)
            (directory / "one.json").write_text(encoded, encoding="utf-8")
            (directory / "two.json").write_text(encoded, encoding="utf-8")
            with self.assertRaisesRegex(
                device_catalog.DeviceCatalogLoadError, "duplicate device profile id"
            ):
                device_catalog.load_device_catalog(directory)

    def test_frozen_locator_reads_only_the_packaged_config_directory(self):
        source = resources.find_device_profiles_directory()
        self.assertIsNotNone(source)
        with tempfile.TemporaryDirectory() as temp_dir:
            frozen_root = Path(temp_dir)
            expected = frozen_root / "config"
            shutil.copytree(source, expected)
            with mock.patch.object(resources.sys, "_MEIPASS", str(frozen_root), create=True):
                self.assertEqual(resources.find_device_profiles_directory(), expected)
                catalog = device_catalog.load_device_catalog()
                self.assertEqual(
                    [profile.device_id for profile in catalog.profiles],
                    [device_catalog.CHROMECAST_ID],
                )

    def test_frozen_locator_does_not_fall_back_to_source_when_bundle_data_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(resources.sys, "_MEIPASS", temp_dir, create=True):
                self.assertIsNone(resources.find_device_profiles_directory())
                with self.assertRaisesRegex(
                    device_catalog.DeviceCatalogLoadError, "directory is unavailable"
                ):
                    device_catalog.load_device_catalog()


if __name__ == "__main__":
    unittest.main()
