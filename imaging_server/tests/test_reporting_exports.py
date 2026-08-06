import importlib.util
import json
import os
import zipfile
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
EXPORTER_PATH = ROOT / "reporting" / "export_reports.py"
IMPORTER_PATH = ROOT / "reporting" / "import_secure_erase_records.py"
CAPTURE_IMPORTER_PATH = ROOT / "reporting" / "import_capture_records.py"
INGEST_PHP = (ROOT / "reporting" / "ingest.php").read_text(encoding="utf-8")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


exporter = _load_module("vstl_reporting_exporter", EXPORTER_PATH)
importer = _load_module("vstl_reporting_importer", IMPORTER_PATH)
capture_importer = _load_module("vstl_reporting_capture_importer", CAPTURE_IMPORTER_PATH)


def _sample_payload():
    return {
        "session_started_at": "2026-06-14T10:11:12+00:00",
        "technician_level": "L1",
        "technician_user_name": "Rahul Operator",
        "bench_id": "BENCH-01",
        "audit_submission_status": "Audit Submitted",
        "serial_no": "SER123",
        "lot_no": "LOT-2206",
        "box_no": "BOX NO 01",
        "sku": "SKU456",
        "mac_id": "AA:BB:CC:DD:EE:FF",
        "secure_erase_reg_id": "SE-REG-001",
        "model": "Latitude Test",
        "cpu": "Intel Test CPU",
        "ram": "(2x16GB) 32GB",
        "bios_version": "1.2.3",
        "installed_os": "Windows 11 Pro",
        "os_version": "25H2",
        "os_license_key": "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE",
        "display": {
            "resolution": "1920 x 1080",
            "type": "Touch",
            "result": "PASS",
            "remarks": "",
        },
        "keyboard_type": "US QWERTY WITH BACK LIGHT",
        "keyboard_language": "US",
        "burn_test": {
            "result": "PASS",
            "duration_sec": "900",
            "actual_duration_sec": "902",
            "max_temp_c": "82",
            "fan_status": "OK",
            "fan_min_rpm": "2100",
            "fan_max_rpm": "3900",
            "fan_avg_rpm": "3150",
            "remarks": "stable",
        },
        "qc_elapsed_sec": "971",
        "qc_tests": {
            "failed": False,
            "tests": {
                "keyboard": {"result": "PASS"},
                "camera": {"result": "PASS"},
                "speaker": {"result": "PASS"},
                "microphone": {"result": "PASS"},
                "fingerprint": {"result": "PASS", "evidence": "fprintd detected finger"},
                "driver_preflight": {
                    "result": "PASS",
                    "evidence": "model=Dell Latitude 5530; audio=ready; fingerprint_linux=yes",
                    "remarks": "Linux drivers ready",
                },
                "wireless": {
                    "result": "PASS",
                    "wifi_status": "PASS",
                    "bluetooth_status": "PASS",
                },
                "ports": {"result": "PASS", "evidence": "USB Type-A #1"},
            },
        },
        "phase3": {"capture": {"ok": True, "duration_sec": 123}},
        "lock_audit": {"detected_locks": []},
        "raw_data": {
            "box_scope": {
                "lot_no": "LOT-2206",
                "box_no": "BOX NO 01",
                "total": 20,
                "imaged": 7,
                "remaining": 13,
                "brand": "HP",
                "model": "HP EliteBook 640 14 inch G9",
                "model_label": "HP EliteBook 640 14 inch G9",
                "model_count": 1,
            },
            "gpu": {
                "integrated_gpu": "Intel Iris Xe",
                "discrete_gpu": "NVIDIA RTX",
                "discrete_gpu_memory": "DEDICATED 4 GB",
            },
            "ram": {
                "module_count": 2,
                "modules": [
                    {
                        "vendor": "Vendor A",
                        "model_description": "DDR5",
                        "ram_type": "PC5",
                        "serial_number": "RAM1",
                        "product_number": "HMAA2GS6CJR8N-XN",
                        "ct_number": "RAMCT123456",
                        "part_number": "L67710-001",
                    },
                    {
                        "vendor": "Vendor B",
                        "model_description": "DDR5",
                        "ram_type": "PC5",
                        "serial_number": "RAM2",
                        "product_number": "HMAA2GS6CJR8N-XN",
                        "ct_number": "RAMCT654321",
                        "part_number": "L67710-001",
                    },
                ],
            },
            "storage": {
                "drive_count": 1,
                "drives": [{
                    "size": "512 GB",
                    "health": "98%",
                    "vendor": "Storage Vendor",
                    "model_description": "NVMe Model",
                    "storage_type": "NVME",
                    "serial_number": "SSD1",
                    "product_number": "SK hynix BC901 HFS256GEJ9X108N",
                    "ct_number": "SSDCT123456",
                    "part_number": "N45477-001",
                }],
            },
            "battery": {
                "battery_count": 1,
                "batteries": [{
                    "health": "95%",
                    "design_capacity_mwh": "51000",
                    "full_charged_capacity_mwh": "48450",
                    "current_capacity_mwh": "48450",
                    "current_capacity_value_mwh": "48000",
                    "cycle_count": "42",
                    "vendor": "Battery Vendor",
                    "model_description": "Battery Model",
                    "serial_number": "BAT1",
                    "ct_number": "6MBME0NWYIKIDV",
                    "part_number": "M73472-005",
                }],
            },
            "system_board": {
                "ct_number": "BOARDCT123456",
            },
            "bios": {"version": "1.2.3"},
        },
    }


def test_flatten_contains_requested_hardware_and_qc_fields():
    row = exporter.flatten({"received_at": "2026-06-14T10:11:12Z", "payload": _sample_payload()})
    assert row["Operation"] == "QC; Capture"
    assert row["User"] == "Rahul Operator"
    assert row["Audit Submission Status"] == "Audit Submitted"
    assert row["Lot Number"] == "LOT-2206"
    assert row["Box Number"] == "BOX NO 01"
    assert row["Box Model"] == "HP EliteBook 640 14 inch G9"
    assert row["Box Total Units"] == "20"
    assert row["Box Imaged Units"] == "7"
    assert row["Box Remaining Units"] == "13"
    assert row["Installed OS"] == "Windows 11 Pro"
    assert row["Secure Erase Reg ID"] == "SE-REG-001"
    assert row["Display Resolution (Short)"] == "FHD"
    assert row["Integrated GPU Name"] == "Intel Iris Xe"
    assert row["Number of RAM Modules"] == "2"
    assert row["RAM Serial Number"] == "RAM1; RAM2"
    assert row["RAM Type"] == "PC5; PC5"
    assert row["RAM CT Number"] == "RAMCT123456; RAMCT654321"
    assert row["RAM Part Number"] == "L67710-001; L67710-001"
    assert row["Storage Health (%)"] == "98%"
    assert row["Storage Type"] == "NVME"
    assert row["Storage CT Number"] == "SSDCT123456"
    assert row["Storage Part Number"] == "N45477-001"
    assert row["Battery CT Number"] == "6MBME0NWYIKIDV"
    assert row["Battery Part Number"] == "M73472-005"
    assert row["Battery Designed Capacity (mWh)"] == "51000"
    assert row["Battery Full Charged Capacity (mWh)"] == "48450"
    assert row["Battery Current Capacity (mWh)"] == "48000"
    assert row["Battery Cycle Count"] == "42"
    assert row["System Board CT Number"] == "BOARDCT123456"
    assert row["Burn Stress Test Result"] == "PASS"
    assert row["Burn Stress Test Duration (sec)"] == "900"
    assert row["CPU Fan Status"] == "OK"
    assert row["CPU Fan Min RPM"] == "2100"
    assert row["CPU Fan Max RPM"] == "3900"
    assert row["CPU Fan Average RPM"] == "3150"
    assert row["QC Elapsed Time (sec)"] == "971"
    assert row["Capture Elapsed Time (sec)"] == "123"
    assert row["Wi-Fi Status"] == "PASS"
    assert row["Bluetooth Status"] == "PASS"
    assert row["Fingerprint Status"] == "PASS"
    assert row["Driver Preflight Status"] == "PASS"
    assert "fingerprint_linux=yes" in row["Driver Preflight Evidence"]
    assert row["Driver Preflight Remarks"] == "Linux drivers ready"
    assert row["Keyboard Language"] == "US"


def test_keyboard_language_falls_back_to_profile_and_raw_keyboard():
    payload = _sample_payload()
    payload.pop("keyboard_language", None)
    payload["keyboard_profile"] = {"print_format": "POLISH"}
    row = exporter.flatten({"payload": payload})
    assert row["Keyboard Language"] == "POLISH"

    payload.pop("keyboard_profile", None)
    payload["raw_data"]["keyboard"] = {"print_format": "CANADIAN FRENCH"}
    row = exporter.flatten({"payload": payload})
    assert row["Keyboard Language"] == "CANADIAN FRENCH"

    payload["raw_data"].pop("keyboard", None)
    payload["keyboard_type"] = "POLISH QWERTY WITH BACK LIGHT"
    row = exporter.flatten({"payload": payload})
    assert row["Keyboard Language"] == "POLISH"

    payload["keyboard_type"] = "US QWERTY WITH ARABIC PRINT WITH BACK LIGHT"
    row = exporter.flatten({"payload": payload})
    assert row["Keyboard Language"] == "US WITH ARABIC PRINT"


def test_flatten_normalizes_audit_submission_status():
    payload = _sample_payload()
    payload["audit_submission_status"] = "operator session expired; audit queued safely - login again to send"

    row = exporter.flatten({"payload": payload})

    assert row["Audit Submission Status"] == "Submission Failed"

    payload = _sample_payload()
    payload.pop("audit_submission_status", None)
    payload["cloud_audit"] = {"ok": False}

    row = exporter.flatten({"payload": payload})

    assert row["Audit Submission Status"] == "Submission Failed"

    payload["cloud_audit"] = {"ok": True}

    row = exporter.flatten({"payload": payload})

    assert row["Audit Submission Status"] == "Audit Submitted"


def test_flatten_corrects_lenovo_dmi_model_and_sku_swap():
    payload = _sample_payload()
    payload["brand"] = "LENOVO"
    payload["model"] = "20WLS1G400"
    payload["sku"] = "LENOVO_MT_20WL_BU_Think_FM_ThinkPad X13 Gen 2i"

    row = exporter.flatten({"payload": payload})

    assert row["SKU / Product Number"] == "20WLS1G400"
    assert row["Model Name"] == "ThinkPad X13 Gen 2i"


def test_flatten_keeps_non_lenovo_model_and_sku_unchanged():
    payload = _sample_payload()
    payload["brand"] = "Dell Inc."
    payload["model"] = "Latitude 5530"
    payload["sku"] = "0B06"

    row = exporter.flatten({"payload": payload})

    assert row["SKU / Product Number"] == "0B06"
    assert row["Model Name"] == "Latitude 5530"


def test_secure_erase_reg_id_falls_back_to_phase3_certificate_id():
    payload = _sample_payload()
    payload.pop("secure_erase_reg_id")
    payload["phase3"] = {
        "erase": {
            "ok": True,
            "certificate_id": "SE-CERT-PHASE3-009",
        }
    }
    row = exporter.flatten({"payload": payload})
    assert row["Secure Erase Reg ID"] == "SE-CERT-PHASE3-009"


def test_operation_elapsed_time_maps_to_each_operation_sheet():
    payload = _sample_payload()
    payload["phase3"] = {
        "restore": {"ok": True, "duration_sec": 240},
        "erase": {"ok": True, "duration_sec": 31},
        "capture": {"ok": True, "duration_sec": 480},
    }
    row = exporter.flatten({"payload": payload})

    assert row["QC Elapsed Time (sec)"] == "971"
    assert row["Restore Elapsed Time (sec)"] == "240"
    assert row["Secure Erase Elapsed Time (sec)"] == "31"
    assert row["Capture Elapsed Time (sec)"] == "480"

    sheets = dict(exporter._xlsx_sheets([row], "all"))
    assert sheets["QC"][0]["Operation Elapsed Time (sec)"] == "971"
    assert sheets["Restore"][0]["Operation Elapsed Time (sec)"] == "240"
    assert sheets["Secure Erase"][0]["Operation Elapsed Time (sec)"] == "31"
    assert sheets["Capture"][0]["Operation Elapsed Time (sec)"] == "480"


def test_legacy_burn_fan_text_is_exported_to_cpu_fan_columns():
    payload = _sample_payload()
    payload["burn_test"] = {
        "result": "PASS",
        "duration_sec": 60,
        "max_temp_c": 68,
        "remarks": "cpu_stress_60s: PASSED; fan_status: NOT SPINNING; fan_max_rpm: 0; ram_test: PASSED",
    }

    row = exporter.flatten({"payload": payload})

    assert row["CPU Fan Status"] == "NOT SPINNING"
    assert row["CPU Fan Max RPM"] == "0"


def test_reporting_ingest_backfills_certificate_for_successful_erase_audits():
    assert "function vstl_issue_local_secure_erase_certificate" in INGEST_PHP
    assert "'vstl_secure_erase_report_certificate_v1'" in INGEST_PHP
    assert "'SE-' . vstl_date_token" in INGEST_PHP
    assert "'cloud certificate response was missing; issued by reporting ingest'" in INGEST_PHP
    assert "$payload['secure_erase_reg_id'] = $certificateId;" in INGEST_PHP
    assert "'NVMe_SANITIZE_OVERWRITE' => 'NIST SP 800-88 Purge'" in INGEST_PHP
    assert "'NVMe_FORMAT_USER_DATA' => 'NIST SP 800-88 Clear'" not in INGEST_PHP
    assert "function vstl_is_certifiable_wipe_method" in INGEST_PHP
    assert "Clear-class and unknown wipe methods are disabled" in INGEST_PHP
    assert "$erase['certificate_status'] = 'refused';" in INGEST_PHP


def test_csv_and_xlsx_exports_are_created(tmp_path):
    data = tmp_path / "audits.jsonl"
    data.write_text(json.dumps({"payload": _sample_payload()}) + "\n", encoding="utf-8")
    rows = exporter.load_rows(data, "capture")
    assert len(rows) == 1

    csv_path = tmp_path / "report.csv"
    xlsx_path = tmp_path / "report.xlsx"
    exporter.write_csv(rows, csv_path, "capture")
    exporter.write_xlsx(rows, xlsx_path, "all")

    csv_text = csv_path.read_text(encoding="utf-8-sig")
    assert "Serial Number" in csv_text
    assert "Captured OS" in csv_text
    assert "Installed OS" not in csv_text
    assert "Display Resolution" not in csv_text
    assert "Secure Erase Reg ID" not in csv_text
    with zipfile.ZipFile(xlsx_path) as archive:
        workbook = archive.read("xl/workbook.xml").decode()
        assert 'name="Restore"' in workbook
        assert 'name="QC"' in workbook
        assert 'name="Secure Erase"' in workbook
        assert 'name="Capture"' in workbook
        assert "Windows 11 Pro" not in archive.read("xl/worksheets/sheet1.xml").decode()
        qc_sheet = archive.read("xl/worksheets/sheet2.xml").decode()
        assert "Windows 11 Pro" in qc_sheet
        assert "<t>QC</t>" in qc_sheet
        assert "<t>Current OS</t>" in qc_sheet
        assert "<t>Installed OS</t>" not in qc_sheet
        assert "Windows 11 Pro" not in archive.read("xl/worksheets/sheet3.xml").decode()
        capture_sheet = archive.read("xl/worksheets/sheet4.xml").decode()
        assert "Windows 11 Pro" in capture_sheet
        assert "<t>Capture</t>" in capture_sheet
        assert "<t>Captured OS</t>" in capture_sheet
        assert "<t>Installed OS</t>" not in capture_sheet
        assert "<t>Secure Erase Reg ID</t>" not in capture_sheet
        assert "<t>Display Resolution</t>" not in capture_sheet
        assert "<t>Keyboard Language</t>" not in capture_sheet
        assert "<t>Keyboard Status</t>" not in capture_sheet
        assert "<t>Fingerprint Status</t>" not in capture_sheet
        assert "<t>Burn Stress Test Result</t>" not in capture_sheet
        assert "<t>CPU Fan Status</t>" not in capture_sheet

        namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        styles = ElementTree.fromstring(archive.read("xl/styles.xml"))
        header_style = styles.find("x:cellXfs", namespace)[1]
        alignment = header_style.find("x:alignment", namespace)
        assert alignment.attrib["horizontal"] == "center"
        assert alignment.attrib["vertical"] == "center"
        assert alignment.attrib["wrapText"] == "0"
        styles_xml = archive.read("xl/styles.xml").decode()
        assert '<left style="medium"><color rgb="FF000000"/></left>' in styles_xml
        assert '<left style="thin"><color rgb="FF000000"/></left>' in styles_xml
        assert 'wrapText="1"' not in styles_xml
        assert 'bestFit="1"' in qc_sheet
        assert 'state="frozen"' in qc_sheet

        borders = styles.find("x:borders", namespace)
        assert borders.attrib["count"] == "3"
        cell_xfs = styles.find("x:cellXfs", namespace)
        assert cell_xfs[1].attrib["borderId"] == "1"
        assert cell_xfs[2].attrib["borderId"] == "2"
        assert cell_xfs[3].attrib["borderId"] == "2"

        os_license_col = exporter._column_name(exporter.HEADERS.index("OS License Key") + 1)
        model_col = exporter._column_name(exporter.HEADERS.index("Model Name") + 1)
        ram_type_col = exporter._column_name(exporter.HEADERS.index("RAM Type") + 1)
        storage_type_col = exporter._column_name(exporter.HEADERS.index("Storage Type") + 1)
        battery_design_col = exporter._column_name(exporter.HEADERS.index("Battery Designed Capacity (mWh)") + 1)
        battery_full_col = exporter._column_name(exporter.HEADERS.index("Battery Full Charged Capacity (mWh)") + 1)
        battery_current_col = exporter._column_name(exporter.HEADERS.index("Battery Current Capacity (mWh)") + 1)
        battery_cycle_col = exporter._column_name(exporter.HEADERS.index("Battery Cycle Count") + 1)
        board_ct_col = exporter._column_name(exporter.HEADERS.index("System Board CT Number") + 1)
        assert f'<c r="{os_license_col}2" t="inlineStr" s="2">' in qc_sheet
        assert f'<c r="{model_col}2" t="inlineStr" s="3">' in qc_sheet
        assert f'<c r="{ram_type_col}2" t="inlineStr" s="2">' in qc_sheet
        assert f'<c r="{storage_type_col}2" t="inlineStr" s="2">' in qc_sheet
        assert f'<c r="{battery_design_col}2" t="inlineStr" s="2">' in qc_sheet
        assert f'<c r="{battery_full_col}2" t="inlineStr" s="2">' in qc_sheet
        assert f'<c r="{battery_current_col}2" t="inlineStr" s="2">' in qc_sheet
        assert f'<c r="{battery_cycle_col}2" t="inlineStr" s="2">' in qc_sheet
        assert f'<c r="{board_ct_col}2" t="inlineStr" s="2">' in qc_sheet
        qc_headers = exporter._sheet_headers("QC")
        assert qc_sheet.count('t="inlineStr" s="1"') == len(qc_headers)
        assert qc_sheet.count('t="inlineStr" s="2"') + qc_sheet.count(
            't="inlineStr" s="3"'
        ) == len(qc_headers)


def test_reports_are_newest_first_in_every_operation_sheet(tmp_path):
    def payload_for(serial: str):
        payload = _sample_payload()
        payload["serial_no"] = serial
        payload["secure_erase_reg_id"] = f"SE-{serial}"
        payload["phase3"] = {
            "restore": {"ok": True, "duration_sec": 11},
            "erase": {"ok": True, "duration_sec": 12},
            "capture": {"ok": True, "duration_sec": 13},
        }
        return payload

    data = tmp_path / "audits.jsonl"
    records = [
        {"received_at": "2026-06-14T10:00:00Z", "payload": payload_for("OLD")},
        {"received_at": "2026-06-14T10:02:00Z", "payload": payload_for("NEW")},
        {"received_at": "2026-06-14T10:01:00Z", "payload": payload_for("MID")},
    ]
    data.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    rows = exporter.load_rows(data, "all")

    assert [row["Serial Number"] for row in rows] == ["NEW", "MID", "OLD"]
    sheets = dict(exporter._xlsx_sheets(rows, "all"))
    for sheet_name in ("Restore", "QC", "Secure Erase", "Capture"):
        assert [row["Serial Number"] for row in sheets[sheet_name]] == [
            "NEW",
            "MID",
            "OLD",
        ]


def test_headers_use_ct_number_and_single_csv_field_order():
    assert "Battery CT Number" in exporter.HEADERS
    assert "Battery Product Number" not in exporter.HEADERS
    assert "RAM CT Number" in exporter.HEADERS
    assert "RAM Product Number" not in exporter.HEADERS
    assert "Storage CT Number" in exporter.HEADERS
    assert "Storage Product Number" not in exporter.HEADERS
    assert "Number of Entries" in exporter.HEADERS
    assert "User" in exporter.HEADERS
    assert "Audit Submission Status" in exporter.HEADERS
    assert "Audit Submission Status" in exporter.CENTER_VALUE_HEADERS
    assert "Lot Number" in exporter.HEADERS
    assert "Box Number" in exporter.HEADERS
    assert "Box Model" in exporter.HEADERS
    assert "Box Total Units" in exporter.HEADERS
    assert "Box Imaged Units" in exporter.HEADERS
    assert "Box Remaining Units" in exporter.HEADERS
    assert "Box Number" in exporter.CENTER_VALUE_HEADERS
    assert "Box Remaining Units" in exporter.CENTER_VALUE_HEADERS
    assert "System Board CT Number" in exporter.HEADERS
    assert "Battery Cycle Count" in exporter.HEADERS
    assert "RAM Type" in exporter.HEADERS
    assert "Storage Type" in exporter.HEADERS
    assert "Battery Designed Capacity (mWh)" in exporter.HEADERS
    assert "Battery Full Charged Capacity (mWh)" in exporter.HEADERS
    assert "Battery Current Capacity (mWh)" in exporter.HEADERS
    assert "Secure Erase Reg ID" in exporter.HEADERS
    assert "Secure Erase Reg ID" in exporter.CENTER_VALUE_HEADERS
    assert "Fingerprint Status" in exporter.HEADERS
    assert "Fingerprint Status" in exporter.CENTER_VALUE_HEADERS
    assert "Keyboard Language" in exporter.HEADERS
    assert "Keyboard Language" in exporter.CENTER_VALUE_HEADERS
    assert "Operation Elapsed Time (sec)" in exporter.HEADERS
    assert "QC Elapsed Time (sec)" in exporter.HEADERS
    assert "Restore Elapsed Time (sec)" in exporter.HEADERS
    assert "Secure Erase Elapsed Time (sec)" in exporter.HEADERS
    assert "Capture Elapsed Time (sec)" in exporter.HEADERS
    assert "CPU Fan Status" in exporter.HEADERS
    assert "CPU Fan Max RPM" in exporter.HEADERS
    assert "Operation Elapsed Time (sec)" in exporter.CENTER_VALUE_HEADERS
    assert "CPU Fan Status" in exporter.CENTER_VALUE_HEADERS
    assert exporter.HEADERS.index("RAM Type") == exporter.HEADERS.index("Total RAM (GB)") + 1
    assert exporter.HEADERS.index("Storage Type") == exporter.HEADERS.index("Storage Capacity (GB)") + 1
    assert exporter.HEADERS.index("Battery Designed Capacity (mWh)") == exporter.HEADERS.index("Battery Health (%)") + 1
    assert exporter.HEADERS.index("Battery Full Charged Capacity (mWh)") == exporter.HEADERS.index("Battery Designed Capacity (mWh)") + 1
    assert exporter.HEADERS.index("Battery Current Capacity (mWh)") == exporter.HEADERS.index("Battery Full Charged Capacity (mWh)") + 1
    assert exporter.HEADERS.index("Battery Cycle Count") == exporter.HEADERS.index("Battery Current Capacity (mWh)") + 1
    assert exporter.HEADERS.index("System Board CT Number") == exporter.HEADERS.index("BIOS Version") - 1
    assert exporter.HEADERS.index("Lot Number") == exporter.HEADERS.index("BIOS Lock Status") + 1
    assert exporter.HEADERS.index("Box Number") == exporter.HEADERS.index("Lot Number") + 1
    assert exporter.HEADERS.index("Box Model") == exporter.HEADERS.index("Box Number") + 1
    assert exporter.HEADERS.index("Keyboard Language") == exporter.HEADERS.index("Keyboard Type") + 1
    assert "RAM Type" in exporter.CENTER_VALUE_HEADERS
    assert "Storage Type" in exporter.CENTER_VALUE_HEADERS
    assert "Battery Designed Capacity (mWh)" in exporter.CENTER_VALUE_HEADERS
    assert "Battery Full Charged Capacity (mWh)" in exporter.CENTER_VALUE_HEADERS
    assert "Battery Current Capacity (mWh)" in exporter.CENTER_VALUE_HEADERS
    assert "Secure Erase Reg ID" in exporter._sheet_headers("Restore")
    assert "Secure Erase Reg ID" in exporter._sheet_headers("QC")
    assert "Secure Erase Reg ID" in exporter._sheet_headers("Secure Erase")
    assert "Secure Erase Reg ID" not in exporter._sheet_headers("Capture")
    assert "Fingerprint Status" in exporter._sheet_headers("Restore")
    assert "Fingerprint Status" in exporter._sheet_headers("QC")
    assert "Fingerprint Status" not in exporter._sheet_headers("Secure Erase")
    assert "Fingerprint Status" not in exporter._sheet_headers("Capture")
    assert "Driver Preflight Status" in exporter._sheet_headers("Restore")
    assert "Keyboard Language" in exporter._sheet_headers("QC")
    assert "Keyboard Language" not in exporter._sheet_headers("Secure Erase")
    assert "Keyboard Language" not in exporter._sheet_headers("Capture")
    assert "Driver Preflight Evidence" in exporter._sheet_headers("QC")
    assert "Driver Preflight Remarks" not in exporter._sheet_headers("Secure Erase")
    assert "Driver Preflight Status" not in exporter._sheet_headers("Capture")
    assert "Burn Stress Test Result" in exporter._sheet_headers("Restore")
    assert "Burn Stress Test Result" in exporter._sheet_headers("QC")
    assert "Burn Stress Test Result" not in exporter._sheet_headers("Secure Erase")
    assert "Burn Stress Test Result" not in exporter._sheet_headers("Capture")
    assert "CPU Fan Status" in exporter._sheet_headers("Restore")
    assert "CPU Fan Status" in exporter._sheet_headers("QC")
    assert "CPU Fan Status" not in exporter._sheet_headers("Secure Erase")
    assert "CPU Fan Status" not in exporter._sheet_headers("Capture")
    assert "Operation Elapsed Time (sec)" in exporter._sheet_headers("Restore")
    assert "Operation Elapsed Time (sec)" in exporter._sheet_headers("QC")
    assert "Operation Elapsed Time (sec)" in exporter._sheet_headers("Secure Erase")
    assert "Operation Elapsed Time (sec)" in exporter._sheet_headers("Capture")
    assert "Audit Submission Status" in exporter._sheet_headers("Restore")
    assert "Audit Submission Status" in exporter._sheet_headers("QC")
    assert "Audit Submission Status" in exporter._sheet_headers("Secure Erase")
    assert "Audit Submission Status" in exporter._sheet_headers("Capture")
    assert "Lot Number" in exporter._sheet_headers("Restore")
    assert "Box Number" in exporter._sheet_headers("QC")
    assert "Box Model" in exporter._sheet_headers("Secure Erase")
    assert "Box Remaining Units" in exporter._sheet_headers("Capture")


def test_part_numbers_never_fall_back_to_ct_model_or_serial():
    payload = _sample_payload()
    module = payload["raw_data"]["ram"]["modules"][0]
    drive = payload["raw_data"]["storage"]["drives"][0]
    battery = payload["raw_data"]["battery"]["batteries"][0]
    module.pop("part_number", None)
    drive.pop("part_number", None)
    battery.pop("part_number", None)

    row = exporter.flatten({"payload": payload})

    assert row["RAM Part Number"] == "L67710-001"
    assert row["Storage Part Number"] == ""
    assert row["Battery Part Number"] == ""
    assert row["Storage Part Number"] != row["Storage CT Number"]
    assert row["Battery Part Number"] != row["Battery CT Number"]


def test_ct_columns_keep_auto_product_ids_when_ct_labels_are_absent():
    payload = _sample_payload()
    module = payload["raw_data"]["ram"]["modules"][0]
    drive = payload["raw_data"]["storage"]["drives"][0]
    module["product_number"] = "HMAA2GS6CJR8N-XN"
    drive["product_number"] = "SK hynix BC901 HFS256GEJ9X108N"
    for item in payload["raw_data"]["ram"]["modules"]:
        item.pop("ct_number", None)
    for item in payload["raw_data"]["storage"]["drives"]:
        item.pop("ct_number", None)

    row = exporter.flatten({"payload": payload})

    assert row["RAM CT Number"] == "HMAA2GS6CJR8N-XN"
    assert row["Storage CT Number"] == "SK hynix BC901 HFS256GEJ9X108N"


def test_filtered_xlsx_contains_only_requested_operation_sheet(tmp_path):
    xlsx_path = tmp_path / "qc-report.xlsx"
    exporter.write_xlsx([exporter.flatten({"payload": _sample_payload()})], xlsx_path, "qc")

    with zipfile.ZipFile(xlsx_path) as archive:
        workbook = archive.read("xl/workbook.xml").decode()
        assert 'name="QC"' in workbook
        assert 'name="Restore"' not in workbook
        assert "xl/worksheets/sheet2.xml" not in archive.namelist()


def test_csv_bundle_matches_all_xlsx_operation_split(tmp_path):
    payload = _sample_payload()
    data = tmp_path / "audits.jsonl"
    data.write_text(json.dumps({"payload": payload}) + "\n", encoding="utf-8")
    rows = exporter.load_rows(data, "all")

    bundle = tmp_path / "all-csv.zip"
    exporter.write_csv_bundle(rows, bundle)

    with zipfile.ZipFile(bundle) as archive:
        assert archive.namelist() == [
            "restore.csv",
            "qc.csv",
            "secure-erase.csv",
            "capture.csv",
        ]
        assert "Windows 11 Pro" not in archive.read("restore.csv").decode("utf-8-sig")
        assert "Windows 11 Pro" in archive.read("qc.csv").decode("utf-8-sig")
        assert "Windows 11 Pro" not in archive.read("secure-erase.csv").decode("utf-8-sig")
        capture_csv = archive.read("capture.csv").decode("utf-8-sig")
        assert "Windows 11 Pro" in capture_csv
        assert "Captured OS" in capture_csv
        assert "Display Resolution" not in capture_csv
        assert "Keyboard Language" not in capture_csv
        assert "Secure Erase Reg ID" not in capture_csv
        assert "Fingerprint Status" not in capture_csv
        assert "Burn Stress Test Result" not in capture_csv
        assert "Capture" in capture_csv


def test_secure_erase_sheet_keeps_reg_id_but_excludes_qc_columns(tmp_path):
    payload = _sample_payload()
    payload["phase3"] = {
        "erase": {
            "ok": True,
            "record_id": "ERASE-42",
            "wipe_method": "ATA_SANITIZE_BLOCK_ERASE",
            "wipe_standard": "NIST SP 800-88 Purge",
            "verified": True,
            "duration_sec": 9,
            "certificate_status": "issued",
        }
    }
    row = exporter.flatten({"payload": payload})
    assert row["Secure Erase Reg ID"] == "SE-REG-001"
    assert row["Wipe Method"] == "ATA_SANITIZE_BLOCK_ERASE"
    assert row["Wipe Standard"] == "NIST SP 800-88 Purge"
    assert row["Wipe Verified"] == "Yes"
    assert row["Wipe Duration (sec)"] == "9"

    xlsx_path = tmp_path / "secure-erase-report.xlsx"
    exporter.write_xlsx([row], xlsx_path, "secure_erase")

    with zipfile.ZipFile(xlsx_path) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        assert "<t>Secure Erase Reg ID</t>" in sheet
        assert "SE-REG-001" in sheet
        assert "ATA_SANITIZE_BLOCK_ERASE" in sheet
        assert "<t>Display Resolution</t>" not in sheet
        assert "<t>Keyboard Language</t>" not in sheet
        assert "<t>Keyboard Status</t>" not in sheet
        assert "<t>Fingerprint Status</t>" not in sheet
        assert "<t>Burn Stress Test Result</t>" not in sheet
        assert "<t>CPU Fan Status</t>" not in sheet


def test_report_timestamp_uses_server_received_time_in_uae_timezone():
    payload = _sample_payload()
    payload["session_started_at"] = "2026-06-19T20:35:07+00:00"
    row = exporter.flatten({
        "received_at": "2026-06-19T17:07:27+00:00",
        "payload": payload,
    })
    assert row["Date"] == "2026-06-19"
    assert row["Time"] == "21:07:27"


def test_secure_erase_importer_backfills_nfs_records_and_export_dedupes(tmp_path):
    records_root = tmp_path / ".vstl-secure-erase"
    records_root.mkdir()
    data = tmp_path / "audits.jsonl"
    record_path = records_root / "drive-fingerprint.json"
    record = {
        "schema": "vstl_secure_erase_authorization_v1",
        "ok": True,
        "erase_ok": True,
        "erase_verified": True,
        "system_serial": "1BZK2R2",
        "drive_fingerprint": "drive-fingerprint",
        "device_type": "SATA_SSD",
        "device_model": "TOSHIBA KSG60ZMV256G M.2 2280 256GB",
        "drive_serial": "59GA26LXK8GN",
        "device_size_bytes": 256060514304,
        "wipe_method": "ATA_SANITIZE_BLOCK_ERASE",
        "wipe_standard": "NIST SP 800-88 Purge",
        "certificate_id": "SE-20260619-0742B2CEC01D2066",
        "certificate_status": "issued",
        "verification_hash": "0742",
        "certificate": {
            "certificate_id": "SE-20260619-0742B2CEC01D2066",
            "serial_no": "1BZK2R2",
            "brand": "Dell Inc.",
            "model": "Latitude 5490",
            "mac_id": "8C:04:BA:1E:69:92",
            "technician_level": "L1",
            "bench_id": "debian",
            "device_model": "TOSHIBA KSG60ZMV256G M.2 2280 256GB",
            "device_serial": "59GA26LXK8GN",
            "device_size_gb": 256,
            "wipe_method": "ATA_SANITIZE_BLOCK_ERASE",
            "wipe_standard": "NIST SP 800-88 Purge",
            "duration_sec": 8,
            "certificate_status": "issued",
        },
    }
    record_path.write_text(json.dumps(record), encoding="utf-8")
    os.utime(record_path, (1781892447, 1781892447))

    assert importer.import_records(records_root, data) == 1
    assert importer.import_records(records_root, data) == 0
    rows = exporter.load_rows(data, "secure_erase")
    assert len(rows) == 1
    assert rows[0]["Serial Number"] == "1BZK2R2"
    assert rows[0]["Model Name"] == "Latitude 5490"
    assert rows[0]["Secure Erase Reg ID"] == "SE-20260619-0742B2CEC01D2066"
    assert rows[0]["Wipe Method"] == "ATA_SANITIZE_BLOCK_ERASE"
    assert rows[0]["Date"] == "2026-06-19"


def test_duplicate_serials_are_preserved_and_counted_per_operation(tmp_path):
    records = []
    for second in (1, 2, 3):
        payload = _sample_payload()
        payload["session_started_at"] = f"2026-06-14T10:11:{second:02d}+00:00"
        payload["phase3"] = {}
        records.append({"payload": payload})

    restore_payload = _sample_payload()
    restore_payload["qc_tests"] = {}
    restore_payload["phase3"] = {"restore": {"ok": True}}
    records.append({"payload": restore_payload})

    rows = [exporter.flatten(record) for record in records]
    sheets = dict(exporter._xlsx_sheets(rows, "all"))

    assert len(sheets["QC"]) == 3
    assert [row["Number of Entries"] for row in sheets["QC"]] == ["3", "3", "3"]
    assert len(sheets["Restore"]) == 1
    assert sheets["Restore"][0]["Number of Entries"] == "1"

    xlsx_path = tmp_path / "duplicates.xlsx"
    exporter.write_xlsx(rows, xlsx_path, "all")
    with zipfile.ZipFile(xlsx_path) as archive:
        qc_sheet = archive.read("xl/worksheets/sheet2.xml").decode()
        assert qc_sheet.count("<t>SER123</t>") == 3


def test_export_enriches_backfilled_rows_from_same_serial_hardware(tmp_path):
    donor = _sample_payload()
    donor["serial_no"] = "SER-MISSING-HW"
    donor["phase3"] = {}

    backfill = {
        "session_started_at": "2026-06-19T10:00:00+00:00",
        "serial_no": "SER-MISSING-HW",
        "selected_option_label": "Certified Secure Erase",
        "report_source": "secure_erase_authorization_backfill",
        "phase3": {
            "erase": {
                "ok": True,
                "certificate_id": "SE-BACKFILL-001",
                "method": "ATA_SANITIZE_BLOCK_ERASE",
            }
        },
        "raw_data": {
            "storage": {
                "drive_count": 1,
                "drives": [{"model_description": "Minimal erased SSD"}],
            }
        },
    }
    data = tmp_path / "audits.jsonl"
    data.write_text(
        json.dumps({"payload": backfill}) + "\n"
        + json.dumps({"payload": donor}) + "\n",
        encoding="utf-8",
    )

    rows = exporter.load_rows(data, "secure_erase")

    assert rows[0]["Serial Number"] == "SER-MISSING-HW"
    assert rows[0]["Model Name"] == "Latitude Test"
    assert rows[0]["CPU"] == "Intel Test CPU"
    assert rows[0]["RAM Serial Number"] == "RAM1; RAM2"
    assert rows[0]["Battery Serial Number"] == "BAT1"


def test_capture_importer_backfills_nfs_capture_metadata(tmp_path):
    image_dir = tmp_path / "images" / "DELL_LATITUDE_5490_Win_11"
    image_dir.mkdir(parents=True)
    metadata = {
        "schema": "vstl_capture_metadata_v2",
        "captured_at": "2026-06-19T10:58:41+00:00",
        "image_name": "DELL_LATITUDE_5490_Win_11.img",
        "source_serial": "1BZK2R2",
        "source_device": "/dev/sda",
        "brand": "Dell Inc.",
        "model": "Latitude 5490",
        "sku": "0816",
        "cpu": "Intel Core i5-8350U @ 1.70GHz",
        "os_name": "Windows 11 Pro",
        "os_version": "25H2",
        "validation": {
            "ok": True,
            "os_info": {"os_name": "Windows 11 Pro", "os_version": "25H2"},
            "secure_erase_gate": {
                "identity": {
                    "device_model": "TOSHIBA KSG60ZMV256G",
                    "device_type": "SATA_SSD",
                    "drive_serial": "59GA26LXK8GN",
                    "device_size_bytes": 256060514304,
                }
            },
        },
    }
    (image_dir / "vstl_capture_metadata.json").write_text(
        json.dumps(metadata), encoding="utf-8",
    )
    data = tmp_path / "audits.jsonl"

    assert capture_importer.import_records(tmp_path / "images", data) == 1
    assert capture_importer.import_records(tmp_path / "images", data) == 0
    rows = exporter.load_rows(data, "capture")

    assert len(rows) == 1
    assert rows[0]["Operation"] == "Capture"
    assert rows[0]["Serial Number"] == "1BZK2R2"
    assert rows[0]["Model Name"] == "Latitude 5490"
    assert rows[0]["CPU"] == "Intel Core i5-8350U @ 1.70GHz"
    assert rows[0]["Storage Type"] == "SATA_SSD"
