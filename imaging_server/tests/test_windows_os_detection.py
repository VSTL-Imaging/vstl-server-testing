import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CAPTURE_PATH = ROOT / "bench-client" / "vstl_image_capture.py"
spec = importlib.util.spec_from_file_location("vstl_image_capture", CAPTURE_PATH)
ic = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(ic)


class WindowsOsDetectionTests(unittest.TestCase):
    def test_product_name_pro_wins_over_loose_enterprise_edition_hit(self):
        self.assertEqual(
            ic._normalize_windows_product("Windows 10 Pro", "Enterprise", "26200"),
            "Windows 11 Pro",
        )
        self.assertEqual(
            ic._normalize_windows_product("Windows 11 Pro", "Enterprise", "26200"),
            "Windows 11 Pro",
        )

    def test_generic_product_uses_professional_edition(self):
        self.assertEqual(
            ic._normalize_windows_product("Windows 10", "Professional", "26200"),
            "Windows 11 Pro",
        )

    def test_exact_registry_edition_overrides_conflicting_product_name(self):
        self.assertEqual(
            ic._normalize_windows_product(
                "Windows 10 Enterprise",
                "Professional",
                "26200",
                prefer_edition=True,
            ),
            "Windows 11 Pro",
        )

    def test_oem_pro_product_id_overrides_conflicting_enterprise_name(self):
        self.assertEqual(
            ic._normalize_windows_product(
                "Windows 10 Enterprise",
                "Enterprise",
                "26200",
                product_id="00355-62989-47148-AAOEM",
                prefer_edition=True,
            ),
            "Windows 11 Pro",
        )

    def test_reged_export_parser_reads_string_values(self):
        values = ic._parse_reg_export_values(
            r'''
Windows Registry Editor Version 5.00

[HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion]
"ProductName"="Windows 10 Enterprise"
"EditionID"="Professional"
"DisplayVersion"="25H2"
"CurrentBuild"="26200"
'''
        )
        self.assertEqual(values["ProductName"], "Windows 10 Enterprise")
        self.assertEqual(values["EditionID"], "Professional")

    def test_reged_export_parser_reads_hex_utf16_values(self):
        values = ic._parse_reg_export_values(
            r'''
Windows Registry Editor Version 5.00

[HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion]
"ProductName"=hex(1):57,00,69,00,6e,00,64,00,6f,00,77,00,73,00,20,00,31,00,\
  30,00,20,00,45,00,6e,00,74,00,65,00,72,00,70,00,72,00,69,00,73,00,65,00,\
  00,00
"ProductId"=hex(1):30,00,30,00,33,00,35,00,35,00,2d,00,36,00,32,00,39,00,\
  38,00,39,00,2d,00,34,00,37,00,31,00,34,00,38,00,2d,00,41,00,41,00,4f,00,\
  45,00,4d,00,00,00
'''
        )
        self.assertEqual(values["ProductName"], "Windows 10 Enterprise")
        self.assertEqual(values["ProductId"], "00355-62989-47148-AAOEM")

    def test_reged_export_parser_can_limit_to_currentversion_section(self):
        values = ic._parse_reg_export_values(
            r'''
Windows Registry Editor Version 5.00

[HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion]
"CompositionEditionID"="Enterprise"
"CurrentBuild"="26200"
"DisplayVersion"="25H2"
"EditionID"="Professional"
"ProductName"="Windows 10 Pro"
"ProductId"="00355-62989-47148-AAOEM"

[HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags]
"ProductName"="Windows 10 Enterprise"
''',
            section=r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        )
        self.assertEqual(values["ProductName"], "Windows 10 Pro")
        self.assertEqual(values["EditionID"], "Professional")
        self.assertEqual(values["ProductId"], "00355-62989-47148-AAOEM")

    def test_capture_plan_uses_win_11_pro_token_with_uppercase_feature_release(self):
        plan = ic.build_capture_image_plan(
            "Dell Inc.",
            "Latitude 5530",
            "0B06",
            "12th Gen Intel Core i7-1265U",
            {"os_name": "Windows 11 Pro", "os_version": "25h2", "os_build": "26200"},
        )
        self.assertEqual(plan["os_name"], "Windows 11 Pro")
        self.assertEqual(plan["os_version"], "25H2")
        self.assertEqual(plan["os_token"], "Win_11_Pro_25H2")
        self.assertNotIn("Enterprise", plan["image_subdir"])


if __name__ == "__main__":
    unittest.main()
