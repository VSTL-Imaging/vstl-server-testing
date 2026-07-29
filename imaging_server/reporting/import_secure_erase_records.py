#!/usr/bin/env python3
"""Backfill reporting rows from verified secure-erase authorization records."""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows local tests
    fcntl = None


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _iso_from_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _existing_keys(data_path: Path) -> set[str]:
    keys: set[str] = set()
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
            erase = phase3.get("erase") if isinstance(phase3.get("erase"), dict) else {}
            certificate = erase.get("certificate") if isinstance(erase.get("certificate"), dict) else {}
            for value in (
                payload.get("secure_erase_reg_id"),
                erase.get("secure_erase_reg_id"),
                erase.get("certificate_id"),
                certificate.get("certificate_id"),
                erase.get("drive_fingerprint"),
                certificate.get("drive_fingerprint"),
            ):
                text = _text(value)
                if text:
                    keys.add(text)
    return keys


def _drive_row(record: dict, certificate: dict) -> dict:
    size_gb = certificate.get("device_size_gb")
    if not size_gb:
        size_bytes = int(record.get("device_size_bytes") or certificate.get("device_size_bytes") or 0)
        size_gb = round(size_bytes / 1_000_000_000) if size_bytes else ""
    return {
        "size": f"{size_gb} GB" if size_gb else "",
        "health": "",
        "vendor": "",
        "model_description": _text(
            certificate.get("device_model") or record.get("device_model")
        ),
        "storage_type": _text(record.get("device_type") or certificate.get("device_type")),
        "serial_number": _text(
            certificate.get("device_serial") or record.get("drive_serial")
        ),
        "ct_number": "",
        "part_number": "",
    }


def _payload_from_record(record: dict, received_at: str) -> dict:
    certificate = record.get("certificate") if isinstance(record.get("certificate"), dict) else {}
    certificate_id = _text(
        certificate.get("certificate_id") or record.get("certificate_id")
    )
    serial_no = _text(certificate.get("serial_no") or record.get("system_serial"))
    wipe_method = _text(
        certificate.get("wipe_method") or record.get("wipe_method")
    )
    wipe_standard = _text(
        certificate.get("wipe_standard") or record.get("wipe_standard")
    )
    duration_sec = certificate.get("duration_sec")
    if duration_sec in (None, ""):
        duration_sec = record.get("duration_sec") or 0
    erase = {
        "ok": bool(record.get("erase_ok", record.get("ok", False))),
        "verified": bool(record.get("erase_verified", False)),
        "method": wipe_method,
        "wipe_method": wipe_method,
        "wipe_standard": wipe_standard,
        "device": _text(certificate.get("device")),
        "device_model": _text(certificate.get("device_model") or record.get("device_model")),
        "duration_sec": duration_sec,
        "certificate_id": certificate_id,
        "secure_erase_reg_id": certificate_id,
        "verification_hash": _text(
            certificate.get("verification_hash") or record.get("verification_hash")
        ),
        "certificate_status": _text(
            certificate.get("certificate_status") or record.get("certificate_status")
        ),
        "remote_post_ok": bool(record.get("remote_post_ok", certificate.get("remote_post_ok", False))),
        "remote_error": _text(certificate.get("remote_error")),
        "drive_fingerprint": _text(record.get("drive_fingerprint")),
        "record": record,
        "certificate": certificate,
    }
    return {
        "schema": "vstl_report_secure_erase_backfill_v1",
        "report_source": "secure_erase_authorization_backfill",
        "session_started_at": received_at,
        "technician_level": _text(certificate.get("technician_level")),
        "bench_id": _text(certificate.get("bench_id")),
        "serial_no": serial_no,
        "sku": "",
        "mac_id": _text(certificate.get("mac_id")),
        "brand": _text(certificate.get("brand")),
        "model": _text(certificate.get("model")),
        "status": "completed" if erase["ok"] else "failed",
        "selected_option": 3,
        "selected_option_label": "Certified Secure Erase",
        "secure_erase_reg_id": certificate_id,
        "raw_data": {
            "storage": {
                "drive_count": 1,
                "drives": [_drive_row(record, certificate)],
            }
        },
        "phase3": {"erase": erase},
    }


def import_records(records_root: Path, data_path: Path) -> int:
    if not records_root.exists():
        return 0
    existing = _existing_keys(data_path)
    pending = []
    for path in sorted(records_root.glob("*.json"), key=lambda item: item.stat().st_mtime):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not (record.get("erase_ok") or record.get("ok")):
            continue
        certificate = record.get("certificate") if isinstance(record.get("certificate"), dict) else {}
        keys = {
            _text(certificate.get("certificate_id") or record.get("certificate_id")),
            _text(record.get("drive_fingerprint")),
        }
        keys.discard("")
        if keys and keys.intersection(existing):
            continue
        received_at = _iso_from_mtime(path)
        payload = _payload_from_record(record, received_at)
        pending.append({"received_at": received_at, "payload": payload})
        existing.update(keys)
    if not pending:
        return 0
    data_path.parent.mkdir(parents=True, exist_ok=True)
    with data_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        for item in pending:
            handle.write(json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return len(pending)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records-root", default="/images/dev/.vstl-secure-erase")
    parser.add_argument("--data", default="/var/lib/vstl-reports/audits.jsonl")
    args = parser.parse_args()
    count = import_records(Path(args.records_root), Path(args.data))
    print(f"imported={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
