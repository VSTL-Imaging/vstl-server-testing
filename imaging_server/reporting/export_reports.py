#!/usr/bin/env python3
"""Export append-only VSTL audit records as CSV or XLSX."""
from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


HEADERS = [
    "Operation", "Overall Status", "Audit Submission Status",
    "Operation Elapsed Time (sec)",
    "QC Elapsed Time (sec)", "Restore Elapsed Time (sec)",
    "Secure Erase Elapsed Time (sec)", "Capture Elapsed Time (sec)",
    "Secure Erase Reg ID",
    "Wipe Method", "Wipe Standard", "Wipe Verified", "Wipe Duration (sec)",
    "Certificate Status", "Date", "Time",
    "Technician Level", "User", "Bench ID", "Serial Number", "Number of Entries",
    "SKU / Product Number",
    "MAC ID", "Model Name", "CPU", "Integrated GPU Name", "Discrete GPU Name",
    "Discrete GPU Memory (GB)", "Total RAM (GB)", "RAM Type",
    "Number of RAM Modules", "RAM Vendor", "RAM Model Description",
    "RAM Serial Number", "RAM CT Number", "RAM Part Number",
    "Storage Capacity (GB)", "Storage Type", "Storage Health (%)",
    "Number of Storage Devices", "Storage Vendor", "Storage Model Description",
    "Storage Serial Number", "Storage CT Number", "Storage Part Number",
    "Battery Health (%)",
    "Battery Designed Capacity (mWh)", "Battery Full Charged Capacity (mWh)",
    "Battery Current Capacity (mWh)",
    "Battery Cycle Count", "Number of Batteries",
    "Battery Vendor", "Battery Model Description", "Battery Serial Number",
    "Battery CT Number", "Battery Part Number", "System Board CT Number",
    "BIOS Version", "Installed OS", "OS Version",
    "OS License Key", "Display Resolution", "Display Resolution (Short)",
    "Display Type", "Display Test Result", "Display Remarks", "Keyboard Type",
    "Keyboard Language", "Keyboard Status", "Camera Status", "Speaker Status", "Microphone Status",
    "Fingerprint Status", "Driver Preflight Status", "Driver Preflight Evidence",
    "Driver Preflight Remarks", "Wi-Fi Status", "Bluetooth Status", "Ports Availability",
    "Ports Remarks", "Cosmetic Grade", "Parts Required", "Additional Remarks",
    "MDM Status", "BIOS Lock Status",
    "Lot Number", "Box Number", "Box Model",
    "Box Total Units", "Box Imaged Units", "Box Remaining Units",
    "Burn Stress Test Result", "Burn Stress Test Duration (sec)",
    "Burn Stress Max Temperature (C)", "CPU Fan Status", "CPU Fan Min RPM",
    "CPU Fan Max RPM", "CPU Fan Average RPM", "Burn Stress Test Remarks",
]

CENTER_VALUE_HEADERS = {
    "Operation", "Overall Status", "Audit Submission Status",
    "Operation Elapsed Time (sec)",
    "QC Elapsed Time (sec)", "Restore Elapsed Time (sec)",
    "Secure Erase Elapsed Time (sec)", "Capture Elapsed Time (sec)",
    "Secure Erase Reg ID",
    "Wipe Method", "Wipe Standard", "Wipe Verified", "Wipe Duration (sec)",
    "Certificate Status", "Date", "Time",
    "Technician Level", "User", "Bench ID", "Serial Number", "Number of Entries",
    "SKU / Product Number",
    "MAC ID", "Discrete GPU Memory (GB)", "Total RAM (GB)", "RAM Type",
    "Number of RAM Modules", "RAM CT Number", "Storage Capacity (GB)", "Storage Type",
    "Storage Health (%)", "Number of Storage Devices", "Battery Health (%)",
    "Battery Designed Capacity (mWh)", "Battery Current Capacity (mWh)",
    "Battery Full Charged Capacity (mWh)", "Battery Cycle Count",
    "Number of Batteries", "Storage CT Number",
    "Battery CT Number", "System Board CT Number", "BIOS Version",
    "Installed OS", "OS Version",
    "OS License Key", "Display Resolution", "Display Resolution (Short)",
    "Display Type", "Display Test Result", "Display Remarks", "Keyboard Type",
    "Keyboard Language",
    "Fingerprint Status", "Driver Preflight Status", "Cosmetic Grade",
    "Lot Number", "Box Number", "Box Model",
    "Box Total Units", "Box Imaged Units", "Box Remaining Units",
    "Burn Stress Test Result",
    "Burn Stress Test Duration (sec)", "Burn Stress Max Temperature (C)",
    "CPU Fan Status", "CPU Fan Min RPM", "CPU Fan Max RPM",
    "CPU Fan Average RPM",
}

REPORT_SHEETS = [
    ("restore", "Restore"),
    ("qc", "QC"),
    ("secure erase", "Secure Erase"),
    ("capture", "Capture"),
]

REMOVED_CAPTURE_SECURE_ERASE_HEADERS = {
    "Display Resolution", "Display Resolution (Short)", "Display Type",
    "Display Test Result", "Display Remarks", "Keyboard Type", "Keyboard Language", "Keyboard Status",
    "Camera Status", "Speaker Status", "Microphone Status", "Fingerprint Status",
    "Driver Preflight Status", "Driver Preflight Evidence", "Driver Preflight Remarks",
    "Wi-Fi Status", "Bluetooth Status", "Ports Availability", "Ports Remarks",
    "Cosmetic Grade", "Parts Required", "Additional Remarks",
    "MDM Status", "BIOS Lock Status",
    "Burn Stress Test Result", "Burn Stress Test Duration (sec)",
    "Burn Stress Max Temperature (C)", "CPU Fan Status", "CPU Fan Min RPM",
    "CPU Fan Max RPM", "CPU Fan Average RPM", "Burn Stress Test Remarks",
}

SHEET_HEADER_OVERRIDES = {
    "QC": {"Installed OS": "Current OS"},
    "Capture": {"Installed OS": "Captured OS"},
}


def _report_timezone() -> ZoneInfo:
    name = os.environ.get("VSTL_REPORT_TIMEZONE", "Asia/Dubai")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _sheet_headers(sheet_name: str) -> list[str]:
    headers = list(HEADERS)
    if sheet_name in {"Secure Erase", "Capture"}:
        headers = [
            header for header in headers
            if header not in REMOVED_CAPTURE_SECURE_ERASE_HEADERS
        ]
    if sheet_name == "Capture":
        headers = [header for header in headers if header != "Secure Erase Reg ID"]
    return headers


def _display_header(sheet_name: str, header: str) -> str:
    return SHEET_HEADER_OVERRIDES.get(sheet_name, {}).get(header, header)


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value).strip()


def _has_value(value) -> bool:
    text = _text(value)
    return bool(text and text.upper() not in {"UNKNOWN", "N/A", "NA", "NONE"})


def _first_text(*values) -> str:
    for value in values:
        text = _text(value)
        if text:
            return text
    return ""


def _keyboard_language(payload: dict, raw: dict) -> str:
    profile = payload.get("keyboard_profile")
    if not isinstance(profile, dict):
        profile = {}
    raw_keyboard = raw.get("keyboard")
    if not isinstance(raw_keyboard, dict):
        raw_keyboard = {}
    return _first_text(
        payload.get("keyboard_language"),
        profile.get("print_format"),
        raw_keyboard.get("print_format"),
    )


def _box_scope_details(payload: dict, raw: dict) -> dict[str, str]:
    raw_box = (
        raw.get("box_scope")
        if isinstance(raw, dict) and isinstance(raw.get("box_scope"), dict)
        else {}
    )
    payload_box = payload.get("box_scope") if isinstance(payload.get("box_scope"), dict) else {}

    def pick(*values) -> str:
        return _first_text(*values)

    lot_no = pick(
        payload.get("lot_no"),
        payload.get("lot_number"),
        payload_box.get("lot_no"),
        payload_box.get("lot_number"),
        raw_box.get("lot_no"),
        raw_box.get("lot_number"),
    )
    box_no = pick(
        payload.get("box_no"),
        payload.get("box_number"),
        payload_box.get("box_no"),
        payload_box.get("box_number"),
        raw_box.get("box_no"),
        raw_box.get("box_number"),
    )
    model_label = pick(
        payload.get("box_model_label"),
        payload.get("box_model"),
        payload_box.get("model_label"),
        raw_box.get("model_label"),
    )
    if not model_label:
        brand = pick(payload.get("box_brand"), payload_box.get("brand"), raw_box.get("brand"))
        model = pick(payload.get("box_model_name"), payload_box.get("model"), raw_box.get("model"))
        if brand and model and brand.casefold() not in model.casefold():
            model_label = f"{brand} {model}"
        else:
            model_label = model or brand

    return {
        "lot_no": lot_no,
        "box_no": box_no,
        "model_label": model_label,
        "total": pick(payload.get("box_total"), payload_box.get("total"), raw_box.get("total")),
        "imaged": pick(payload.get("box_imaged"), payload_box.get("imaged"), raw_box.get("imaged")),
        "remaining": pick(
            payload.get("box_remaining"),
            payload_box.get("remaining"),
            raw_box.get("remaining"),
        ),
    }


_LENOVO_MTM_RE = re.compile(r"^[0-9A-Z]{4}[0-9A-Z]{3,}$", re.I)


def _lenovo_human_model_from_sku(sku: str) -> str:
    text = _text(sku)
    match = re.search(r"(?:^|_)FM_(.+)$", text, re.I)
    if not match:
        return ""
    model = match.group(1).replace("_", " ").strip()
    return re.sub(r"\s+", " ", model)


def _looks_like_lenovo_mtm(value: str) -> bool:
    text = _text(value).upper()
    if not text or text == "UNKNOWN":
        return False
    return bool(_LENOVO_MTM_RE.fullmatch(text) and any(ch.isdigit() for ch in text))


def _normalized_identity_fields(payload: dict) -> tuple[str, str]:
    model = _text(payload.get("model"))
    sku = _text(payload.get("sku"))
    brand = _first_text(payload.get("brand"), payload.get("manufacturer"))
    raw = payload.get("raw_data") if isinstance(payload.get("raw_data"), dict) else {}
    if isinstance(raw.get("identity"), dict):
        identity = raw["identity"]
        brand = _first_text(brand, identity.get("brand"), identity.get("manufacturer"))
    if "LENOVO" not in brand.upper():
        return model, sku
    human_model = _lenovo_human_model_from_sku(sku)
    if not human_model:
        return model, sku
    normalized_sku = model if _looks_like_lenovo_mtm(model) else sku
    return human_model, normalized_sku


def _parse_named_value(text: str, name: str) -> str:
    match = re.search(
        rf"\b{re.escape(name)}\b\s*[:=]\s*([^;,\n\r]+)",
        text or "",
        re.I,
    )
    return match.group(1).strip() if match else ""


def _burn_text_blob(burn: dict) -> str:
    parts = [
        _text(burn.get("remarks")),
        _text(burn.get("evidence")),
        _text(burn.get("summary")),
    ]
    raw = burn.get("raw") if isinstance(burn.get("raw"), dict) else {}
    for key in ("stress_ng", "memtester_last", "fio_last"):
        parts.append(_text(raw.get(key)))
    return "; ".join(part for part in parts if part)


def _fan_samples_from_burn(burn: dict) -> list[int]:
    raw = burn.get("raw") if isinstance(burn.get("raw"), dict) else {}
    values = raw.get("fan_samples_rpm") or []
    samples: list[int] = []
    for value in values:
        try:
            rpm = int(float(value))
        except (TypeError, ValueError):
            continue
        samples.append(max(0, rpm))
    return samples


def _burn_fan_field(burn: dict, field: str) -> str:
    fan = burn.get("fan") if isinstance(burn.get("fan"), dict) else {}
    text_blob = _burn_text_blob(burn)
    aliases = {
        "fan_status": ("fan_status", "cpu_fan_status"),
        "fan_min_rpm": ("fan_min_rpm", "cpu_fan_min_rpm"),
        "fan_max_rpm": ("fan_max_rpm", "cpu_fan_max_rpm"),
        "fan_avg_rpm": ("fan_avg_rpm", "cpu_fan_avg_rpm", "fan_average_rpm"),
    }
    for alias in aliases[field]:
        value = _first_text(burn.get(alias), fan.get(alias))
        if value:
            return value
        parsed = _parse_named_value(text_blob, alias)
        if parsed:
            return parsed
    samples = _fan_samples_from_burn(burn)
    if field == "fan_status":
        if samples:
            return "OK" if any(sample > 0 for sample in samples) else "NOT SPINNING"
        return ""
    nonzero = [sample for sample in samples if sample > 0]
    if not nonzero:
        return ""
    if field == "fan_min_rpm":
        return str(min(nonzero))
    if field == "fan_max_rpm":
        return str(max(nonzero))
    if field == "fan_avg_rpm":
        return str(round(sum(nonzero) / len(nonzero)))
    return ""


def _join(items: list[dict], key: str) -> str:
    values = [_text(item.get(key)) for item in items]
    return "; ".join(value for value in values if value and value.upper() != "UNKNOWN")


def _join_distinct(items: list[dict], key: str, *different_from_keys: str) -> str:
    values = []
    seen = set()
    for item in items:
        value = _text(item.get(key))
        if not value or value.upper() == "UNKNOWN":
            continue
        related = {_text(item.get(other)).upper() for other in different_from_keys}
        folded = value.upper()
        if folded in related or folded in seen:
            continue
        seen.add(folded)
        values.append(value)
    return "; ".join(values)


def _timestamp(record: dict, payload: dict) -> tuple[str, str]:
    # Server receive time is the audit clock of record. Bench laptops can boot
    # with stale RTC values, and several secure-erase records have client
    # timestamps hours away from the server's UAE-local wall clock.
    raw = record.get("received_at") or payload.get("session_started_at") or ""
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        stamp = datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    stamp = stamp.astimezone(_report_timezone())
    return stamp.date().isoformat(), stamp.strftime("%H:%M:%S")


def _row_sort_key(row: dict[str, str]) -> tuple[str, str]:
    return _text(row.get("Date")), _text(row.get("Time"))


def _newest_first(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(rows, key=_row_sort_key, reverse=True)


def _operations(payload: dict) -> list[str]:
    phase3 = payload.get("phase3") or {}
    operations = []
    if payload.get("qc_tests"):
        operations.append("QC")
    if phase3.get("restore"):
        operations.append("Restore")
    if phase3.get("erase"):
        operations.append("Secure Erase")
    if phase3.get("capture"):
        operations.append("Capture")
    if not operations:
        label = _text(payload.get("selected_option_label"))
        operations.append(label or "Audit")
    return operations


def _record_payload(record: dict) -> dict:
    return record.get("payload") if isinstance(record.get("payload"), dict) else record


def _section_score(value) -> int:
    if isinstance(value, dict):
        return sum(_section_score(child) for child in value.values())
    if isinstance(value, list):
        return sum(_section_score(child) for child in value)
    return 1 if _has_value(value) else 0


def _payload_score(payload: dict) -> int:
    raw = payload.get("raw_data") if isinstance(payload.get("raw_data"), dict) else {}
    score_keys = (
        "sku", "mac_id", "brand", "model", "cpu", "ram", "bios_version",
        "installed_os", "os_version",
    )
    return (
        sum(1 for key in score_keys if _has_value(payload.get(key)))
        + _section_score(raw.get("gpu"))
        + _section_score(raw.get("ram"))
        + _section_score(raw.get("storage"))
        + _section_score(raw.get("battery"))
        + _section_score(raw.get("system_board"))
        + _section_score(raw.get("bios"))
        + _section_score(raw.get("os"))
    )


def _donors_by_serial(records: list[dict]) -> dict[str, dict]:
    donors: dict[str, dict] = {}
    for record in records:
        payload = _record_payload(record)
        serial = _text(payload.get("serial_no")).upper()
        if not serial:
            continue
        current = donors.get(serial)
        if current is None or _payload_score(payload) > _payload_score(current):
            donors[serial] = payload
    return donors


def _merge_related_hardware(record: dict, donors: dict[str, dict]) -> dict:
    payload = _record_payload(record)
    serial = _text(payload.get("serial_no")).upper()
    donor = donors.get(serial)
    if not donor or donor is payload:
        return record

    merged = copy.deepcopy(record)
    target = _record_payload(merged)
    for key in (
        "sku", "mac_id", "brand", "model", "cpu", "ram", "bios_version",
        "installed_os", "os_version", "os_license_key",
    ):
        if not _has_value(target.get(key)) and _has_value(donor.get(key)):
            target[key] = copy.deepcopy(donor.get(key))

    target_raw = target.setdefault("raw_data", {})
    donor_raw = donor.get("raw_data") if isinstance(donor.get("raw_data"), dict) else {}
    for key in ("gpu", "ram", "storage", "battery", "system_board", "bios", "os"):
        donor_section = donor_raw.get(key)
        if not donor_section:
            continue
        target_section = target_raw.get(key)
        if _section_score(donor_section) > _section_score(target_section):
            target_raw[key] = copy.deepcopy(donor_section)
    return merged


def _resolution_short(value: str) -> str:
    match = re.search(r"(\d{3,5})\s*[xX×]\s*(\d{3,5})", value or "")
    if not match:
        return ""
    size = (int(match.group(1)), int(match.group(2)))
    names = {
        (1280, 720): "HD",
        (1366, 768): "HD",
        (1600, 900): "HD+",
        (1920, 1080): "FHD",
        (1920, 1200): "WUXGA",
        (2560, 1440): "QHD",
        (2560, 1600): "WQXGA",
        (3840, 2160): "4K UHD",
    }
    return names.get(size, f"{size[0]}x{size[1]}")


def _test_status(tests: dict, key: str) -> str:
    return _text((tests.get(key) or {}).get("result"))


def _operation_elapsed_from_row(row: dict[str, str], sheet_name: str) -> str:
    mapping = {
        "QC": "QC Elapsed Time (sec)",
        "Restore": "Restore Elapsed Time (sec)",
        "Secure Erase": "Secure Erase Elapsed Time (sec)",
        "Capture": "Capture Elapsed Time (sec)",
    }
    return _text(row.get(mapping.get(sheet_name, "")))


def _normalize_audit_submission_status(value) -> str:
    status = _text(value)
    folded = status.lower()
    if not folded:
        return ""
    if any(token in folded for token in ("failed", "failure", "error", "expired", "queued", "retry", "false")):
        return "Submission Failed"
    if any(token in folded for token in ("submitted", "success", "saved", "stored", "ok", "true")):
        return "Audit Submitted"
    return status


def _audit_submission_status(payload: dict) -> str:
    cloud_audit = payload.get("cloud_audit") if isinstance(payload.get("cloud_audit"), dict) else {}
    audit_submission = (
        payload.get("audit_submission")
        if isinstance(payload.get("audit_submission"), dict)
        else {}
    )
    status = _first_text(
        payload.get("audit_submission_status"),
        payload.get("cloud_audit_status"),
        payload.get("cloud_submission_status"),
        audit_submission.get("status"),
        cloud_audit.get("status"),
    )
    if status:
        return _normalize_audit_submission_status(status)
    for ok in (audit_submission.get("ok"), cloud_audit.get("ok")):
        if isinstance(ok, bool):
            return "Audit Submitted" if ok else "Submission Failed"
    return ""


def flatten(record: dict) -> dict[str, str]:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
    raw = payload.get("raw_data") or {}
    gpu = raw.get("gpu") or {}
    ram = raw.get("ram") or {}
    modules = ram.get("modules") or []
    storage = raw.get("storage") or {}
    drives = storage.get("drives") or []
    battery = raw.get("battery") or {}
    batteries = battery.get("batteries") or []
    system_board = raw.get("system_board") or payload.get("system_board") or {}
    bios = raw.get("bios") or {}
    os_info = raw.get("os") or {}
    burn = payload.get("burn_test") or raw.get("burn_test") or {}
    display = payload.get("display") or {}
    tests = (payload.get("qc_tests") or {}).get("tests") or {}
    locks = set((payload.get("lock_audit") or {}).get("detected_locks") or [])
    date, time = _timestamp(record, payload)
    operations = _operations(payload)
    phase3 = payload.get("phase3") or {}
    erase = phase3.get("erase") if isinstance(phase3.get("erase"), dict) else {}
    restore = phase3.get("restore") if isinstance(phase3.get("restore"), dict) else {}
    capture = phase3.get("capture") if isinstance(phase3.get("capture"), dict) else {}
    erase_result = erase.get("result") if isinstance(erase.get("result"), dict) else {}
    restore_result = restore.get("result") if isinstance(restore.get("result"), dict) else {}
    capture_result = capture.get("result") if isinstance(capture.get("result"), dict) else {}
    erase_record = erase.get("record") if isinstance(erase.get("record"), dict) else {}
    erase_registration = (
        erase.get("registration") if isinstance(erase.get("registration"), dict) else {}
    )
    erase_certificate = (
        erase.get("certificate") if isinstance(erase.get("certificate"), dict) else {}
    )
    normalized_model, normalized_sku = _normalized_identity_fields(payload)

    wireless = tests.get("wireless") or {}
    wifi = _text(wireless.get("wifi_status") or wireless.get("result"))
    bluetooth = _text(wireless.get("bluetooth_status") or wireless.get("result"))
    resolution = _text(display.get("resolution"))
    operation_statuses = []
    for value in phase3.values():
        if isinstance(value, dict):
            operation_statuses.append("PASS" if value.get("ok") else "FAIL")
    overall = _text(payload.get("status"))
    if not overall and operation_statuses:
        overall = "PASS" if all(x == "PASS" for x in operation_statuses) else "FAIL"
    if not overall and payload.get("qc_tests"):
        overall = "FAIL" if (payload.get("qc_tests") or {}).get("failed") else "PASS"
    qc_tests = payload.get("qc_tests") if isinstance(payload.get("qc_tests"), dict) else {}
    post_qc = qc_tests.get("post_qc") if isinstance(qc_tests.get("post_qc"), dict) else {}
    box_scope = _box_scope_details(payload, raw)
    qc_elapsed = _first_text(
        payload.get("qc_elapsed_sec"),
        payload.get("qc_duration_sec"),
        qc_tests.get("elapsed_sec"),
        qc_tests.get("duration_sec"),
        burn.get("actual_duration_sec") if payload.get("qc_tests") else "",
        burn.get("duration_sec") if payload.get("qc_tests") else "",
    )
    restore_elapsed = _first_text(
        restore.get("duration_sec"),
        restore.get("elapsed_sec"),
        restore_result.get("duration_sec"),
        restore_result.get("elapsed_sec"),
    )
    secure_erase_elapsed = _first_text(
        erase.get("duration_sec"),
        erase.get("elapsed_sec"),
        erase.get("wipe_duration_sec"),
        erase_result.get("duration_sec"),
        erase_result.get("elapsed_sec"),
        erase_record.get("duration_sec"),
        erase_certificate.get("duration_sec"),
    )
    capture_elapsed = _first_text(
        capture.get("duration_sec"),
        capture.get("elapsed_sec"),
        capture_result.get("duration_sec"),
        capture_result.get("elapsed_sec"),
    )

    return {
        "Operation": "; ".join(operations),
        "Overall Status": overall,
        "Audit Submission Status": _audit_submission_status(payload),
        "Operation Elapsed Time (sec)": "",
        "QC Elapsed Time (sec)": qc_elapsed,
        "Restore Elapsed Time (sec)": restore_elapsed,
        "Secure Erase Elapsed Time (sec)": secure_erase_elapsed,
        "Capture Elapsed Time (sec)": capture_elapsed,
        "Secure Erase Reg ID": _first_text(
            payload.get("secure_erase_reg_id"),
            payload.get("secure_erase_registration_id"),
            payload.get("certificate_id"),
            erase.get("secure_erase_reg_id"),
            erase.get("registration_id"),
            erase.get("reg_id"),
            erase.get("record_id"),
            erase.get("certificate_id"),
            erase_result.get("secure_erase_reg_id"),
            erase_result.get("registration_id"),
            erase_result.get("reg_id"),
            erase_result.get("record_id"),
            erase_result.get("certificate_id"),
            erase_record.get("secure_erase_reg_id"),
            erase_record.get("registration_id"),
            erase_record.get("reg_id"),
            erase_record.get("record_id"),
            erase_record.get("certificate_id"),
            erase_registration.get("secure_erase_reg_id"),
            erase_registration.get("registration_id"),
            erase_registration.get("reg_id"),
            erase_registration.get("record_id"),
            erase_registration.get("certificate_id"),
        ),
        "Wipe Method": _first_text(
            erase.get("method"),
            erase.get("wipe_method"),
            erase_result.get("method"),
            erase_result.get("wipe_method"),
            erase_record.get("wipe_method"),
            erase_certificate.get("wipe_method"),
        ),
        "Wipe Standard": _first_text(
            erase.get("wipe_standard"),
            erase.get("standard"),
            erase_result.get("wipe_standard"),
            erase_result.get("standard"),
            erase_record.get("wipe_standard"),
            erase_certificate.get("wipe_standard"),
        ),
        "Wipe Verified": _first_text(
            erase.get("verified"),
            erase.get("wipe_verified"),
            erase_result.get("verified"),
            erase_result.get("wipe_verified"),
            erase_record.get("erase_verified"),
            erase_certificate.get("wipe_verified"),
        ),
        "Wipe Duration (sec)": _first_text(
            erase.get("duration_sec"),
            erase.get("wipe_duration_sec"),
            erase_result.get("duration_sec"),
            erase_record.get("duration_sec"),
            erase_certificate.get("duration_sec"),
        ),
        "Certificate Status": _first_text(
            erase.get("certificate_status"),
            erase_record.get("certificate_status"),
            erase_certificate.get("certificate_status"),
        ),
        "Date": date,
        "Time": time,
        "Technician Level": _text(payload.get("technician_level")),
        "User": _first_text(
            payload.get("technician_user_name"),
            payload.get("user"),
            (raw.get("operator") or {}).get("name") if isinstance(raw.get("operator"), dict) else "",
        ),
        "Bench ID": _text(payload.get("bench_id")),
        "Serial Number": _text(payload.get("serial_no")),
        "Number of Entries": "",
        "SKU / Product Number": normalized_sku,
        "MAC ID": _text(payload.get("mac_id")),
        "Model Name": normalized_model,
        "CPU": _text(payload.get("cpu")),
        "Integrated GPU Name": _text(gpu.get("integrated_gpu")),
        "Discrete GPU Name": _text(gpu.get("discrete_gpu")),
        "Discrete GPU Memory (GB)": _text(gpu.get("discrete_gpu_memory")),
        "Total RAM (GB)": _text(payload.get("ram")),
        "Number of RAM Modules": _text(ram.get("module_count", len(modules))),
        "RAM Vendor": _join(modules, "vendor"),
        "RAM Model Description": _join(modules, "model_description"),
        "RAM Type": _join(modules, "ram_type"),
        "RAM Serial Number": _join(modules, "serial_number"),
        "RAM CT Number": _join(modules, "ct_number") or _join_distinct(modules, "product_number"),
        "RAM Part Number": _join(modules, "part_number"),
        "Storage Capacity (GB)": _join(drives, "size"),
        "Storage Health (%)": _join(drives, "health"),
        "Number of Storage Devices": _text(storage.get("drive_count", len(drives))),
        "Storage Vendor": _join(drives, "vendor"),
        "Storage Model Description": _join(drives, "model_description"),
        "Storage Type": _join(drives, "storage_type") or _join(drives, "type"),
        "Storage Serial Number": _join(drives, "serial_number"),
        "Storage CT Number": _join(drives, "ct_number") or _join_distinct(drives, "product_number"),
        "Storage Part Number": _join(drives, "part_number"),
        "Battery Health (%)": _join(batteries, "health"),
        "Number of Batteries": _text(battery.get("battery_count", len(batteries))),
        "Battery Designed Capacity (mWh)": _join(batteries, "design_capacity_mwh") or _join(batteries, "capacity_mwh"),
        "Battery Full Charged Capacity (mWh)": (
            _join(batteries, "full_charged_capacity_mwh")
            or _join(batteries, "current_capacity_mwh")
        ),
        "Battery Current Capacity (mWh)": (
            _join(batteries, "current_capacity_value_mwh")
            or _join(batteries, "current_capacity_mwh")
        ),
        "Battery Cycle Count": _join(batteries, "cycle_count"),
        "Battery Vendor": _join(batteries, "vendor"),
        "Battery Model Description": _join(batteries, "model_description"),
        "Battery Serial Number": _join(batteries, "serial_number"),
        "Battery CT Number": _join(batteries, "ct_number"),
        "Battery Part Number": _join(batteries, "part_number"),
        "System Board CT Number": _first_text(
            payload.get("system_board_ct_number"),
            system_board.get("ct_number"),
        ),
        "BIOS Version": _text(payload.get("bios_version") or bios.get("version")),
        "Installed OS": _text(payload.get("installed_os") or os_info.get("os_name")),
        "OS Version": _text(payload.get("os_version") or os_info.get("os_version")),
        "OS License Key": _text(payload.get("os_license_key")),
        "Display Resolution": resolution,
        "Display Resolution (Short)": _resolution_short(resolution),
        "Display Type": _text(display.get("type")),
        "Display Test Result": _text(display.get("result") or _test_status(tests, "display")),
        "Display Remarks": _text(display.get("remarks") or (tests.get("display") or {}).get("remarks")),
        "Keyboard Type": _text(payload.get("keyboard_type")),
        "Keyboard Language": _keyboard_language(payload, raw),
        "Keyboard Status": _text(_test_status(tests, "keyboard") or payload.get("keyboard_status")),
        "Camera Status": _test_status(tests, "camera"),
        "Speaker Status": _test_status(tests, "speaker"),
        "Microphone Status": _test_status(tests, "microphone"),
        "Fingerprint Status": _test_status(tests, "fingerprint"),
        "Driver Preflight Status": _test_status(tests, "driver_preflight"),
        "Driver Preflight Evidence": _text((tests.get("driver_preflight") or {}).get("evidence")),
        "Driver Preflight Remarks": _text((tests.get("driver_preflight") or {}).get("remarks")),
        "Wi-Fi Status": wifi,
        "Bluetooth Status": bluetooth,
        "Ports Availability": _text(payload.get("ports_availability") or (tests.get("ports") or {}).get("evidence")),
        "Ports Remarks": _text(payload.get("ports_remarks") or (tests.get("ports") or {}).get("remarks")),
        "Cosmetic Grade": _text(payload.get("cosmetic_grade") or post_qc.get("cosmetic_grade")),
        "Parts Required": _text(payload.get("parts_required") or post_qc.get("parts_required")),
        "Additional Remarks": _text(payload.get("additional_remarks") or post_qc.get("additional_remarks")),
        "MDM Status": "LOCKED" if locks.intersection({"intune", "azure_ad", "vendor_mdm"}) else "CLEAR",
        "BIOS Lock Status": "LOCKED" if "bios_password" in locks else "CLEAR",
        "Lot Number": _text(box_scope.get("lot_no")),
        "Box Number": _text(box_scope.get("box_no")),
        "Box Model": _text(box_scope.get("model_label")),
        "Box Total Units": _text(box_scope.get("total")),
        "Box Imaged Units": _text(box_scope.get("imaged")),
        "Box Remaining Units": _text(box_scope.get("remaining")),
        "Burn Stress Test Result": _text(burn.get("result")),
        "Burn Stress Test Duration (sec)": _text(burn.get("duration_sec")),
        "Burn Stress Max Temperature (C)": _text(burn.get("max_temp_c")),
        "CPU Fan Status": _burn_fan_field(burn, "fan_status"),
        "CPU Fan Min RPM": _burn_fan_field(burn, "fan_min_rpm"),
        "CPU Fan Max RPM": _burn_fan_field(burn, "fan_max_rpm"),
        "CPU Fan Average RPM": _burn_fan_field(burn, "fan_avg_rpm"),
        "Burn Stress Test Remarks": _text(burn.get("remarks")),
    }


def load_rows(data_path: Path, report_type: str) -> list[dict[str, str]]:
    rows = []
    if not data_path.exists():
        return rows
    wanted = report_type.replace("_", " ").lower()
    records = []
    with data_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    donors = _donors_by_serial(records)
    for record in records:
        operations = [item.lower() for item in _operations(_record_payload(record))]
        if wanted != "all" and not any(wanted in item for item in operations):
            continue
        rows.append(flatten(_merge_related_hardware(record, donors)))
    return _newest_first(rows)


def _csv_fieldnames(sheet_name: str) -> list[str]:
    return [_display_header(sheet_name, header) for header in _sheet_headers(sheet_name)]


def _csv_display_row(row: dict[str, str], sheet_name: str) -> dict[str, str]:
    return {
        _display_header(sheet_name, header): row.get(header, "")
        for header in _sheet_headers(sheet_name)
    }


def write_csv(rows: list[dict[str, str]], output: Path, report_type: str = "all") -> None:
    sheet_name, sheet_rows = _xlsx_sheets(rows, report_type)[0]
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=_csv_fieldnames(sheet_name))
        writer.writeheader()
        writer.writerows(_csv_display_row(row, sheet_name) for row in sheet_rows)


def _csv_bytes(rows: list[dict[str, str]], sheet_name: str) -> bytes:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=_csv_fieldnames(sheet_name))
    writer.writeheader()
    writer.writerows(_csv_display_row(row, sheet_name) for row in rows)
    return ("\ufeff" + handle.getvalue()).encode("utf-8")


def write_csv_bundle(rows: list[dict[str, str]], output: Path) -> None:
    """Write one CSV file per operation inside a ZIP archive.

    CSV has no worksheet concept, so the "All" CSV export mirrors the XLSX
    workbook layout by shipping separate operation CSV files together.
    """
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for sheet_name, sheet_rows in _xlsx_sheets(rows, "all"):
            filename = sheet_name.lower().replace(" ", "-") + ".csv"
            archive.writestr(filename, _csv_bytes(sheet_rows, sheet_name))


def _cell(ref: str, value: str, style: int = 0) -> str:
    safe = escape(_text(value))
    return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t>{safe}</t></is></c>'


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _column_widths(rows: list[dict[str, str]], sheet_name: str) -> list[float]:
    widths = []
    for header in _sheet_headers(sheet_name):
        values = [_display_header(sheet_name, header)]
        values.extend(_text(row.get(header, "")).replace("\r", " ").replace("\n", " ") for row in rows)
        longest = max((len(value) for value in values), default=0)
        # Add breathing room for Excel's filter arrow while keeping very long
        # evidence fields usable on normal screens.
        widths.append(float(min(max(longest + 4, 10), 80)))
    return widths


def _worksheet_xml(rows: list[dict[str, str]], sheet_name: str) -> str:
    sheet_rows = []
    headers = _sheet_headers(sheet_name)
    header_cells = "".join(
        _cell(f"{_column_name(i)}1", _display_header(sheet_name, header), 1)
        for i, header in enumerate(headers, 1)
    )
    sheet_rows.append(f'<row r="1" ht="22" customHeight="1">{header_cells}</row>')
    for row_no, row in enumerate(rows, 2):
        cells = "".join(
            _cell(
                f"{_column_name(col)}{row_no}",
                row.get(header, ""),
                2 if header in CENTER_VALUE_HEADERS else 3,
            )
            for col, header in enumerate(headers, 1)
        )
        sheet_rows.append(f'<row r="{row_no}" ht="20" customHeight="1">{cells}</row>')
    last_col = _column_name(len(headers))
    cols = "".join(
        f'<col min="{i}" max="{i}" width="{width:.1f}" bestFit="1" customWidth="1"/>'
        for i, width in enumerate(_column_widths(rows, sheet_name), 1)
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        '<cols>' + cols + '</cols><sheetData>' + "".join(sheet_rows) + '</sheetData>'
        f'<autoFilter ref="A1:{last_col}{max(1, len(rows) + 1)}"/>'
        '</worksheet>'
    )
    return sheet


def _xlsx_sheets(
    rows: list[dict[str, str]],
    report_type: str,
) -> list[tuple[str, list[dict[str, str]]]]:
    def with_entry_counts(
        source_rows: list[dict[str, str]],
        sheet_name: str,
    ) -> list[dict[str, str]]:
        prepared = []
        serial_counts: dict[str, int] = {}
        for row in source_rows:
            serial = _text(row.get("Serial Number")).upper()
            if serial:
                serial_counts[serial] = serial_counts.get(serial, 0) + 1
        for row in source_rows:
            prepared_row = dict(row)
            prepared_row["Operation"] = sheet_name
            prepared_row["Operation Elapsed Time (sec)"] = _operation_elapsed_from_row(
                prepared_row, sheet_name,
            )
            serial = _text(row.get("Serial Number")).upper()
            prepared_row["Number of Entries"] = (
                str(serial_counts.get(serial, 0)) if serial else ""
            )
            prepared.append(prepared_row)
        return prepared

    def row_score(row: dict[str, str]) -> tuple[int, str]:
        useful_headers = [
            "MAC ID", "Model Name", "CPU", "Storage Model Description",
            "Storage Serial Number", "Storage Capacity (GB)", "Storage Type",
            "Wipe Method", "Wipe Standard", "Certificate Status",
        ]
        filled = sum(1 for header in useful_headers if _text(row.get(header)))
        return filled, _text(row.get("Date")) + " " + _text(row.get("Time"))

    def dedupe_secure_erase_rows(source_rows: list[dict[str, str]]) -> list[dict[str, str]]:
        by_id: dict[str, dict[str, str]] = {}
        passthrough: list[dict[str, str]] = []
        for row in source_rows:
            reg_id = _text(row.get("Secure Erase Reg ID"))
            if not reg_id:
                passthrough.append(row)
                continue
            existing = by_id.get(reg_id)
            if existing is None or row_score(row) >= row_score(existing):
                by_id[reg_id] = row
        return passthrough + list(by_id.values())

    normalized_type = report_type.replace("_", " ").lower()
    if normalized_type != "all":
        title = next(
            (sheet_name for key, sheet_name in REPORT_SHEETS if key == normalized_type),
            "VSTL Report",
        )
        sheet_rows = dedupe_secure_erase_rows(rows) if title == "Secure Erase" else rows
        return [(title, with_entry_counts(_newest_first(sheet_rows), title))]

    sheets = []
    for operation_key, sheet_name in REPORT_SHEETS:
        operation_rows = []
        for row in rows:
            if operation_key not in _text(row.get("Operation")).lower():
                continue
            operation_row = dict(row)
            operation_row["Operation"] = sheet_name
            operation_rows.append(operation_row)
        if sheet_name == "Secure Erase":
            operation_rows = dedupe_secure_erase_rows(operation_rows)
        operation_rows = _newest_first(operation_rows)
        sheets.append((sheet_name, with_entry_counts(operation_rows, sheet_name)))
    return sheets


def write_xlsx(
    rows: list[dict[str, str]],
    output: Path,
    report_type: str = "all",
) -> None:
    sheets = _xlsx_sheets(rows, report_type)
    worksheet_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    workbook_sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, (name, _) in enumerate(sheets, 1)
    )
    worksheet_relationships = "".join(
        f'<Relationship Id="rId{i}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    styles_relationship_id = len(sheets) + 1
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + worksheet_overrides +
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets>{workbook_sheets}</sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + worksheet_relationships +
            f'<Relationship Id="rId{styles_relationship_id}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
            '</Relationships>'
        ),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="3"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill>'
            '<fill><patternFill patternType="solid"><fgColor rgb="FF7030A0"/><bgColor indexed="64"/></patternFill></fill></fills>'
            '<borders count="3"><border/>'
            '<border><left style="medium"><color rgb="FF000000"/></left>'
            '<right style="medium"><color rgb="FF000000"/></right>'
            '<top style="medium"><color rgb="FF000000"/></top>'
            '<bottom style="medium"><color rgb="FF000000"/></bottom>'
            '<diagonal/></border>'
            '<border><left style="thin"><color rgb="FF000000"/></left>'
            '<right style="thin"><color rgb="FF000000"/></right>'
            '<top style="thin"><color rgb="FF000000"/></top>'
            '<bottom style="thin"><color rgb="FF000000"/></bottom>'
            '<diagonal/></border></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="4"><xf xfId="0"/>'
            '<xf xfId="0" fontId="1" fillId="2" borderId="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
            '<alignment horizontal="center" vertical="center" wrapText="0"/></xf>'
            '<xf xfId="0" borderId="2" applyBorder="1" applyAlignment="1">'
            '<alignment horizontal="center" vertical="center" wrapText="0"/></xf>'
            '<xf xfId="0" borderId="2" applyBorder="1" applyAlignment="1">'
            '<alignment horizontal="left" vertical="center" wrapText="0"/></xf></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>'
        ),
    }
    for index, (sheet_name, sheet_rows) in enumerate(sheets, 1):
        files[f"xl/worksheets/sheet{index}.xml"] = _worksheet_xml(sheet_rows, sheet_name)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--format", choices=("csv", "csv_zip", "xlsx"), required=True)
    parser.add_argument("--type", choices=("all", "restore", "qc", "secure_erase", "capture"), default="all")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows = load_rows(Path(args.data), args.type)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "csv":
        write_csv(rows, output, args.type)
    elif args.format == "csv_zip":
        write_csv_bundle(rows, output)
    else:
        write_xlsx(rows, output, args.type)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
