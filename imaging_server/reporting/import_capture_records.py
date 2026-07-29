#!/usr/bin/env python3
"""Backfill report rows from captured-image metadata on the NFS image share."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _iso_from_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _existing_capture_keys(data_path: Path) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    if not data_path.exists():
        return keys
    with data_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
            phase3 = payload.get("phase3") if isinstance(payload.get("phase3"), dict) else {}
            capture = phase3.get("capture") if isinstance(phase3.get("capture"), dict) else {}
            if not capture:
                continue
            result = capture.get("result") if isinstance(capture.get("result"), dict) else {}
            image_name = _text(capture.get("image_name") or result.get("image_name"))
            serial = _text(payload.get("serial_no")).upper()
            if serial or image_name:
                keys.add((serial, image_name))
    return keys


def _drive_from_metadata(metadata: dict) -> dict:
    validation = metadata.get("validation") if isinstance(metadata.get("validation"), dict) else {}
    gate = validation.get("secure_erase_gate") if isinstance(validation.get("secure_erase_gate"), dict) else {}
    identity = gate.get("identity") if isinstance(gate.get("identity"), dict) else {}
    record = gate.get("record") if isinstance(gate.get("record"), dict) else {}
    size_bytes = identity.get("device_size_bytes") or record.get("device_size_bytes") or 0
    try:
        size_gb = str(round(int(size_bytes) / 1_000_000_000))
    except (TypeError, ValueError):
        size_gb = ""
    return {
        "size": f"{size_gb} GB" if size_gb else "",
        "health": "",
        "vendor": "",
        "model_description": _text(identity.get("device_model") or record.get("device_model")),
        "storage_type": _text(identity.get("device_type") or record.get("device_type")),
        "serial_number": _text(identity.get("drive_serial") or record.get("drive_serial")),
        "ct_number": "",
        "part_number": "",
    }


def _payload_from_metadata(metadata: dict, received_at: str, metadata_path: Path) -> dict:
    validation = metadata.get("validation") if isinstance(metadata.get("validation"), dict) else {}
    os_info = validation.get("os_info") if isinstance(validation.get("os_info"), dict) else {}
    serial = _text(metadata.get("source_serial"))
    image_name = _text(metadata.get("image_name"))
    return {
        "schema": "vstl_capture_metadata_backfill_report_v1",
        "report_source": "capture_metadata_backfill",
        "session_started_at": _text(metadata.get("captured_at")) or received_at,
        "technician_level": "",
        "bench_id": "",
        "serial_no": serial,
        "sku": _text(metadata.get("sku") or metadata.get("part_number")),
        "mac_id": "",
        "brand": _text(metadata.get("brand")),
        "model": _text(metadata.get("model")),
        "cpu": _text(metadata.get("cpu")),
        "status": "completed",
        "selected_option": 4,
        "selected_option_label": "Capture Full System Image",
        "installed_os": _text(metadata.get("os_name") or os_info.get("os_name")),
        "os_version": _text(metadata.get("os_version") or os_info.get("os_version")),
        "raw_data": {
            "storage": {
                "drive_count": 1,
                "drives": [_drive_from_metadata(metadata)],
            },
            "os": {
                "os_name": _text(metadata.get("os_name") or os_info.get("os_name")),
                "os_version": _text(metadata.get("os_version") or os_info.get("os_version")),
                "os_build": _text(metadata.get("os_build") or os_info.get("os_build")),
                "evidence": _text(os_info.get("evidence")),
            },
        },
        "phase3": {
            "capture": {
                "ok": True,
                "verified": bool(validation.get("ok", True)),
                "image_name": image_name,
                "device": _text(metadata.get("source_device") or validation.get("device")),
                "metadata_path": str(metadata_path),
            }
        },
    }


def import_records(records_root: Path, data_path: Path) -> int:
    existing = _existing_capture_keys(data_path)
    pending = []
    for path in sorted(records_root.rglob("vstl_capture_metadata.json")):
        try:
            metadata = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        received_at = _text(metadata.get("captured_at")) or _iso_from_mtime(path)
        payload = _payload_from_metadata(metadata, received_at, path)
        key = (_text(payload.get("serial_no")).upper(), _text((payload["phase3"]["capture"]).get("image_name")))
        if key in existing:
            continue
        existing.add(key)
        pending.append({"received_at": received_at, "payload": payload})
    if not pending:
        return 0
    data_path.parent.mkdir(parents=True, exist_ok=True)
    with data_path.open("a", encoding="utf-8") as handle:
        for record in pending:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(pending)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records_root", nargs="?", default="/images/dev")
    parser.add_argument("data_path", nargs="?", default="/var/lib/vstl-reports/audits.jsonl")
    args = parser.parse_args()
    count = import_records(Path(args.records_root), Path(args.data_path))
    print(f"imported={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
