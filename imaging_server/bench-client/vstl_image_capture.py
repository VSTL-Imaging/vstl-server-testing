"""
vstl_image_capture.py â€” Phase 3 Capture Full System Image
==========================================================
Mounts the configured NFS golden-copy share, runs Clonezilla's ocs-sr
in ``savedisk`` mode to capture the SUT's primary disk as a partclone
image, computes the SHA-256 of the result, and posts back to the
backend so the captured image is registered as a new (or updated)
golden-copy entry.

Design choice 2a: image storage is an NFS export from the FOG server.
The NFS path comes from ``GET /api/imaging/bench-settings`` so admins
can change it without redeploying the bench.

Result schema
-------------
{
  "ok": True,
  "image_name":  "DELL_LATITUDE-5500_DL-LAT-5500-A1_2026-05-18T10-30-00Z.img",
  "image_path":  "/mnt/vstl-golden/DELL_LATITUDE-5500_..."
  "image_size_gb": 42.5,
  "image_sha256":  "abc123...",
  "started_at": "ISO-8601",
  "completed_at": "ISO-8601",
  "duration_sec": int,
  "device":     "/dev/nvme0n1",
  "evidence":   "ocs-sr output excerpt, capped at 6 KB",
  "error_message": ""           # populated on failure
}

Caller is responsible for already having an active NFS settings doc on
the backend (``PUT /api/imaging/system-settings``).
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional


_EVIDENCE_CAP = 6000
_NFS_MOUNT_POINT = "/home/partimag"
_CAPTURE_META_FILE = "vstl_capture_metadata.json"
_SECURE_ERASE_RECORD_DIR = ".vstl-secure-erase"
_WINDOWS_MOUNT_POINT = "/mnt/vstl-capture-os"
_BAD_SKU_VALUES = {"", "UNKNOWN", "UNKNOWN_SKU", "NOPN", "NONE", "N/A", "NA"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_compact() -> str:
    """Compact UTC timestamp safe for filenames: 2026-05-18T10-30-00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    try:
        rc = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=timeout, check=False)
        return rc.returncode, rc.stdout or "", rc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired as e:
        return 124, "", f"timeout after {e.timeout}s"
    except OSError as e:
        return 1, "", f"OSError: {e}"


def _safe_filename_token(s: str) -> str:
    s = (s or "").strip().upper().replace(" ", "_")
    return re.sub(r"[^A-Z0-9_\-]", "_", s) or "UNKNOWN"


def _normalize_hardware_id(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").strip().upper())


def secure_erase_gate_identity(system_serial: str, drive: dict) -> dict:
    """Build the exact machine+drive identity used by the capture gate."""
    normalized_system_serial = _normalize_hardware_id(system_serial)
    drive_wwn = _normalize_hardware_id(str(drive.get("device_wwn") or ""))
    drive_serial = _normalize_hardware_id(str(drive.get("device_serial") or ""))
    stable_drive_id = drive_wwn or drive_serial
    issues = []
    if not normalized_system_serial or normalized_system_serial in {"UNKNOWN", "NONE", "NA"}:
        issues.append("Laptop serial number is unavailable.")
    if not stable_drive_id:
        issues.append("Storage serial/WWN is unavailable; exact drive cannot be verified.")

    canonical = "|".join([normalized_system_serial, stable_drive_id])
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest() if not issues else ""
    return {
        "ok": not issues,
        "issues": issues,
        "system_serial": normalized_system_serial,
        "drive_serial": drive_serial,
        "drive_wwn": drive_wwn,
        "stable_drive_id": stable_drive_id,
        "drive_fingerprint": fingerprint,
        "device_type": str(drive.get("device_type") or ""),
        "device_model": str(drive.get("device_model") or ""),
        "device_size_bytes": int(drive.get("device_size_bytes") or 0),
    }


def _secure_erase_record_path(
    system_serial: str,
    drive: dict,
    repository_root: Optional[str] = None,
) -> tuple[str, dict]:
    identity = secure_erase_gate_identity(system_serial, drive)
    root = repository_root or _NFS_MOUNT_POINT
    if not identity["ok"]:
        return "", identity
    filename = f"{identity['drive_fingerprint']}.json"
    return os.path.join(root, _SECURE_ERASE_RECORD_DIR, filename), identity


def _record_matches_secure_erase_identity(record: dict, identity: dict) -> tuple[bool, str]:
    """Return whether a persisted erase record matches this laptop and drive.

    Some live boots expose both NVMe WWN/EUI and serial, while another boot may
    expose only one of them. The gate still requires the same laptop serial and
    verified erase record, but accepts either stable storage identifier.
    """
    if record.get("schema") != "vstl_secure_erase_authorization_v1":
        return False, "schema"
    if record.get("system_serial") != identity["system_serial"]:
        return False, "system_serial"
    if record.get("erase_ok") is not True or record.get("erase_verified") is not True:
        return False, "erase_status"

    record_fingerprint = _normalize_hardware_id(str(record.get("drive_fingerprint") or ""))
    record_stable = _normalize_hardware_id(str(record.get("stable_drive_id") or ""))
    record_serial = _normalize_hardware_id(str(record.get("drive_serial") or ""))
    record_wwn = _normalize_hardware_id(str(record.get("drive_wwn") or ""))

    current_fingerprint = _normalize_hardware_id(str(identity.get("drive_fingerprint") or ""))
    current_stable = _normalize_hardware_id(str(identity.get("stable_drive_id") or ""))
    current_serial = _normalize_hardware_id(str(identity.get("drive_serial") or ""))
    current_wwn = _normalize_hardware_id(str(identity.get("drive_wwn") or ""))

    checks = (
        ("drive_fingerprint", record_fingerprint, current_fingerprint),
        ("stable_drive_id", record_stable, current_stable),
        ("drive_serial", record_serial, current_serial),
        ("drive_wwn", record_wwn, current_wwn),
    )
    for label, left, right in checks:
        if left and right and left == right:
            return True, label
    return False, "drive_identity"


def _find_matching_secure_erase_record(record_dir: str, identity: dict) -> tuple[str, dict, str]:
    """Scan authorization records for a compatible same-laptop drive record."""
    try:
        names = os.listdir(record_dir)
    except OSError:
        return "", {}, ""
    for name in sorted(names):
        if not name.endswith(".json"):
            continue
        candidate = os.path.join(record_dir, name)
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        matched, matched_by = _record_matches_secure_erase_identity(record, identity)
        if matched:
            return candidate, record, matched_by
    return "", {}, ""


def write_secure_erase_record(
    system_serial: str,
    drive: dict,
    erase_result: dict,
    certificate: Optional[dict] = None,
    certificate_ok: bool = False,
    repository_root: Optional[str] = None,
) -> dict:
    """Persist an exact machine+drive erase authorization on the NFS server."""
    path, identity = _secure_erase_record_path(system_serial, drive, repository_root)
    if not identity["ok"]:
        return {"ok": False, "issues": identity["issues"], "identity": identity}
    if not erase_result.get("ok") or not erase_result.get("verified"):
        return {
            "ok": False,
            "issues": ["Secure erase must complete successfully and pass verification."],
            "identity": identity,
        }

    record = {
        "schema": "vstl_secure_erase_authorization_v1",
        **identity,
        "erase_ok": True,
        "erase_verified": True,
        "wipe_method": str(erase_result.get("method") or ""),
        "wipe_standard": str((certificate or {}).get("wipe_standard") or erase_result.get("standard") or ""),
        "wipe_started_at": str(erase_result.get("started_at") or ""),
        "wipe_completed_at": str(erase_result.get("completed_at") or ""),
        "certificate_ok": bool(certificate_ok),
        "certificate_id": str((certificate or {}).get("certificate_id") or ""),
        "verification_hash": str((certificate or {}).get("verification_hash") or ""),
        "certificate_status": str((certificate or {}).get("certificate_status") or ""),
        "remote_post_ok": bool((certificate or {}).get("remote_post_ok")),
        "remote_certificate_id": str((certificate or {}).get("remote_certificate_id") or ""),
        "certificate": certificate or {},
        "recorded_at": _now_iso(),
    }
    record_dir = os.path.dirname(path)
    temp_path = f"{path}.tmp-{uuid.uuid4().hex}"
    try:
        os.makedirs(record_dir, mode=0o755, exist_ok=True)
        os.chmod(record_dir, 0o755)
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o644)
        return {"ok": True, "path": path, "identity": identity, "record": record}
    except OSError as exc:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        return {
            "ok": False,
            "issues": [f"Could not store secure-erase authorization: {exc}"],
            "identity": identity,
        }


def check_secure_erase_record(
    system_serial: str,
    drive: dict,
    repository_root: Optional[str] = None,
) -> dict:
    """Validate the persistent erase record for this exact machine and drive."""
    path, identity = _secure_erase_record_path(system_serial, drive, repository_root)
    if not identity["ok"]:
        return {"ok": False, "issues": identity["issues"], "identity": identity}
    try:
        with open(path, "r", encoding="utf-8") as f:
            record = json.load(f)
    except FileNotFoundError:
        fallback_path, fallback_record, matched_by = _find_matching_secure_erase_record(
            os.path.dirname(path), identity
        )
        if fallback_record:
            return {
                "ok": True,
                "issues": [],
                "identity": identity,
                "path": fallback_path,
                "record": fallback_record,
                "matched_by": matched_by,
                "expected_path": path,
            }
        return {
            "ok": False,
            "issues": [
                "No certified secure-erase record exists for this laptop serial and storage device."
            ],
            "identity": identity,
            "path": path,
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "issues": [f"Secure-erase record is unreadable: {exc}"],
            "identity": identity,
            "path": path,
        }

    valid, matched_by = _record_matches_secure_erase_identity(record, identity)
    if not valid:
        return {
            "ok": False,
            "issues": ["Secure-erase record does not match this exact laptop and storage device."],
            "identity": identity,
            "path": path,
            "record": record,
        }
    return {
        "ok": True,
        "issues": [],
        "identity": identity,
        "path": path,
        "record": record,
        "matched_by": matched_by,
    }


def _clean_os_name(value: str) -> str:
    value = " ".join((value or "").replace("\x00", " ").split())
    value = re.sub(r"^Microsoft\s+", "", value, flags=re.I)
    value = re.sub(r"\bProfessional\b", "Pro", value, flags=re.I)
    return value or "Windows"


def _clean_os_version(value: str) -> str:
    value = " ".join((value or "").replace("\x00", " ").split())
    match = re.fullmatch(r"([0-9]{2})h([12])", value, flags=re.I)
    if match:
        return f"{match.group(1)}H{match.group(2)}"
    return value


def _os_filename_token(os_name: str, os_version: str = "", os_build: str = "") -> str:
    name = _clean_os_name(os_name)
    name = re.sub(r"\bWindows\b", "Win", name, flags=re.I)
    version = _clean_os_version(os_version or os_build or "Unknown")
    token = "_".join([name, version]).strip("_")
    token = token.replace(".", "_").replace("-", "_")
    return re.sub(r"[^A-Za-z0-9_]+", "_", token).strip("_") or "Win_Unknown"


def _safe_filename_token_mixed(s: str) -> str:
    s = (s or "").strip().replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9_\-]", "_", s).strip("_") or "Unknown"


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


_OEM_RECOVERY_TOKENS = {
    "dellsupport",
    "dellrestore",
    "dellutility",
    "supportassist",
    "factoryimage",
    "pbrimage",
    "recoveryimage",
    "restore",
    "lenovorecovery",
    "lenovopart",
}

_SMALL_OEM_IMAGE_MAX_BYTES = 64 * 1024 * 1024 * 1024


def _run_json(cmd: list[str], timeout: int = 30) -> tuple[bool, dict, str]:
    rc, out, err = _run(cmd, timeout=timeout)
    if rc != 0:
        return False, {}, f"rc={rc} stdout={out[:300]} stderr={err[:300]}"
    try:
        return True, json.loads(out), ""
    except json.JSONDecodeError as e:
        return False, {}, f"json parse failed: {e}; output={out[:300]}"


def _children(node: dict) -> list[dict]:
    return node.get("children") or []


def _find_disk_tree(device: str) -> tuple[bool, dict, str]:
    ok, data, err = _run_json(
        ["lsblk", "-Jbo", "NAME,PATH,TYPE,SIZE,FSTYPE,PARTTYPE,PARTLABEL,LABEL,MOUNTPOINTS"],
        timeout=20,
    )
    if not ok:
        return False, {}, err
    want = os.path.realpath(device or "")
    for disk in data.get("blockdevices", []):
        if disk.get("type") != "disk":
            continue
        if os.path.realpath(disk.get("path") or "") == want:
            return True, disk, ""
    return False, {}, f"{device} not found in lsblk disk list"


def _part_role(part: dict) -> str:
    label_text = _norm_text(" ".join([
        str(part.get("partlabel") or ""),
        str(part.get("label") or ""),
    ]))
    text = _norm_text(" ".join([
        str(part.get("fstype") or ""),
        str(part.get("parttype") or ""),
        label_text,
    ]))
    parttype = (part.get("parttype") or "").lower()
    fstype = (part.get("fstype") or "").lower()
    size = int(part.get("size") or 0)
    if parttype == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b" or fstype in {"vfat", "fat", "fat32"} or "efi" in text:
        return "efi"
    if parttype == "e3c9e316-0b5c-4db8-817d-f92df00215ae" or "microsoftreserved" in text or text == "msr":
        return "msr"
    if (
        parttype == "de94bba4-06d1-4d40-a16a-bfd50179d6ac"
        or "recovery" in text
        or "winre" in text
        or any(token in label_text for token in _OEM_RECOVERY_TOKENS)
        or (label_text == "image" and 0 < size <= _SMALL_OEM_IMAGE_MAX_BYTES)
    ):
        return "recovery"
    if fstype == "ntfs":
        return "windows"
    return "extra"


def validate_partition_layout(device: str) -> dict:
    """Enforce the capture policy for a clean Windows source disk.

    EFI/MSR system partitions are allowed because they are normal on a
    fresh UEFI Windows installation. WinRE and known OEM recovery/support
    NTFS partitions (for example DellSupport and small Dell Image volumes)
    are also allowed. Extra user data partitions still block capture.
    A missing WinRE partition is recorded as a warning only; some approved
    source images are captured without Windows Recovery enabled.
    """
    ok, disk, err = _find_disk_tree(device)
    if not ok:
        return {"ok": False, "issues": [f"Cannot inspect partitions: {err}"], "partitions": []}

    issues: list[str] = []
    warnings: list[str] = []
    parts: list[dict] = []
    windows_parts: list[dict] = []
    recovery_parts: list[dict] = []
    extras: list[dict] = []
    for p in _children(disk):
        if p.get("type") != "part":
            continue
        role = _part_role(p)
        row = {
            "path": p.get("path") or "",
            "size": p.get("size") or 0,
            "fstype": p.get("fstype") or "",
            "label": p.get("label") or "",
            "partlabel": p.get("partlabel") or "",
            "parttype": p.get("parttype") or "",
            "role": role,
        }
        parts.append(row)
        if role == "windows":
            windows_parts.append(row)
        elif role == "recovery":
            recovery_parts.append(row)
        elif role == "extra":
            extras.append(row)

    if not windows_parts:
        issues.append("No Windows C: NTFS partition was detected.")
    if len(windows_parts) > 1:
        issues.append("More than one Windows/data NTFS partition was detected.")
    if not recovery_parts:
        warnings.append("No Windows recovery/WinRE partition was detected.")
    if extras:
        labels = ", ".join(f"{p['path']}({p['fstype'] or p['partlabel'] or 'unknown'})" for p in extras)
        issues.append(f"Extra unsupported partition(s) detected: {labels}.")

    os_part = max(windows_parts, key=lambda p: int(p.get("size") or 0), default={})
    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "partitions": parts,
        "os_partition": os_part.get("path", ""),
        "recovery_count": len(recovery_parts),
    }


def _mount_windows_readonly(partition: str) -> tuple[bool, str]:
    os.makedirs(_WINDOWS_MOUNT_POINT, exist_ok=True)
    _run(["umount", _WINDOWS_MOUNT_POINT], timeout=10)
    attempts = [
        ["mount", "-o", "ro,noload", partition, _WINDOWS_MOUNT_POINT],
        ["mount", "-t", "ntfs3", "-o", "ro", partition, _WINDOWS_MOUNT_POINT],
        ["ntfs-3g", "-o", "ro", partition, _WINDOWS_MOUNT_POINT],
    ]
    evidence = []
    for cmd in attempts:
        rc, out, err = _run(cmd, timeout=25)
        evidence.append(f"$ {' '.join(cmd)} rc={rc} {out[:120]} {err[:180]}")
        if rc == 0:
            return True, "\n".join(evidence)
    return False, "\n".join(evidence)


def _umount_windows() -> None:
    _run(["umount", _WINDOWS_MOUNT_POINT], timeout=10)


def _windows_tree_issues(root: str) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    evidence: list[str] = []
    windows_dir = os.path.join(root, "Windows")
    users_dir = os.path.join(root, "Users")
    if not os.path.isdir(windows_dir):
        issues.append("Windows directory was not found on the OS partition.")
    hiber = os.path.join(root, "hiberfil.sys")
    if os.path.exists(hiber):
        evidence.append(
            "hiberfil.sys=present (non-blocking; PXE boot is independent of Windows Fast Startup/hibernation, "
            "and Clonezilla capture is run with -rm-win-swap-hib)"
        )

    allowed_profiles = {
        "all users", "default", "default user", "public",
        "desktop.ini", "defaultuser0", "administrator", "guest",
        "defaultaccount", "wdagutilityaccount",
    }
    try:
        profiles = [n for n in os.listdir(users_dir)] if os.path.isdir(users_dir) else []
    except OSError:
        profiles = []
    real_profiles = [
        n for n in profiles
        if _norm_text(n) not in {_norm_text(x) for x in allowed_profiles}
    ]
    if real_profiles:
        issues.append("Custom user profile folder(s) exist; capture requires a clean/OOBE Windows state: " + ", ".join(real_profiles[:6]))
    evidence.append("profiles=" + ",".join(profiles[:20]))

    browser_hits: list[str] = []
    for base in real_profiles:
        profile_root = os.path.join(users_dir, base, "AppData", "Local")
        for rel in (
            os.path.join("Google", "Chrome", "User Data"),
            os.path.join("Microsoft", "Edge", "User Data"),
        ):
            candidate = os.path.join(profile_root, rel)
            if not os.path.isdir(candidate):
                continue
            for dirpath, _dirs, files in os.walk(candidate):
                if "Login Data" in files or "Cookies" in files:
                    browser_hits.append(os.path.relpath(dirpath, root))
                    if len(browser_hits) >= 5:
                        break
                if len(browser_hits) >= 5:
                    break
    if browser_hits:
        issues.append("Browser session/credential artifacts found: " + ", ".join(browser_hits))

    sam = os.path.join(root, "Windows", "System32", "config", "SAM")
    if os.path.exists(sam):
        if shutil.which("chntpw"):
            rc, out, err = _run(["chntpw", "-l", sam], timeout=20)
            evidence.append(("chntpw rc=%s\n%s\n%s" % (rc, out[-1500:], err[-500:]))[:2200])
            if rc != 0:
                issues.append("Could not verify Windows local accounts with chntpw.")
        else:
            issues.append("Cannot verify Windows local account passwords because chntpw is missing.")
    else:
        issues.append("Windows SAM hive was not found; cannot verify local accounts.")
    return issues, evidence


def _registry_string_lines(path: str) -> list[str]:
    for cmd in (["strings", "-el", path], ["strings", path]):
        rc, out, _err = _run(cmd, timeout=30)
        if rc == 0 and out.strip():
            return [line.strip().replace("\x00", "") for line in out.splitlines() if line.strip()]
    return []


def _parse_reg_export_values(text: str, section: str = "") -> dict[str, str]:
    def _hex_to_string(raw_value: str) -> str:
        try:
            _kind, payload = raw_value.split(":", 1)
        except ValueError:
            return ""
        hex_text = re.sub(r"[^0-9a-fA-F]", "", payload)
        if len(hex_text) < 2:
            return ""
        try:
            data = bytes.fromhex(hex_text)
        except ValueError:
            return ""
        for encoding in ("utf-16le", "utf-8", "latin-1"):
            try:
                value = data.decode(encoding, errors="ignore")
            except LookupError:
                continue
            value = value.replace("\x00", "").strip()
            if value and sum(ch.isprintable() for ch in value) >= max(1, len(value) - 1):
                return value
        return ""

    logical_lines: list[str] = []
    pending = ""
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if pending:
            line = pending + line.lstrip()
        if line.endswith("\\"):
            pending = line[:-1]
            continue
        logical_lines.append(line)
        pending = ""
    if pending:
        logical_lines.append(pending)

    section_norm = section.strip().strip("[]").lower()
    in_section = not section_norm
    values: dict[str, str] = {}
    for raw_line in logical_lines:
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            header = line.strip("[]").lower()
            if section_norm:
                if in_section and header != section_norm:
                    break
                in_section = header == section_norm
            continue
        if not in_section:
            continue
        match = re.match(r'"([^"]+)"=(.+)$', line)
        if not match:
            continue
        name, raw_value = match.group(1), match.group(2).strip()
        if raw_value.startswith('"') and raw_value.endswith('"'):
            value = raw_value[1:-1]
            value = value.replace(r"\\", "\\").replace(r"\"", '"')
            values[name] = value.replace("\x00", "").strip()
        elif raw_value.lower().startswith("hex"):
            values[name] = _hex_to_string(raw_value)
    return {k: v for k, v in values.items() if v}


def _current_version_registry_values(software_hive: str) -> tuple[dict[str, str], str]:
    """Read exact HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion values.

    The previous fallback scanned the entire SOFTWARE hive with strings. That
    can pick up unrelated ProductName/EditionID values and mislabel Pro images
    as Enterprise. reged is shipped in the live rootfs via chntpw, so use it
    first for the exact key Windows Settings uses.
    """
    candidates = []
    found = shutil.which("reged")
    if found:
        candidates.append(found)
    candidates.extend(["/usr/sbin/reged", "/sbin/reged", "/usr/bin/reged"])

    seen: set[str] = set()
    key_forms = (
        r"\Microsoft\Windows NT\CurrentVersion",
        r"Microsoft\Windows NT\CurrentVersion",
        r"\\Microsoft\\Windows NT\\CurrentVersion",
    )
    for exe in candidates:
        if not exe or exe in seen:
            continue
        seen.add(exe)
        if not os.path.exists(exe):
            continue
        for key in key_forms:
            out_path = os.path.join(
                tempfile.gettempdir(), f"vstl-currentversion-{uuid.uuid4().hex}.reg"
            )
            try:
                rc, out, err = _run([
                    exe,
                    "-x",
                    software_hive,
                    r"HKEY_LOCAL_MACHINE\SOFTWARE",
                    key,
                    out_path,
                ], timeout=30)
                if not os.path.exists(out_path):
                    continue
                raw = b""
                try:
                    with open(out_path, "rb") as f:
                        raw = f.read()
                except OSError:
                    raw = b""
                text = ""
                for encoding in ("utf-16", "utf-16le", "utf-8", "latin-1"):
                    try:
                        text = raw.decode(encoding, errors="ignore")
                    except LookupError:
                        continue
                    if "CurrentVersion" in text or "ProductName" in text or "EditionID" in text:
                        break
                values = _parse_reg_export_values(
                    text,
                    section=r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows NT\CurrentVersion",
                )
                if values:
                    return values, f"reged exact CurrentVersion via {exe} key={key} rc={rc}"
            except OSError:
                continue
            finally:
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
    return {}, "reged exact CurrentVersion unavailable"


def _value_after_key(lines: list[str], key: str) -> str:
    key_l = key.lower()
    stop_keys = {
        "productname", "displayversion", "releaseid", "currentbuild",
        "currentbuildnumber", "editionid", "compositioneditionid",
        "installationtype", "productid",
    }
    for idx, line in enumerate(lines):
        if line.strip().lower() != key_l:
            continue
        for candidate in lines[idx + 1: idx + 10]:
            value = candidate.strip()
            if not value or value.lower() in stop_keys:
                continue
            return value
    return ""


def _first_regex_value(lines: list[str], pattern: str) -> str:
    rx = re.compile(pattern, re.I)
    for line in lines:
        match = rx.search(line.strip())
        if match:
            return match.group(1).strip() if match.groups() else line.strip()
    return ""


def _edition_suffix_from_text(value: str) -> str:
    text = " ".join((value or "").replace("\x00", " ").split()).lower()
    if "workstation" in text:
        return " Pro for Workstations"
    if "enterprise" in text:
        return " Enterprise"
    if "education" in text:
        return " Education"
    if re.search(r"\bpro(?:fessional)?\b", text):
        return " Pro"
    if "home" in text or re.search(r"\bcore(?:singlelanguage)?\b", text):
        return " Home"
    return ""


def _edition_suffix_from_product_id(product_id: str) -> str:
    """Map conservative Windows Product ID patterns seen in Settings/About.

    The reported Latitude shows Product ID 00355-...-AAOEM. That prefix/channel
    is used by OEM Windows Pro installs, while Enterprise is not an OEM
    preinstall edition. Use it only as a narrow override.
    """
    pid = (product_id or "").strip().upper()
    if re.match(r"^00355-\d{5}-\d{5}-AAOEM$", pid):
        return " Pro"
    return ""


def _edition_suffix(
    product: str = "",
    edition: str = "",
    product_id: str = "",
    prefer_edition: bool = False,
) -> str:
    """Return the Windows edition suffix, trusting ProductName first.

    Raw SOFTWARE hive string scans can find unrelated Enterprise values near
    EditionID/CompositionEditionID. ProductName is what Windows Settings shows,
    so if it already says Pro/Home/Enterprise we do not let a loose EditionID
    scan override it.
    """
    pid_suffix = _edition_suffix_from_product_id(product_id)
    if pid_suffix:
        return pid_suffix
    if prefer_edition:
        return _edition_suffix_from_text(edition) or _edition_suffix_from_text(product)
    return _edition_suffix_from_text(product) or _edition_suffix_from_text(edition)


def _windows_release_from_build(build: str) -> str:
    try:
        build_no = int(re.sub(r"\D", "", build or "0") or "0")
    except ValueError:
        build_no = 0
    # Windows client release mapping. Use floor thresholds so minor CUs still map.
    for floor, release in (
        (26200, "25H2"),
        (26100, "24H2"),
        (22631, "23H2"),
        (22621, "22H2"),
        (22000, "21H2"),
        (19045, "22H2"),
        (19044, "21H2"),
        (19043, "21H1"),
        (19042, "20H2"),
        (19041, "2004"),
    ):
        if build_no >= floor:
            return release
    return ""


def _normalize_windows_product(
    product: str,
    edition: str,
    build: str,
    product_id: str = "",
    prefer_edition: bool = False,
) -> str:
    product = _clean_os_name(product or "")
    try:
        build_no = int(re.sub(r"\D", "", build or "0") or "0")
    except ValueError:
        build_no = 0
    suffix = _edition_suffix(
        product, edition, product_id=product_id, prefer_edition=prefer_edition
    )
    product_l = product.lower()
    if (not product or product == "Windows") and build_no >= 22000:
        return ("Windows 11" + suffix).strip()
    if (not product or product == "Windows") and build_no:
        return ("Windows 10" + suffix).strip()

    normalized = product
    if "windows 10" in product_l and build_no >= 22000:
        normalized = re.sub(r"\bWindows\s+10\b", "Windows 11", product, flags=re.I)

    normalized_suffix = _edition_suffix_from_text(normalized)
    if suffix and (not normalized_suffix or (prefer_edition and normalized_suffix != suffix)):
        base = re.sub(
            r"\s+(pro for workstations|home|pro|professional|enterprise|education).*$",
            "",
            normalized,
            flags=re.I,
        )
        return (base + suffix).strip()
    return normalized or "Windows"


def detect_windows_os_info(root: str) -> dict:
    """Best-effort Windows edition/version from the offline SOFTWARE hive."""
    software = os.path.join(root, "Windows", "System32", "config", "SOFTWARE")
    info = {"os_name": "Windows", "os_version": "Unknown", "os_build": "", "evidence": ""}
    if not os.path.exists(software):
        info["evidence"] = "SOFTWARE hive not found"
        return info
    values, source = _current_version_registry_values(software)
    lines: list[str] = []
    prefer_edition = bool(values)
    if values:
        product = values.get("ProductName", "")
        version = _clean_os_version(values.get("DisplayVersion", "") or values.get("ReleaseId", ""))
        build = values.get("CurrentBuild", "") or values.get("CurrentBuildNumber", "")
        product_id = values.get("ProductId", "")
        edition = (
            values.get("EditionID", "")
            or values.get("CompositionEditionID", "")
            or values.get("ProductSuite", "")
        )
    else:
        lines = _registry_string_lines(software)
        if not lines:
            info["evidence"] = "SOFTWARE hive strings unavailable"
            return info
        product = (
            _value_after_key(lines, "ProductName")
            or _first_regex_value(lines, r"\b(Windows\s+(?:10|11)(?:\s+[A-Za-z0-9 /]+)?)\b")
        )
        version = _clean_os_version(
            _value_after_key(lines, "DisplayVersion")
            or _value_after_key(lines, "ReleaseId")
            or _first_regex_value(lines, r"\b(2[0-9]H[12])\b")
        )
        build = (
            _value_after_key(lines, "CurrentBuild")
            or _value_after_key(lines, "CurrentBuildNumber")
        )
        product_id = _value_after_key(lines, "ProductId")
        edition = _value_after_key(lines, "EditionID") or _value_after_key(lines, "CompositionEditionID")
        source = "strings whole SOFTWARE fallback"

    if not product and not edition and not build:
        info["evidence"] = "SOFTWARE hive strings unavailable"
        return info

    raw_product = product
    raw_edition = edition
    raw_product_id = product_id
    product = _normalize_windows_product(
        product, edition, build, product_id=product_id, prefer_edition=prefer_edition
    )
    if not version:
        version = _windows_release_from_build(build)

    info.update({
        "os_name": product,
        "os_version": version or build or "Unknown",
        "os_build": build,
        "evidence": (
            f"Source={source}; RawProductName={raw_product}; RawEditionID={raw_edition}; "
            f"ProductId={raw_product_id}; ProductName={product}; DisplayVersion={version}; Build={build}"
        ),
    })
    return info


def inspect_installed_windows(device: str) -> dict:
    """Read the installed Windows edition/version without changing the disk."""
    result = {
        "os_name": "Unknown",
        "os_version": "Unknown",
        "os_build": "",
        "evidence": "",
    }
    if not device:
        result["evidence"] = "no internal disk supplied"
        return result
    layout = validate_partition_layout(device)
    partition = layout.get("os_partition") or ""
    if not partition:
        result["evidence"] = "; ".join(layout.get("issues") or ["Windows partition not found"])
        return result
    mounted, evidence = _mount_windows_readonly(partition)
    if not mounted:
        result["evidence"] = evidence
        return result
    try:
        return detect_windows_os_info(_WINDOWS_MOUNT_POINT)
    finally:
        _umount_windows()


def validate_capture_readiness(
    device: str,
    ident: dict,
    cpu_model: str = "",
    lock_audit: Optional[dict] = None,
) -> dict:
    """Strict pre-capture gate from the Server Process document."""
    issues: list[str] = []
    warnings: list[str] = []
    evidence: list[str] = []
    sku = (ident.get("sku") or ident.get("part_number") or "").strip()
    if sku.upper() in _BAD_SKU_VALUES:
        issues.append("SKU/Unit Part Number not detected. Cannot store captured image.")

    audit_locks = set((lock_audit or {}).get("detected_locks") or [])
    blocking_locks = audit_locks.intersection({"intune", "azure_ad", "vendor_mdm", "bios_password", "drive_password"})
    if blocking_locks:
        issues.append("Lock/MDM/encryption signal still present: " + ", ".join(sorted(blocking_locks)))

    layout = validate_partition_layout(device)
    issues.extend(layout.get("issues") or [])
    warnings.extend(layout.get("warnings") or [])
    os_info: dict = {"os_name": "Windows", "os_version": "Unknown", "os_build": "", "evidence": ""}

    os_part = layout.get("os_partition") or ""
    if os_part:
        mounted, mount_ev = _mount_windows_readonly(os_part)
        evidence.append(mount_ev)
        if mounted:
            try:
                os_info = detect_windows_os_info(_WINDOWS_MOUNT_POINT)
                if os_info.get("evidence"):
                    evidence.append("OS: " + os_info.get("evidence", ""))
                win_issues, win_evidence = _windows_tree_issues(_WINDOWS_MOUNT_POINT)
                issues.extend(win_issues)
                evidence.extend(win_evidence)
            finally:
                _umount_windows()
        else:
            issues.append("Could not mount Windows C: partition read-only for password/profile checks.")

    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "device": device,
        "sku": sku,
        "model": ident.get("model", ""),
        "brand": ident.get("brand", ""),
        "cpu": cpu_model,
        "os_info": os_info,
        "partition_layout": layout,
        "evidence": "\n---\n".join(evidence)[:_EVIDENCE_CAP],
    }


def mount_nfs(nfs_host: str, nfs_share: str,
               mount_options: str = "rw,nolock,vers=3") -> tuple[bool, str]:
    """Mount the NFS golden-copy share at ``_NFS_MOUNT_POINT``.

    Idempotent: if it's already mounted we return success.
    """
    os.makedirs(_NFS_MOUNT_POINT, exist_ok=True)
    # Already mounted?
    rc, out, _ = _run(["mountpoint", "-q", _NFS_MOUNT_POINT], timeout=5)
    if rc == 0:
        return True, f"{_NFS_MOUNT_POINT} already mounted"

    src = f"{nfs_host}:{nfs_share}"
    rc, out, err = _run(
        ["mount", "-t", "nfs", "-o", mount_options, src, _NFS_MOUNT_POINT],
        timeout=30,
    )
    if rc != 0:
        return False, f"mount failed: rc={rc} stdout={out[:200]} stderr={err[:400]}"
    # Verify the repository is really writable before handing control to ocs-sr.
    # Without this, Clonezilla falls back to its blue choose-mode screen and no
    # VSTL image is captured.
    probe = os.path.join(_NFS_MOUNT_POINT, f".vstl_capture_write_test_{uuid.uuid4().hex}")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok\n")
        os.unlink(probe)
    except OSError as e:
        return False, f"mounted {src}, but write test failed at {_NFS_MOUNT_POINT}: {e}"

    return True, f"mounted {src} -> {_NFS_MOUNT_POINT}; write test OK"


def umount_nfs() -> None:
    """Best-effort unmount; never raises."""
    _run(["umount", _NFS_MOUNT_POINT], timeout=15)


def _sha256_file(path: str) -> str:
    """Stream the file through SHA-256. Returns hex digest or '' on error."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _fmt_bytes(num: Optional[float]) -> str:
    if num is None:
        return ""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TB"


_VIRTUAL_NET_PREFIXES = (
    "br-", "docker", "dummy", "podman", "tap", "tailscale",
    "tun", "veth", "virbr", "vmnet", "wg", "zt",
)


def _network_byte_totals(sys_class_net: str = "/sys/class/net") -> dict:
    """Return aggregate counters for usable, link-up bench interfaces.

    Clonezilla writes capture data to NFS (TX) and reads restore data from
    NFS (RX). Kernel interface counters therefore provide a reliable live
    rate even when partclone omits its optional ``Rate:`` status line.
    """
    totals = {"rx": 0, "tx": 0, "interfaces": []}
    try:
        names = sorted(os.listdir(sys_class_net))
    except OSError:
        return totals

    for name in names:
        lower = name.lower()
        if lower == "lo" or lower.startswith(_VIRTUAL_NET_PREFIXES):
            continue
        base = os.path.join(sys_class_net, name)
        try:
            with open(os.path.join(base, "operstate"), "r", encoding="ascii") as f:
                operstate = f.read().strip().lower()
            if operstate not in {"up", "unknown"}:
                continue
            with open(os.path.join(base, "statistics", "rx_bytes"), "r", encoding="ascii") as f:
                rx_bytes = int(f.read().strip())
            with open(os.path.join(base, "statistics", "tx_bytes"), "r", encoding="ascii") as f:
                tx_bytes = int(f.read().strip())
        except (OSError, ValueError):
            continue
        totals["rx"] += max(rx_bytes, 0)
        totals["tx"] += max(tx_bytes, 0)
        totals["interfaces"].append(name)
    return totals


def _update_network_progress(state: dict, speed_state: dict, now: float,
                             totals: Optional[dict] = None) -> None:
    """Update live RX/TX rates using a short rolling counter window."""
    counters = totals if totals is not None else _network_byte_totals()
    rx = int(counters.get("rx") or 0)
    tx = int(counters.get("tx") or 0)
    interfaces = counters.get("interfaces") or []
    state["network_interfaces"] = ", ".join(interfaces)

    samples = speed_state.setdefault("network_samples", [])
    samples.append((now, rx, tx))
    cutoff = now - 5.0
    samples[:] = [sample for sample in samples if sample[0] >= cutoff]
    if len(samples) < 2:
        state.setdefault("download_speed", "--")
        state.setdefault("upload_speed", "--")
        state.setdefault("network_rate", "--")
        return

    sample_time, sample_rx, sample_tx = samples[0]
    elapsed = now - sample_time
    if elapsed <= 0:
        return
    rx_rate = max(rx - sample_rx, 0) / elapsed
    tx_rate = max(tx - sample_tx, 0) / elapsed
    state["download_speed"] = _fmt_bytes(rx_rate) + "/s"
    state["upload_speed"] = _fmt_bytes(tx_rate) + "/s"
    if state.get("operation") == "restoring":
        state["network_rate"] = state["download_speed"]
        state["network_direction"] = "download"
    else:
        state["network_rate"] = state["upload_speed"]
        state["network_direction"] = "upload"


def _parse_hms(text: str) -> Optional[int]:
    m = re.match(r"^\s*(\d+):(\d{2}):(\d{2})\s*$", text or "")
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _image_tree_size(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _parts_from_image_dir(path: str) -> list[str]:
    parts_path = os.path.join(path, "parts")
    try:
        with open(parts_path, "r", encoding="utf-8", errors="replace") as f:
            parts = [p for p in f.read().strip().split() if p]
            if parts:
                return parts
    except OSError:
        pass
    parts = set()
    if os.path.isdir(path):
        for name in os.listdir(path):
            m = re.match(r"^([A-Za-z0-9_]+n\d+p\d+|[A-Za-z0-9_]+p\d+|[A-Za-z0-9_]+\d+)\.", name)
            if m:
                parts.add(m.group(1))
    return sorted(parts)


def build_capture_image_plan(
    brand: str,
    model: str,
    part_number: str,
    cpu_model: str = "",
    os_info: Optional[dict] = None,
) -> dict:
    os_info = os_info or {}
    os_name = _clean_os_name(os_info.get("os_name") or "Windows")
    os_version = _clean_os_version(os_info.get("os_version") or os_info.get("os_build") or "Unknown")
    os_build = os_info.get("os_build") or ""
    os_token = _os_filename_token(os_name, os_version, os_build)
    image_subdir = "_".join([
        _safe_filename_token(brand or "GENERIC"),
        _safe_filename_token(model or "UNKNOWN_MODEL"),
        _safe_filename_token(part_number or "NOPN"),
        _safe_filename_token(cpu_model or "UNKNOWN_CPU"),
        os_token,
    ])
    image_name = image_subdir + ".img"
    image_dir = os.path.join(_NFS_MOUNT_POINT, image_subdir)
    image_old_subdir = image_subdir + "_old"
    return {
        "image_name": image_name,
        "image_subdir": image_subdir,
        "image_dir": image_dir,
        "image_old_name": image_old_subdir + ".img",
        "image_old_subdir": image_old_subdir,
        "image_old_dir": os.path.join(_NFS_MOUNT_POINT, image_old_subdir),
        "os_name": os_name,
        "os_version": os_version,
        "os_build": os_build,
        "os_token": os_token,
    }


def capture_target_exists(image_plan: dict) -> bool:
    return os.path.isdir(image_plan.get("image_dir", ""))


def rotate_existing_capture_target(image_plan: dict) -> None:
    image_dir = image_plan.get("image_dir", "")
    old_dir = image_plan.get("image_old_dir", "")
    if not image_dir or not old_dir:
        raise ValueError("invalid image plan")
    if os.path.exists(old_dir):
        shutil.rmtree(old_dir)
    if os.path.exists(image_dir):
        os.rename(image_dir, old_dir)


def _newest_part_from_image_dir(path: str) -> str:
    newest_part = ""
    newest_mtime = 0.0
    if not os.path.isdir(path):
        return newest_part
    for name in os.listdir(path):
        if name in {"parts", "Info-lshw.txt", "Info-lspci.txt", _CAPTURE_META_FILE}:
            continue
        full = os.path.join(path, name)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        if mtime <= newest_mtime:
            continue
        m = re.match(r"^([^.]+)\.", name)
        if not m:
            continue
        newest_mtime = mtime
        newest_part = m.group(1)
    return newest_part


def _next_unfinished_restore_part(state: dict) -> str:
    parts = state.get("parts") or []
    if not parts:
        return ""
    completed = set(state.get("completed_partitions") or [])
    for part in parts:
        if part not in completed:
            return part
    return ""


def _set_current_restore_part(state: dict, part: str) -> None:
    if not part:
        return
    state["current_partition"] = part
    parts = state.get("parts") or []
    if part in parts:
        state["partition_index"] = parts.index(part) + 1
    op = state.get("operation") or "capturing"
    state["phase"] = f"{op} {part}"


def _infer_restore_part_from_progress(state: dict) -> None:
    if state.get("operation") != "restoring":
        return
    parts = state.get("parts") or []
    if not parts:
        return
    current = state.get("current_partition") or ""
    completed = set(state.get("completed_partitions") or [])
    if current and current not in parts:
        return
    if current and current not in completed:
        return
    _set_current_restore_part(state, _next_unfinished_restore_part(state))


def _update_capture_state_from_line(line: str, state: dict) -> None:
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line or "").strip()
    if not clean:
        return
    state["last_line"] = clean[-140:]
    lower = clean.lower()

    if "not saved by partclone" in lower and "skip checking" in lower:
        state["last_line"] = "Skipped Partclone-only image check"
        return

    if "program terminated" in lower:
        # partclone prints this when its per-partition worker exits normally;
        # Clonezilla's final process return code decides restore/capture success.
        current = state.get("current_partition")
        parts = state.get("parts") or []
        try:
            part_pct = float(state.get("partition_percent") or 0.0)
        except (TypeError, ValueError):
            part_pct = 0.0
        if (
            state.get("operation") == "restoring"
            and not current
            and parts
            and part_pct >= 99.0
        ):
            current = _next_unfinished_restore_part(state)
            _set_current_restore_part(state, current)
        if current and (not parts or current in parts) and part_pct >= 99.0:
            completed = state.setdefault("completed_partitions", [])
            if current not in completed:
                completed.append(current)
            total = state.get("parts_total") or len(state.get("parts") or [])
            state["parts_done"] = len(completed)
            state["parts_left"] = max(total - len(completed), 0) if total else None
            state["partition_percent"] = 100.0
            state["phase"] = f"finished {current}"
            state["last_line"] = f"Finished {current}; preparing next partition"
        else:
            state["last_line"] = "Partition tool completed"
        state["partition_eta_sec"] = None
        state["partition_eta_text"] = "--"
        return

    op = state.get("operation") or "capturing"
    m = re.search(r"Starting (?:saving|restoring)\s+(/dev/\S+)\s+as\s+([^.\s]+)", clean)
    if m:
        part = os.path.basename(m.group(1))
        state["current_partition"] = part
        state["current_image_file"] = m.group(2)
        parts = state.get("parts") or []
        if part in parts:
            state["partition_index"] = parts.index(part) + 1
        state["partition_percent"] = 0.0
        state["partition_size"] = "--"
        state["partition_used"] = "--"
        state["partition_free"] = "--"
        state["filesystem"] = "--"
        state["phase"] = f"{op} {part}"
        return

    m = re.search(r"Starting to (?:save|restore)\s+(?:image|device)\s+\((/dev/[^)]+)\)", clean)
    if m:
        part = os.path.basename(m.group(1))
        state["current_partition"] = part
        parts = state.get("parts") or []
        if part in parts:
            state["partition_index"] = parts.index(part) + 1
        state["partition_percent"] = 0.0
        state["phase"] = f"{op} {part}"
        return

    m = re.search(
        r"Starting to restore image\s+\([^)]+\)\s+to device\s+\((/dev/[^)]+)\)",
        clean,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"Starting to clone/restore\s+\([^)]+\)\s+to\s+\((/dev/[^)]+)\)",
            clean,
            re.IGNORECASE,
        )
    if m:
        part = os.path.basename(m.group(1))
        state["current_partition"] = part
        parts = state.get("parts") or []
        if part in parts:
            state["partition_index"] = parts.index(part) + 1
        state["partition_percent"] = 0.0
        state["phase"] = f"{op} {part}"
        return

    m = re.search(r"\bptcl-\S+\b.*?(/dev/[A-Za-z0-9_.+/-]+)", clean, re.IGNORECASE)
    if m:
        part = os.path.basename(m.group(1).rstrip(".,;:'\")"))
        state["current_partition"] = part
        parts = state.get("parts") or []
        if part in parts:
            state["partition_index"] = parts.index(part) + 1
        state["partition_percent"] = 0.0
        state["partition_size"] = "--"
        state["partition_used"] = "--"
        state["partition_free"] = "--"
        state["filesystem"] = "--"
        state["phase"] = f"{op} {part}"
        return

    m = re.search(r"Finished (?:saving|restoring)\s+(/dev/\S+)\s+as", clean)
    if m:
        part = os.path.basename(m.group(1))
        completed = state.setdefault("completed_partitions", [])
        if part not in completed:
            completed.append(part)
        state["parts_done"] = len(completed)
        total = state.get("parts_total") or len(state.get("parts") or [])
        state["parts_left"] = max(total - len(completed), 0) if total else None
        state["partition_percent"] = 100.0
        state["phase"] = f"finished {part}"
        return

    m = re.search(
        r"Partclone successfully .*? to (?:the )?(?:device )?\((/dev/[^)]+)\)",
        clean,
        re.IGNORECASE,
    )
    if m:
        part = os.path.basename(m.group(1))
        completed = state.setdefault("completed_partitions", [])
        if part not in completed:
            completed.append(part)
        state["parts_done"] = len(completed)
        total = state.get("parts_total") or len(state.get("parts") or [])
        state["parts_left"] = max(total - len(completed), 0) if total else None
        state["partition_percent"] = 100.0
        state["phase"] = f"finished {part}"
        state["last_line"] = f"Finished {part}; preparing next partition"
        return

    m = re.search(r"/dev/(\S+)\s+filesystem:\s*([A-Za-z0-9_+\-.]+)", clean)
    if m:
        state["current_partition"] = m.group(1)
        state["filesystem"] = m.group(2).upper()
        return

    m = re.search(r"File system:\s*(.+)$", clean)
    if m:
        state["filesystem"] = m.group(1).strip()
        return

    m = re.search(r"Device size:\s*([0-9.]+\s*[KMGT]?B)", clean)
    if m:
        state["partition_size"] = m.group(1).strip()
        return

    m = re.search(r"Space in use:\s*([0-9.]+\s*[KMGT]?B)", clean)
    if m:
        state["partition_used"] = m.group(1).strip()
        return

    m = re.search(r"Free Space:\s*([0-9.]+\s*[KMGT]?B)", clean)
    if m:
        state["partition_free"] = m.group(1).strip()
        return

    m = re.search(r"Block size:\s*([0-9]+\s*Byte)", clean)
    if m:
        state["block_size"] = m.group(1).strip()
        return

    m = re.search(r"Elapsed:\s*([0-9:]+)\s+Remaining:\s*([0-9:]+)\s+Rate:\s*([0-9.]+\s*[A-Za-z/]+)", clean)
    if m:
        state["partition_elapsed_text"] = m.group(1)
        state["partition_eta_text"] = m.group(2)
        state["partition_eta_sec"] = _parse_hms(m.group(2))
        state["transfer_rate"] = m.group(3)
        return

    m = re.search(
        r"Current\s+Block:\s*([0-9,]+)\s*,?\s*Total\s+Block:\s*([0-9,]+)"
        r"(?:\s*,?\s*Complete:\s*([0-9]+(?:\.[0-9]+)?)\s*%)?",
        clean,
        re.IGNORECASE,
    )
    if m:
        _infer_restore_part_from_progress(state)
        current = int(m.group(1).replace(",", ""))
        total = max(int(m.group(2).replace(",", "")), 1)
        state["current_block"] = current
        state["total_block"] = total
        pct = float(m.group(3)) if m.group(3) is not None else current * 100.0 / total
        state["partition_percent"] = max(0.0, min(100.0, pct))
        complete_idx = lower.find("complete:")
        state["current_block_line"] = (
            clean[:complete_idx] if complete_idx >= 0 else clean
        ).rstrip(" ,")[-140:]
        return

    m = re.search(r"([0-9]+(?:\.[0-9]+)?)%\s+completed", clean)
    if m:
        state["partition_percent"] = max(0.0, min(100.0, float(m.group(1))))


def _emit_capture_progress(progress_callback: Optional[Callable[[dict], None]],
                           state: dict, started: float,
                           image_dir: str,
                           speed_state: dict) -> None:
    if not progress_callback:
        return
    now = time.monotonic()
    state["elapsed_sec"] = int(now - started)
    _update_network_progress(state, speed_state, now)
    parts = _parts_from_image_dir(image_dir)
    if parts:
        state["parts"] = parts
        state["parts_total"] = len(parts)
    newest = ""
    if state.get("operation") != "restoring":
        newest = _newest_part_from_image_dir(image_dir)
        if newest and state.get("parts") and newest not in state["parts"]:
            newest = ""
        if newest and not state.get("current_partition"):
            state["current_partition"] = newest
        if newest and state.get("parts") and newest in state["parts"]:
            state.setdefault("partition_index", state["parts"].index(newest) + 1)

    total = state.get("parts_total") or len(state.get("parts") or [])
    done = len(state.get("completed_partitions") or [])
    state["parts_done"] = done
    state["parts_left"] = max(total - done, 0) if total else None
    if total and state.get("current_partition") in (state.get("parts") or []):
        state["partition_index"] = state["parts"].index(state["current_partition"]) + 1
    if total and done >= total:
        state["partition_percent"] = 100.0
        state["overall_percent"] = 100.0
        state["overall_eta_sec"] = 0
        state["partition_eta_sec"] = 0
        state["partition_eta_text"] = "0:00:00"
        state["parts_left"] = 0

    part_pct = state.get("partition_percent")
    if total:
        current_fraction = (part_pct or 0.0) / 100.0
        state["overall_percent"] = max(
            0.0, min(100.0, ((done + current_fraction) / total) * 100.0)
        )
        eta = state.get("partition_eta_sec")
        if eta is not None:
            remaining_in_current = eta
            average_part = (state["elapsed_sec"] / max(done + current_fraction, 0.05))
            state["overall_eta_sec"] = int(remaining_in_current + max(total - done - 1, 0) * average_part)

    bytes_written = _image_tree_size(image_dir)
    state["bytes_written"] = bytes_written
    prev_bytes = speed_state.get("bytes")
    prev_time = speed_state.get("time")
    if prev_bytes is not None and prev_time is not None and now > prev_time:
        delta = bytes_written - prev_bytes
        if delta >= 0:
            state["write_speed"] = _fmt_bytes(delta / (now - prev_time)) + "/s"
    speed_state["bytes"] = bytes_written
    speed_state["time"] = now
    state["bytes_written_text"] = _fmt_bytes(bytes_written)
    progress_callback(dict(state))


_CAPTURE_PARTCLONE_FAILURE_MARKERS = (
    "failed to use partclone program to save or restore an image",
    "failed to save partition",
    "partclone fail",
    "partclone failed",
)
_CAPTURE_NO_RETRY_MARKERS = (
    "no space left",
    "permission denied",
    "read-only file system",
    "cannot create",
    "cannot write",
    "write error",
)
_CAPTURE_GENERIC_HELP_MARKERS = (
    "if this action fails or hangs, check",
    "is the disk full ?",
    "network connection and nfs service",
)
_CLONEZILLA_CONTINUE_PROMPTS = (
    "press enter to continue",
    "press \"enter\" to continue",
    "press 'enter' to continue",
)


def _plain_ocs_text(text: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text or "").lower()


def _capture_failure_should_retry_with_ntfsclone(evidence: str) -> bool:
    meaningful_lines = [
        line
        for line in _plain_ocs_text(evidence).splitlines()
        if not any(marker in line for marker in _CAPTURE_GENERIC_HELP_MARKERS)
    ]
    text = "\n".join(meaningful_lines)
    if any(marker in text for marker in _CAPTURE_NO_RETRY_MARKERS):
        return False
    return any(marker in text for marker in _CAPTURE_PARTCLONE_FAILURE_MARKERS)


def _capture_savedisk_cmd(device: str, image_subdir: str, method: str) -> list[str]:
    dev_name = os.path.basename(device)
    # Primary path stays Partclone. The fallback keeps Partclone available for
    # non-NTFS partitions, but uses ntfsclone --force --rescue for NTFS.
    clone_flags = ["-q2"]
    if method == "ntfsclone_fallback":
        clone_flags = ["-q", "-q2", "-ntfs-ok", "-rescue", "-sc"]
    return [
        "ocs-sr", "-batch", "--nogui", "-or", _NFS_MOUNT_POINT,
        *clone_flags,
        "-j2", "-rm-win-swap-hib",
        "-z5p", "-i", "4096", "-p", "true",
        "savedisk", image_subdir, dev_name,
    ]


def _send_clonezilla_continue(proc: subprocess.Popen, evidence_lines: list[str],
                              reason: str) -> bool:
    if proc.stdin is None:
        return False
    try:
        proc.stdin.write(b"\n")
        proc.stdin.flush()
        evidence_lines.append(f"sent Clonezilla continue ({reason})")
        return True
    except (BrokenPipeError, OSError):
        return False


def _terminate_capture_process(proc: subprocess.Popen) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
            time.sleep(0.5)
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            return
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.terminate()
        time.sleep(0.5)
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass


def _ocs_savedisk_once(device: str, image_dir: str,
                       progress_callback: Optional[Callable[[dict], None]] = None,
                       timeout: int = 4 * 3600,
                       method: str = "partclone",
                       allow_partclone_retry: bool = False) -> tuple[bool, str, str]:
    """Run one Clonezilla ``ocs-sr savedisk`` attempt."""
    image_subdir = os.path.basename(image_dir.rstrip("/"))
    cmd = _capture_savedisk_cmd(device, image_subdir, method)
    env = dict(os.environ)
    env.setdefault("OCS_ROOT", "/")
    env["OCSROOT"] = _NFS_MOUNT_POINT
    env["ocsroot"] = _NFS_MOUNT_POINT
    started = time.monotonic()
    state = {
        "operation": "capturing",
        "phase": "starting Clonezilla",
        "elapsed_sec": 0,
        "image_subdir": image_subdir,
        "image_dir": image_dir,
        "device": device,
        "parts": [],
        "completed_partitions": [],
        "partition_percent": 0.0,
        "overall_percent": 0.0,
        "last_line": "",
    }
    speed_state: dict = {}
    evidence_lines: list[str] = [
        f"capture_method={method}",
        f"$ {' '.join(cmd)}",
        f"OCSROOT={env.get('OCSROOT')}",
    ]
    _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        return False, image_subdir, "ocs-sr: not found (is Clonezilla Live booted?)"
    except OSError as e:
        return False, image_subdir, f"OSError: {e}"

    buffer = ""
    last_emit = 0.0
    timed_out = False
    sent_continue = False
    early_retry = False
    while True:
        now = time.monotonic()
        if now - started > timeout:
            timed_out = True
            _terminate_capture_process(proc)
            evidence_lines.append(f"ocs-sr timeout after {timeout}s")
            break

        if proc.stdout is None:
            break
        readable, _, _ = select.select([proc.stdout], [], [], 0.5)
        if readable:
            chunk = os.read(proc.stdout.fileno(), 4096).decode(
                "utf-8", "replace"
            )
            if chunk:
                buffer += chunk
                if (
                    not sent_continue
                    and any(marker in _plain_ocs_text(buffer) for marker in _CLONEZILLA_CONTINUE_PROMPTS)
                ):
                    sent_continue = _send_clonezilla_continue(proc, evidence_lines, "prompt")
                pieces = re.split(r"[\r\n]+", buffer)
                buffer = pieces.pop() if pieces else ""
                for piece in pieces:
                    if not piece:
                        continue
                    evidence_lines.append(piece)
                    if len(evidence_lines) > 500:
                        evidence_lines = evidence_lines[-500:]
                    _update_capture_state_from_line(piece, state)
                    if (
                        allow_partclone_retry
                        and _capture_failure_should_retry_with_ntfsclone(piece)
                    ):
                        early_retry = True
                        state["phase"] = "retrying with NTFS clone fallback"
                        state["last_line"] = (
                            "Partclone capture failed; retrying Windows partition with ntfsclone"
                        )
                        evidence_lines.append(
                            "detected Partclone capture failure; aborting this attempt for ntfsclone fallback"
                        )
                        _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
                        _terminate_capture_process(proc)
                        break
                    if (
                        not sent_continue
                        and any(marker in _plain_ocs_text(piece) for marker in _CAPTURE_PARTCLONE_FAILURE_MARKERS)
                    ):
                        sent_continue = _send_clonezilla_continue(proc, evidence_lines, "failure marker")
                if early_retry:
                    break
            elif proc.poll() is not None:
                break

        if early_retry:
            break
        if now - last_emit >= 1.0:
            _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
            last_emit = now
        if proc.poll() is not None:
            break

    if buffer.strip():
        evidence_lines.append(buffer.strip())
        _update_capture_state_from_line(buffer, state)

    rc_value = proc.wait() if not timed_out else 124
    if early_retry:
        rc_value = 125
    elapsed = int(time.monotonic() - started)
    state["phase"] = "completed" if rc_value == 0 else "failed"
    state["elapsed_sec"] = elapsed
    if rc_value == 0:
        state["partition_percent"] = 100.0
        state["overall_percent"] = 100.0
        state["overall_eta_sec"] = 0
        state["partition_eta_sec"] = 0
        state["partition_eta_text"] = "0:00:00"
    _emit_capture_progress(progress_callback, state, started, image_dir, speed_state)
    ev = (
        "\n".join(evidence_lines)[-_EVIDENCE_CAP:] +
        f"\nrc={rc_value}\nelapsed_sec={elapsed}"
    )
    return rc_value == 0, image_subdir, ev


def _emit_capture_stage(progress_callback: Optional[Callable[[dict], None]],
                        message: str) -> None:
    if not progress_callback:
        return
    progress_callback({
        "phase": message,
        "last_line": message,
        "partition_eta_sec": None,
        "partition_eta_text": "--",
        "network_rate": "--",
    })


def _ocs_savedisk(device: str, image_dir: str,
                   progress_callback: Optional[Callable[[dict], None]] = None,
                   timeout: int = 4 * 3600) -> tuple[bool, str, str]:
    """Run Clonezilla ``ocs-sr savedisk`` with one NTFS-specific fallback."""
    ok, image_subdir, evidence = _ocs_savedisk_once(
        device,
        image_dir,
        progress_callback=progress_callback,
        timeout=timeout,
        method="partclone",
        allow_partclone_retry=True,
    )
    if ok:
        return ok, image_subdir, evidence

    fallback_enabled = os.environ.get("VSTL_CAPTURE_NTFSCLONE_FALLBACK", "1").strip().lower()
    if fallback_enabled in {"0", "false", "no", "off"}:
        return False, image_subdir, evidence
    if not _capture_failure_should_retry_with_ntfsclone(evidence):
        return False, image_subdir, evidence

    _emit_capture_stage(
        progress_callback,
        "Partclone capture failed; retrying with ntfsclone fallback",
    )
    shutil.rmtree(image_dir, ignore_errors=True)
    retry_ok, retry_subdir, retry_ev = _ocs_savedisk_once(
        device,
        image_dir,
        progress_callback=progress_callback,
        timeout=timeout,
        method="ntfsclone_fallback",
        allow_partclone_retry=False,
    )
    combined = "\n".join([
        evidence,
        "--- retry with ntfsclone fallback ---",
        retry_ev,
    ])[-_EVIDENCE_CAP:]
    return retry_ok, retry_subdir, combined


def _quarantine_failed_capture(image_dir: str, evidence: str) -> str:
    if not os.path.isdir(image_dir):
        return ""
    try:
        marker = os.path.join(image_dir, "vstl_capture_failure.txt")
        with open(marker, "w", encoding="utf-8", errors="replace") as f:
            f.write((evidence or "")[-_EVIDENCE_CAP:])
            f.write("\n")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dest = f"{image_dir}_failed_{stamp}"
        if os.path.exists(dest):
            dest = f"{dest}_{uuid.uuid4().hex[:8]}"
        os.rename(image_dir, dest)
        return f"failed capture evidence preserved at {dest}"
    except OSError as exc:
        return f"failed capture evidence could not be preserved: {exc}"


def run_capture(
    device: str,
    brand: str,
    model: str,
    part_number: str,
    source_serial: str,
    nfs_host: str,
    nfs_share: str,
    mount_options: str = "rw,nolock,vers=3",
    cpu_model: str = "",
    validation: Optional[dict] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
    image_plan: Optional[dict] = None,
    replace_existing: bool = False,
    secure_erase_authorized: bool = False,
) -> dict:
    """End-to-end: mount NFS, run Clonezilla savedisk, compute SHA256,
    return the structured result. Caller is responsible for POSTing the
    result to /api/imaging/golden-copies/capture-complete and for finally
    unmounting NFS via :func:`umount_nfs`.
    """
    started_at = _now_iso()
    started_ts = time.monotonic()

    if not secure_erase_authorized:
        return _fail_result(
            device,
            started_at,
            started_ts,
            error="Capture blocked: this exact laptop and storage device do not have a verified secure-erase authorization.",
            evidence="Run Certified Secure Erase successfully before attempting capture.",
        )

    ok, mount_ev = mount_nfs(nfs_host, nfs_share, mount_options)
    if not ok:
        return _fail_result(device, started_at, started_ts,
                             error=f"NFS mount failed: {mount_ev}",
                             evidence=mount_ev)

    # Build a deterministic image name
    os_info = (validation or {}).get("os_info") or {}
    image_plan = image_plan or build_capture_image_plan(
        brand, model, part_number, cpu_model, os_info,
    )
    image_name = image_plan["image_name"]
    image_subdir = image_plan["image_subdir"]
    image_dir = image_plan["image_dir"]
    replaced_existing = False
    if os.path.exists(image_dir):
        if not replace_existing:
            return _fail_result(
                device, started_at, started_ts,
                error=f"Exact image backup already exists: {image_dir}",
                evidence="Use replace_existing=True after operator confirmation to rotate the old copy.",
            )
        try:
            rotate_existing_capture_target(image_plan)
            replaced_existing = True
        except (OSError, ValueError) as e:
            return _fail_result(
                device, started_at, started_ts,
                error=f"Could not rotate existing image backup: {e}",
                evidence=f"image_dir={image_dir} old_dir={image_plan.get('image_old_dir', '')}",
            )
    if os.path.exists(image_dir):
        return _fail_result(
            device, started_at, started_ts,
            error=f"Capture target still exists after rotation: {image_dir}",
            evidence="Refusing to overwrite an existing golden-copy directory.",
        )

    ok_save, _subdir, save_ev = _ocs_savedisk(
        device, image_dir, progress_callback=progress_callback,
    )
    if not ok_save:
        quarantine_ev = _quarantine_failed_capture(image_dir, save_ev)
        combined_ev = "\n---\n".join(
            item for item in (mount_ev, save_ev, quarantine_ev) if item
        )
        return _fail_result(device, started_at, started_ts,
                             error="ocs-sr savedisk failed",
                             evidence=combined_ev)

    # Compute SHA-256 of the manifest file produced by Clonezilla (the
    # ``disk`` file lists every partclone image in the set â€” checksumming
    # that gives us a single deterministic hash for the entire set).
    manifest = os.path.join(image_dir, "disk")
    sha = _sha256_file(manifest) if os.path.isfile(manifest) else ""

    # Total size = sum of every file in the image subdir
    total_bytes = 0
    try:
        for root, _dirs, files in os.walk(image_dir):
            for fname in files:
                try:
                    total_bytes += os.path.getsize(os.path.join(root, fname))
                except OSError:
                    pass
    except OSError:
        pass
    image_size_gb = round(total_bytes / (1024 ** 3), 2) if total_bytes else 0

    metadata = {
        "image_name": image_name,
        "image_subdir": image_subdir,
        "brand": brand,
        "model": model,
        "part_number": part_number,
        "sku": part_number,
        "cpu": cpu_model,
        "source_serial": source_serial,
        "source_device": device,
        "captured_at": _now_iso(),
        "os_name": image_plan.get("os_name", ""),
        "os_version": image_plan.get("os_version", ""),
        "os_build": image_plan.get("os_build", ""),
        "os_token": image_plan.get("os_token", ""),
        "replaced_existing": replaced_existing,
        "validation": validation or {},
        "schema": "vstl_capture_metadata_v2",
    }
    try:
        with open(os.path.join(image_dir, _CAPTURE_META_FILE), "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, sort_keys=True)
    except OSError:
        pass

    duration = int(time.monotonic() - started_ts)
    return {
        "ok": True,
        "image_name":   image_name,
        "image_subdir": image_subdir,
        "image_path":   image_dir,
        "image_size_gb": image_size_gb,
        "image_sha256":  sha,
        "started_at":    started_at,
        "completed_at":  _now_iso(),
        "duration_sec":  duration,
        "device":        device,
        "part_number":   part_number,
        "model":         model,
        "brand":         brand,
        "cpu":           cpu_model,
        "os_name":       image_plan.get("os_name", ""),
        "os_version":    image_plan.get("os_version", ""),
        "os_build":      image_plan.get("os_build", ""),
        "os_token":      image_plan.get("os_token", ""),
        "replaced_existing": replaced_existing,
        "metadata_file":  os.path.join(image_dir, _CAPTURE_META_FILE),
        "evidence":      ("\n---\n".join([mount_ev, save_ev]))[:_EVIDENCE_CAP],
        "error_message": "",
    }


def _fail_result(device: str, started_at: str, started_ts: float,
                  error: str, evidence: str) -> dict:
    return {
        "ok": False,
        "image_name": "",
        "image_subdir": "",
        "image_path": "",
        "image_size_gb": 0,
        "image_sha256": "",
        "os_name": "",
        "os_version": "",
        "os_build": "",
        "os_token": "",
        "replaced_existing": False,
        "started_at": started_at,
        "completed_at": _now_iso(),
        "duration_sec": int(time.monotonic() - started_ts),
        "device": device,
        "evidence": evidence[:_EVIDENCE_CAP],
        "error_message": error,
    }
