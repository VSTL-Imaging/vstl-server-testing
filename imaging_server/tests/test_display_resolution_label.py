import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


qc = _load_module("vstl_qc_tests", "bench-client/vstl_qc_tests.py")


class DisplayResolutionLabelTests(unittest.TestCase):
    def test_formats_fhd_resolution(self):
        self.assertEqual(qc._format_display_resolution(1920, 1080), "1920 x 1080 (FHD)")

    def test_formats_hd_and_wuxga_resolutions(self):
        self.assertEqual(qc._format_display_resolution(1366, 768), "1366 x 768 (HD)")
        self.assertEqual(qc._format_display_resolution(1920, 1200), "1920 x 1200 (WUXGA)")

    def test_parse_common_sysfs_modes(self):
        self.assertEqual(qc._parse_resolution_text("1920,1080"), (1920, 1080))
        self.assertEqual(qc._parse_resolution_text("U:1920x1080p-60"), (1920, 1080))


if __name__ == "__main__":
    unittest.main()
