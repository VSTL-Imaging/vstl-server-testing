import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


se = _load_module("vstl_secure_erase", "bench-client/vstl_secure_erase.py")
hw = _load_module("vstl_hw_detect", "bench-client/vstl_hw_detect.py")
qc = _load_module("vstl_qc_tests_storage", "bench-client/vstl_qc_tests.py")


class StorageCapacityUnitsTests(unittest.TestCase):
    def test_lenovo_identity_uses_mtm_as_sku_and_fm_text_as_model(self):
        dmi = {
            "system-manufacturer": "LENOVO",
            "system-product-name": "20WLS1G400",
            "system-serial-number": "PF3ABCDE",
            "system-sku-number": "LENOVO_MT_20WL_BU_Think_FM_ThinkPad X13 Gen 2i",
        }

        with (
            mock.patch.object(hw, "_dmidecode", side_effect=lambda field: dmi[field]),
            mock.patch.object(hw, "detect_mac", return_value="88:A4:C2:29:B5:B0"),
        ):
            identity = hw.detect_identity()

        self.assertEqual(identity["brand"], "LENOVO")
        self.assertEqual(identity["model"], "ThinkPad X13 Gen 2i")
        self.assertEqual(identity["sku"], "20WLS1G400")
        self.assertEqual(identity["serial_no"], "PF3ABCDE")

    def test_secure_erase_uses_vendor_decimal_gb(self):
        self.assertEqual(se._storage_capacity_gb(256_060_514_304), 256)
        self.assertEqual(se._storage_capacity_gb(512_110_190_592), 512)

    def test_hw_storage_label_uses_vendor_decimal_gb(self):
        self.assertEqual(hw._storage_size_label(256_060_514_304), "256 GB")
        self.assertEqual(hw._storage_size_label(1_000_204_886_016), "1 TB")

    def test_qc_storage_filter_excludes_usb_boot_media(self):
        self.assertFalse(qc._block_device_is_internal_disk({
            "name": "sda",
            "type": "disk",
            "tran": "usb",
            "rm": 0,
            "hotplug": 1,
            "mountpoints": ["/run/live/medium"],
        }))
        self.assertTrue(qc._block_device_is_internal_disk({
            "name": "nvme0n1",
            "type": "disk",
            "tran": "nvme",
            "rm": 0,
            "hotplug": 0,
            "mountpoints": [],
        }))

    def test_storage_identity_does_not_copy_model_into_ct_or_part(self):
        smart = "\n".join([
            "Model Number: SK hynix BC901 HFS256GEJ9X108N",
            "Serial Number: 5MC7N000110407236",
            "Firmware Version: 51020C00",
        ])
        with mock.patch.object(hw, "_run", side_effect=[smart, ""]):
            identity = hw._storage_identity("nvme0")
        self.assertEqual(identity["product_number"], "SK hynix BC901 HFS256GEJ9X108N")
        self.assertEqual(identity["ct_number"], hw.UNKNOWN)
        self.assertEqual(identity["part_number"], hw.UNKNOWN)

    def test_battery_uses_only_explicit_ct_and_part_fields(self):
        files = {
            "/sys/class/power_supply/BAT0/energy_full_design": "51310000",
            "/sys/class/power_supply/BAT0/energy_full": "46200000",
            "/sys/class/power_supply/BAT0/cycle_count": "127",
            "/sys/class/power_supply/BAT0/manufacturer": "Hewlett-Packard",
            "/sys/class/power_supply/BAT0/model_name": "Primary",
            "/sys/class/power_supply/BAT0/serial_number": "23827 2023/09/13",
        }

        def fake_open(path, *args, **kwargs):
            path = str(path).replace("\\", "/")
            if path not in files:
                raise OSError(path)
            return io.StringIO(files[path])

        with mock.patch.object(hw.os.path, "isdir", return_value=True), \
             mock.patch.object(hw.os, "listdir", return_value=["BAT0"]), \
             mock.patch("builtins.open", side_effect=fake_open):
            battery = hw.detect_battery()["batteries"][0]

        self.assertEqual(battery["design_capacity_mwh"], "51310")
        self.assertEqual(battery["current_capacity_mwh"], "46200")
        self.assertEqual(battery["full_charged_capacity_mwh"], "46200")
        self.assertEqual(battery["health"], "90%")
        self.assertEqual(battery["cycle_count"], "127")
        self.assertEqual(battery["ct_number"], hw.UNKNOWN)
        self.assertEqual(battery["part_number"], hw.UNKNOWN)

    def test_battery_health_recovers_full_capacity_from_current_percent(self):
        files = {
            "/sys/class/power_supply/BAT0/energy_full_design": "50000000",
            "/sys/class/power_supply/BAT0/energy_full": "20000000",
            "/sys/class/power_supply/BAT0/energy_now": "20000000",
            "/sys/class/power_supply/BAT0/capacity": "40",
        }

        def fake_open(path, *args, **kwargs):
            path = str(path).replace("\\", "/")
            if path not in files:
                raise OSError(path)
            return io.StringIO(files[path])

        with mock.patch.object(hw.os.path, "isdir", return_value=True), \
             mock.patch.object(hw.os, "listdir", return_value=["BAT0"]), \
             mock.patch("builtins.open", side_effect=fake_open), \
             mock.patch.object(hw, "_run", return_value=""):
            battery = hw.detect_battery()["batteries"][0]

        self.assertEqual(battery["design_capacity_mwh"], "50000")
        self.assertEqual(battery["current_capacity_mwh"], "50000")
        self.assertEqual(battery["full_charged_capacity_mwh"], "50000")
        self.assertEqual(battery["health"], "100%")

    def test_battery_health_recovers_degraded_full_capacity_from_current_percent(self):
        files = {
            "/sys/class/power_supply/BAT0/energy_full_design": "50000000",
            "/sys/class/power_supply/BAT0/energy_full": "16000000",
            "/sys/class/power_supply/BAT0/energy_now": "16000000",
            "/sys/class/power_supply/BAT0/capacity": "40",
        }

        def fake_open(path, *args, **kwargs):
            path = str(path).replace("\\", "/")
            if path not in files:
                raise OSError(path)
            return io.StringIO(files[path])

        with mock.patch.object(hw.os.path, "isdir", return_value=True), \
             mock.patch.object(hw.os, "listdir", return_value=["BAT0"]), \
             mock.patch("builtins.open", side_effect=fake_open), \
             mock.patch.object(hw, "_run", return_value=""):
            battery = hw.detect_battery()["batteries"][0]

        self.assertEqual(battery["design_capacity_mwh"], "50000")
        self.assertEqual(battery["current_capacity_mwh"], "40000")
        self.assertEqual(battery["health"], "80%")

    def test_battery_health_prefers_remaining_percent_when_full_value_drifts(self):
        files = {
            "/sys/class/power_supply/BAT0/energy_full_design": "50000000",
            "/sys/class/power_supply/BAT0/energy_full": "36000000",
            "/sys/class/power_supply/BAT0/energy_now": "18400000",
            "/sys/class/power_supply/BAT0/capacity": "40",
        }

        def fake_open(path, *args, **kwargs):
            path = str(path).replace("\\", "/")
            if path not in files:
                raise OSError(path)
            return io.StringIO(files[path])

        with mock.patch.object(hw.os.path, "isdir", return_value=True), \
             mock.patch.object(hw.os, "listdir", return_value=["BAT0"]), \
             mock.patch("builtins.open", side_effect=fake_open), \
             mock.patch.object(hw, "_run", return_value=""):
            battery = hw.detect_battery()["batteries"][0]

        self.assertEqual(battery["design_capacity_mwh"], "50000")
        self.assertEqual(battery["current_capacity_mwh"], "36000")
        self.assertEqual(battery["health"], "72%")

    def test_battery_health_cache_keeps_same_laptop_constant(self):
        original_cache = os.environ.get("VSTL_BATTERY_HEALTH_CACHE")
        first_files = {
            "/sys/class/power_supply/BAT0/energy_full_design": "50000000",
            "/sys/class/power_supply/BAT0/energy_full": "46200000",
            "/sys/class/power_supply/BAT0/energy_now": "46200000",
            "/sys/class/power_supply/BAT0/capacity": "100",
            "/sys/class/power_supply/BAT0/manufacturer": "HP",
            "/sys/class/power_supply/BAT0/model_name": "Primary",
            "/sys/class/power_supply/BAT0/serial_number": "BAT123",
        }
        second_files = dict(first_files)
        second_files.update({
            "/sys/class/power_supply/BAT0/energy_full": "36000000",
            "/sys/class/power_supply/BAT0/energy_now": "18400000",
            "/sys/class/power_supply/BAT0/capacity": "40",
        })

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                os.environ["VSTL_BATTERY_HEALTH_CACHE"] = os.path.join(tmpdir, "battery.json")
                real_open = open
                real_isdir = hw.os.path.isdir

                def run_with(files):
                    def fake_open(path, *args, **kwargs):
                        normalized = str(path).replace("\\", "/")
                        if normalized in files:
                            return io.StringIO(files[normalized])
                        return real_open(path, *args, **kwargs)

                    def fake_isdir(path):
                        normalized = str(path).replace("\\", "/")
                        return normalized == "/sys/class/power_supply" or real_isdir(path)

                    with mock.patch.object(hw.os.path, "isdir", side_effect=fake_isdir), \
                         mock.patch.object(hw.os, "listdir", return_value=["BAT0"]), \
                         mock.patch("builtins.open", side_effect=fake_open), \
                         mock.patch.object(hw, "_run", return_value=""):
                        return hw.detect_battery()["batteries"][0]

                first = run_with(first_files)
                second = run_with(second_files)
        finally:
            if original_cache is None:
                os.environ.pop("VSTL_BATTERY_HEALTH_CACHE", None)
            else:
                os.environ["VSTL_BATTERY_HEALTH_CACHE"] = original_cache

        self.assertEqual(first["current_capacity_mwh"], "46200")
        self.assertEqual(first["health"], "92%")
        self.assertEqual(second["current_capacity_mwh"], "36000")
        self.assertEqual(second["health"], "72%")

    def test_battery_health_matches_batteryinfoview_floor_integer(self):
        files = {
            "/sys/class/power_supply/BAT0/energy_full_design": "67997000",
            "/sys/class/power_supply/BAT0/energy_full": "53952000",
            "/sys/class/power_supply/BAT0/energy_now": "53952000",
            "/sys/class/power_supply/BAT0/capacity": "100",
            "/sys/class/power_supply/BAT0/manufacturer": "SMP",
            "/sys/class/power_supply/BAT0/model_name": "DELL GD1JP65",
            "/sys/class/power_supply/BAT0/serial_number": "2963",
        }

        def fake_open(path, *args, **kwargs):
            path = str(path).replace("\\", "/")
            if path not in files:
                raise OSError(path)
            return io.StringIO(files[path])

        with mock.patch.object(hw.os.path, "isdir", return_value=True), \
             mock.patch.object(hw.os, "listdir", return_value=["BAT0"]), \
             mock.patch("builtins.open", side_effect=fake_open), \
             mock.patch.object(hw, "_run", return_value=""):
            battery = hw.detect_battery()["batteries"][0]

        self.assertEqual(battery["design_capacity_mwh"], "67997")
        self.assertEqual(battery["full_charged_capacity_mwh"], "53952")
        self.assertEqual(battery["current_capacity_value_mwh"], "53952")
        self.assertEqual(battery["health"], "79%")

    def test_battery_health_uses_charge_ratio_not_live_voltage(self):
        files = {
            "/sys/class/power_supply/BAT0/charge_full_design": "100000",
            "/sys/class/power_supply/BAT0/charge_full": "79399",
            "/sys/class/power_supply/BAT0/charge_now": "79399",
            "/sys/class/power_supply/BAT0/capacity": "100",
            "/sys/class/power_supply/BAT0/voltage_min_design": "10000000",
            "/sys/class/power_supply/BAT0/voltage_now": "10880000",
            "/sys/class/power_supply/BAT0/manufacturer": "SMP",
            "/sys/class/power_supply/BAT0/model_name": "DELL GD1JP65",
            "/sys/class/power_supply/BAT0/serial_number": "2963",
        }

        def fake_open(path, *args, **kwargs):
            path = str(path).replace("\\", "/")
            if path not in files:
                raise OSError(path)
            return io.StringIO(files[path])

        with mock.patch.object(hw.os.path, "isdir", return_value=True), \
             mock.patch.object(hw.os, "listdir", return_value=["BAT0"]), \
             mock.patch("builtins.open", side_effect=fake_open), \
             mock.patch.object(hw, "_run", return_value=""):
            battery = hw.detect_battery()["batteries"][0]

        self.assertEqual(battery["design_capacity_mwh"], "1000")
        self.assertEqual(battery["full_charged_capacity_mwh"], "794")
        self.assertEqual(battery["health"], "79%")

    def test_reliable_reported_full_charge_replaces_old_higher_cache(self):
        original_cache = os.environ.get("VSTL_BATTERY_HEALTH_CACHE")
        files = {
            "/sys/class/power_supply/BAT0/energy_full_design": "67997000",
            "/sys/class/power_supply/BAT0/energy_full": "53952000",
            "/sys/class/power_supply/BAT0/energy_now": "53952000",
            "/sys/class/power_supply/BAT0/capacity": "100",
            "/sys/class/power_supply/BAT0/manufacturer": "SMP",
            "/sys/class/power_supply/BAT0/model_name": "DELL GD1JP65",
            "/sys/class/power_supply/BAT0/serial_number": "2963",
        }
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                cache_path = os.path.join(tmpdir, "battery.json")
                os.environ["VSTL_BATTERY_HEALTH_CACHE"] = cache_path
                with open(cache_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "SMP|DELL GD1JP65|2963|67997|BAT0": {
                            "full_charged_capacity_mwh": 67997,
                            "design_capacity_mwh": "67997",
                        }
                    }))

                real_open = open

                def fake_open(path, *args, **kwargs):
                    normalized = str(path).replace("\\", "/")
                    if normalized in files:
                        return io.StringIO(files[normalized])
                    return real_open(path, *args, **kwargs)

                with mock.patch.object(hw.os.path, "isdir", return_value=True), \
                     mock.patch.object(hw.os, "listdir", return_value=["BAT0"]), \
                     mock.patch("builtins.open", side_effect=fake_open), \
                     mock.patch.object(hw, "_run", return_value=""):
                    battery = hw.detect_battery()["batteries"][0]
        finally:
            if original_cache is None:
                os.environ.pop("VSTL_BATTERY_HEALTH_CACHE", None)
            else:
                os.environ["VSTL_BATTERY_HEALTH_CACHE"] = original_cache

        self.assertEqual(battery["current_capacity_mwh"], "53952")
        self.assertEqual(battery["health"], "79%")

    def test_system_board_ct_comes_from_explicit_asset_tag(self):
        board = """
Base Board Information
    Manufacturer: HP
    Product Name: 8B41
    Serial Number: PGBVC00WB5K0C9
    Asset Tag: 6MBME0NWYIKIDV
"""
        with mock.patch.object(hw.os, "geteuid", return_value=0, create=True), \
             mock.patch.object(hw, "_run", side_effect=[board, ""]):
            result = hw.detect_system_board()
        self.assertEqual(result["ct_number"], "6MBME0NWYIKIDV")


if __name__ == "__main__":
    unittest.main()
