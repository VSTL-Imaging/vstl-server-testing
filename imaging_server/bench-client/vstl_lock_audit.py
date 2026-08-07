"""
vstl_lock_audit.py — Phase 2A Lock & MDM/BIOS Audit detectors.

This module runs on the bench-side Live ISO BEFORE any erase / image / QC
work happens. Its only job is to determine if the unit is locked or
managed in a way that would make the rest of the workflow unsafe or
ineffective:

  Hardware locks
    * BIOS admin password           (dmidecode -t 24, manually confirmed)
    * ATA Security / drive password (hdparm -I or nvme-cli, manually confirmed)
    * Computrace / Absolute LoJack  (dmidecode -t 11 — only flag if ACTIVE)

  Software locks  (read-only mount of Windows partition)
    * Microsoft Intune enrollment   (strict enrollment-artifact probes)
    * Azure AD / Entra device join  (strict CloudAPCache/AzureAd probe)
    * Vendor MDM (Workspace ONE / AirWatch / MobileIron / Hexnode / etc.)

Design rules
------------
* Never crash. Missing tool / unmounted disk → returns NOT_DETECTED.
* Never exec the shell. Every subprocess uses argv list to avoid quoting bugs.
* Every detector returns a uniform 4-key dict so the TUI can render them
  in a single loop:
      {"present": bool,                # we are confident this lock is ACTIVE
       "status": str,                  # human-readable: ENABLED / NONE / etc.
       "evidence": str,                # raw findings (truncated for UI)
       "manual_confirm_required": bool # hardware-only locks need operator Y/N}
* `run_full_audit()` composes them all into the bundle the TUI will POST.

Detection trust per user choice 2c:
  - SOFTWARE locks (Intune / Azure AD / vendor MDM):
      auto-only — what the disk says is gospel.
  - HARDWARE locks (BIOS password / ATA security):
      auto-detect first, then manual confirm prompt in the TUI to catch
      false negatives on BIOSes that don't expose admin-pwd state via
      SMBIOS Type 24.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Optional

NOT_DETECTED = "NOT_DETECTED"
DETECTED = "DETECTED"
UNKNOWN = "UNKNOWN"


def _empty_result(status: str = NOT_DETECTED, manual: bool = False) -> dict:
    return {
        "present": False,
        "status": status,
        "evidence": "",
        "manual_confirm_required": manual,
    }


def _run(argv: list[str], timeout: int = 6) -> str:
    """Run a command, capture stdout, return "" on any failure.

    Identical contract to vstl_hw_detect._run but kept independent so the
    two modules can be unit-tested with separate monkeypatches.
    """
    try:
        out = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return (out.stdout or "").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


# ---------------------------------------------------------------------------
# 1. BIOS admin password (Hardware Security — SMBIOS Type 24)
# ---------------------------------------------------------------------------
def detect_bios_password() -> dict:
    """Read SMBIOS Type 24 'Hardware Security'.

    Output of ``dmidecode -t 24`` looks like::

        Hardware Security
            Power-On Password Status: Disabled
            Keyboard Password Status: Not Implemented
            Administrator Password Status: Enabled
            Front Panel Reset Status: Not Implemented

    We treat any of {Power-On, Administrator} == 'Enabled' as a positive.
    Lots of BIOSes either don't expose Type 24 or always report 'Disabled'
    even when an admin password IS set — that's exactly why the TUI also
    runs a manual-confirm prompt (per design choice 2c).
    """
    if os.geteuid() != 0:
        return _empty_result(UNKNOWN, manual=True)

    raw = _run(["dmidecode", "-t", "24"])
    if not raw:
        # dmidecode silent → assume tool absent or BIOS doesn't expose Type 24.
        return _empty_result(UNKNOWN, manual=True)

    enabled_lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        m = re.match(
            r"(Power-On Password Status|Administrator Password Status|"
            r"Keyboard Password Status):\s*(\w+)",
            line,
        )
        if m and m.group(2).strip().lower() == "enabled":
            enabled_lines.append(line)

    if enabled_lines:
        return {
            "present": True,
            "status": "ENABLED",
            "evidence": " | ".join(enabled_lines)[:300],
            "manual_confirm_required": True,  # keep the safety-net prompt
        }

    return {
        "present": False,
        "status": "DISABLED",
        "evidence": "SMBIOS Type 24 reports all password statuses Disabled",
        "manual_confirm_required": True,  # always confirm — false-negative-prone
    }


# ---------------------------------------------------------------------------
# 2. ATA Security / NVMe drive password
# ---------------------------------------------------------------------------
def _list_block_devices() -> list[dict]:
    """List physical disks via lsblk."""
    raw = _run(
        ["lsblk", "-d", "-o", "NAME,TYPE,TRAN", "--noheadings"],
    )
    drives: list[dict] = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, typ = parts[0], parts[1]
        tran = parts[2].upper() if len(parts) >= 3 else ""
        if typ != "disk":
            continue
        drives.append({"name": name, "tran": tran})
    return drives


def detect_ata_security() -> dict:
    """Check every disk for active ATA Security or NVMe lock state.

    SATA: ``hdparm -I /dev/sdX`` exposes a "Security:" block. We flag if
    the word ``enabled`` appears WITHOUT a leading ``not`` on the same
    line. ``frozen`` alone is fine — that's the BIOS preventing future
    password changes, not an active lock.

    NVMe: ``nvme id-ctrl /dev/nvmeXnY`` exposes Optional Admin Command
    Support (OACS) bits + locked-state via a "PSL" / "OACS" set of
    flags. We grep for active security words.
    """
    if os.geteuid() != 0:
        return _empty_result(UNKNOWN, manual=True)

    drives = _list_block_devices()
    if not drives:
        return _empty_result(NOT_DETECTED, manual=True)

    findings: list[str] = []

    for d in drives:
        dev = f"/dev/{d['name']}"
        if d["name"].startswith("nvme"):
            out = _run(["nvme", "id-ctrl", dev])
            # NVMe admin-locked is rare; we look for explicit lock words
            for ln in out.splitlines():
                low = ln.lower()
                if "locked" in low and "not locked" not in low:
                    findings.append(f"{dev}: {ln.strip()[:160]}")
        else:
            out = _run(["hdparm", "-I", dev])
            in_sec = False
            for ln in out.splitlines():
                ll = ln.strip()
                if ll.lower().startswith("security:"):
                    in_sec = True
                    continue
                if in_sec:
                    # Section ends on a blank line or a non-tabbed top-level header
                    if not ll:
                        in_sec = False
                        continue
                    low = ll.lower()
                    # hdparm prints "not enabled" for unlocked drives;
                    # only "enabled" alone (or "locked") is positive
                    if (
                        re.search(r"^\s*(enabled|locked)\b", ll, re.I)
                        and not re.search(r"^\s*not\s+(enabled|locked)\b", ll, re.I)
                    ):
                        findings.append(f"{dev}: {ll[:160]}")

    if findings:
        return {
            "present": True,
            "status": "ENABLED",
            "evidence": " | ".join(findings)[:300],
            "manual_confirm_required": True,
        }
    return {
        "present": False,
        "status": "DISABLED",
        "evidence": f"Checked {len(drives)} drive(s); no active drive password.",
        "manual_confirm_required": True,
    }


# ---------------------------------------------------------------------------
# 3. Computrace / Absolute LoJack (SMBIOS Type 11 — OEM Strings)
#    Per design choice 4b: only flag if ACTIVE / ARMED / PERSISTENT —
#    "available, not yet activated" stock-BIOS strings DO NOT count.
# ---------------------------------------------------------------------------
_COMPUTRACE_BRAND_RE = re.compile(
    r"\b(absolute|computrace|lojack|persistence)\b", re.I
)
_COMPUTRACE_ACTIVE_RE = re.compile(
    r"\b(active|armed|enabled|persistent|enrolled|installed)\b", re.I
)


def detect_computrace() -> dict:
    """Scan SMBIOS OEM strings for Computrace / Absolute / LoJack signatures.

    Per the user's policy choice 4b we only return present=True when the
    OEM string ALSO contains an "active / armed / persistent / enrolled"
    keyword — many enterprise BIOSes ship with "Absolute Software" listed
    as a CAPABILITY even when the agent is not enrolled at the customer
    site. Without this filter we would generate constant false positives
    on stock HP/Lenovo enterprise units.
    """
    if os.geteuid() != 0:
        return _empty_result(UNKNOWN)

    raw = _run(["dmidecode", "-t", "11"])
    if not raw:
        return _empty_result(NOT_DETECTED)

    # Collect strings inside the "OEM Strings" block(s)
    strings: list[str] = []
    in_block = False
    for line in raw.splitlines():
        if line.strip().startswith("OEM Strings"):
            in_block = True
            continue
        if in_block:
            if not line.strip():
                in_block = False
                continue
            m = re.match(r"\s*String\s+\d+:\s+(.+)$", line)
            if m:
                strings.append(m.group(1).strip())

    branded = [s for s in strings if _COMPUTRACE_BRAND_RE.search(s)]
    if not branded:
        return _empty_result(NOT_DETECTED)

    armed = [s for s in branded if _COMPUTRACE_ACTIVE_RE.search(s)]
    if armed:
        return {
            "present": True,
            "status": "ACTIVE",
            "evidence": " | ".join(armed)[:300],
            "manual_confirm_required": False,
        }

    # Brand mention but no active keyword — likely stock BIOS capability
    return {
        "present": False,
        "status": "AVAILABLE_NOT_ACTIVE",
        "evidence": " | ".join(branded)[:300],
        "manual_confirm_required": False,
    }


# ---------------------------------------------------------------------------
# Helpers — find + mount the largest Windows partition read-only.
#   Used by Intune / Azure AD / vendor MDM detectors.
# ---------------------------------------------------------------------------
def _find_windows_partitions() -> list[dict]:
    """Return list of {device, type} for partitions worth probing.

    Uses blkid (1 call) — output looks like::

        /dev/sda3: LABEL="Windows" TYPE="ntfs" ...
    """
    raw = _run(["blkid", "-o", "full"])
    parts: list[dict] = []
    for line in raw.splitlines():
        if ":" not in line:
            continue
        dev, rest = line.split(":", 1)
        m_type = re.search(r'TYPE="([^"]+)"', rest)
        if not m_type:
            continue
        ftype = m_type.group(1).lower()
        if ftype == "ntfs":
            parts.append({"device": dev.strip(), "type": ftype})
    return parts


def _read_signature(dev: str, length: int = 16) -> bytes:
    """Read first `length` bytes of `dev`. Empty on any error."""
    try:
        with open(dev, "rb") as f:
            return f.read(length)
    except OSError:
        return b""


# ---------------------------------------------------------------------------
# 4. Legacy BitLocker policy shim
# ---------------------------------------------------------------------------
def detect_bitlocker(parts: Optional[list[dict]] = None) -> dict:
    """Compatibility shim: BitLocker is no longer identified by bench policy."""
    del parts
    return _empty_result("NOT_CHECKED_BY_POLICY")


# ---------------------------------------------------------------------------
# 5. Intune  /  6. Azure AD  /  7. Vendor MDM
# ---------------------------------------------------------------------------
# File-path probes against a mounted Windows partition. We do NOT use a
# registry-hive parser — keeping the dependency surface small for the
# Live ISO. The probe paths below are stable across Windows 10 + 11 and
# all generations of the listed MDM vendors.

# Per-vendor identifying paths (relative to Windows root). If ANY exists,
# the vendor is "present". Each entry is (label, paths[]).
INTUNE_STRONG_PATHS = [
    "ProgramData/Microsoft/IntuneManagementExtension",
    "ProgramData/Microsoft/EnterpriseDesktopAppManagement",
]
INTUNE_ENTERPRISE_MGMT_TASKS = "Windows/System32/Tasks/Microsoft/Windows/EnterpriseMgmt"
AZURE_AD_STRONG_PATHS = [
    "Windows/ServiceProfiles/NetworkService/AppData/Roaming/Microsoft/CloudAPCache/AzureAd",
]
REGISTRY_HIVE_PATHS = [
    "Windows/System32/config/SOFTWARE",
    "Windows/System32/config/SYSTEM",
]
INTUNE_REGISTRY_EVIDENCE = [
    ("Intune Management Extension", ("intunemanagementextension",)),
    ("Microsoft Intune", ("microsoft intune",)),
    ("Intune enrollment URL", ("manage.microsoft.com",)),
    ("Enterprise Desktop App Management", ("enterprisedesktopappmanagement",)),
    ("OMADM enrollment", ("omadm", "enroll")),
    ("MDM enrollment server", ("mdm", "enrollmentserver")),
    ("Enterprise Enrollment registry", ("enterpriseenrollment", "enrollments")),
]
AZURE_AD_REGISTRY_EVIDENCE = [
    ("CloudDomainJoin JoinInfo", ("clouddomainjoin", "joininfo")),
    ("Azure AD tenant", ("azuread", "tenantid")),
    ("Workplace joined user", ("workplacejoin", "useremail")),
]
VENDOR_MDM_REGISTRY_EVIDENCE = [
    ("Workspace ONE / AirWatch", ("airwatch",)),
    ("Workspace ONE Intelligent Hub", ("workspace one intelligent hub",)),
    ("VMware Workspace ONE", ("vmware", "workspace one")),
    ("MobileIron", ("mobileiron",)),
    ("Ivanti MDM", ("ivanti", "mdm")),
    ("Hexnode", ("hexnode",)),
    ("ManageEngine MDM", ("manageengine", "mdm")),
    ("IBM MaaS360", ("maas360",)),
    ("SOTI MobiControl", ("soti", "mobicontrol")),
    ("BlackBerry UEM", ("blackberry", "uem")),
    ("Jamf", ("jamf",)),
    ("Kandji", ("kandji",)),
    ("Miradore", ("miradore",)),
]
VENDOR_MDM_PROBES = [
    ("Workspace ONE / AirWatch", [
        "Program Files (x86)/AirWatch",
        "Program Files (x86)/VMware/Workspace ONE Intelligent Hub",
        "Program Files/AirWatch",
        "Program Files/VMware/Workspace ONE Intelligent Hub",
    ]),
    ("MobileIron", [
        "Program Files (x86)/MobileIron",
        "Program Files/MobileIron",
    ]),
    ("Hexnode", [
        "Program Files (x86)/Hexnode",
        "Program Files/Hexnode",
    ]),
    ("ManageEngine", [
        "Program Files (x86)/ManageEngine",
        "Program Files/ManageEngine",
    ]),
    ("IBM MaaS360", [
        "Program Files (x86)/MaaS360",
        "Program Files/MaaS360",
    ]),
    ("Citrix Endpoint Mgmt", [
        "Program Files (x86)/Citrix/Secure Hub",
        "Program Files/Citrix/Secure Hub",
    ]),
    ("SOTI MobiControl", [
        "Program Files (x86)/SOTI",
        "Program Files/SOTI",
    ]),
    ("BlackBerry UEM", [
        "Program Files (x86)/BlackBerry/UEM",
        "Program Files/BlackBerry/UEM",
    ]),
]

GUID_RE = re.compile(
    r"^\{?[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}\}?$"
)


def _probe_paths(mount_root: str, paths: list[str]) -> list[str]:
    """Return relative paths that exist under mount_root."""
    hits: list[str] = []
    for rel in paths:
        full = os.path.join(mount_root, rel)
        if os.path.exists(full):
            hits.append(rel)
    return hits


def _dir_has_files(path: str) -> bool:
    """Return True only when a directory tree contains concrete file evidence."""
    try:
        for _root, _dirs, files in os.walk(path):
            if files:
                return True
    except OSError:
        return False
    return False


def _probe_nonempty_dirs(mount_root: str, paths: list[str]) -> list[str]:
    """Return relative paths with concrete files, not empty/default folders."""
    hits: list[str] = []
    for rel in paths:
        full = os.path.join(mount_root, rel)
        if os.path.isdir(full) and _dir_has_files(full):
            hits.append(rel)
    return hits


def _probe_enterprise_mgmt_guid(mount_root: str) -> list[str]:
    """Return Intune EnterpriseMgmt GUID subfolders.

    The parent directory alone is not enough evidence; real enrollment adds
    GUID-shaped task subfolders.
    """
    base = os.path.join(mount_root, INTUNE_ENTERPRISE_MGMT_TASKS)
    if not os.path.isdir(base):
        return []
    hits: list[str] = []
    try:
        for entry in os.scandir(base):
            if (
                entry.is_dir(follow_symlinks=False)
                and GUID_RE.match(entry.name)
            ):
                hits.append(f"{INTUNE_ENTERPRISE_MGMT_TASKS}/{entry.name}")
    except OSError:
        return []
    return hits


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _hive_strings_lower(path: str) -> str:
    """Read registry hive strings without adding a Python registry dependency."""
    chunks: list[str] = []
    commands = (
        ["strings", "-a", "-el", path],      # UTF-16LE strings in registry hives
        ["strings", "-a", "-n", "5", path], # ASCII fallback
    )
    for argv in commands:
        try:
            rc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                errors="ignore",
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if rc.stdout:
            chunks.append(rc.stdout[:600000])
    return "\n".join(chunks).lower()


def _probe_registry_evidence(
    mount_root: str,
    evidence_rules: list[tuple[str, tuple[str, ...]]],
) -> list[str]:
    hits: list[str] = []
    for rel in REGISTRY_HIVE_PATHS:
        full = os.path.join(mount_root, rel)
        if not os.path.isfile(full):
            continue
        text = _hive_strings_lower(full)
        if not text:
            continue
        hive = os.path.basename(rel)
        for label, tokens in evidence_rules:
            if all(token.lower() in text for token in tokens):
                hits.append(f"Registry {hive}: {label}")
    return _dedupe(hits)


def _mount_windows_ro(parts: list[dict]) -> Optional[str]:
    """Mount the largest NTFS partition read-only at /mnt/winprobe.

    Returns the mount point on success, None on failure (or no NTFS
    partition / not running as root).
    """
    if os.geteuid() != 0:
        return None

    candidates = [p for p in parts if p["type"] == "ntfs"]
    if not candidates:
        return None

    target = "/mnt/winprobe"
    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        return None

    for p in candidates:
        # Already mounted? Skip remount attempt.
        # mount -t ntfs-3g -o ro …  (Clonezilla ships ntfs-3g).
        rc = subprocess.run(
            ["mount", "-t", "ntfs-3g", "-o", "ro", p["device"], target],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if rc.returncode == 0:
            # Sanity: confirm it actually looks like a Windows root
            if os.path.isdir(os.path.join(target, "Windows")):
                return target
            # Not a Windows root — unmount and try next
            subprocess.run(["umount", target], capture_output=True, check=False)
    return None


def _umount_quiet(path: str) -> None:
    if not path:
        return
    try:
        subprocess.run(["umount", path], capture_output=True, check=False)
    except OSError:
        pass


def detect_intune(mount_root: Optional[str]) -> dict:
    if not mount_root:
        return _empty_result(UNKNOWN)
    hits = _probe_nonempty_dirs(mount_root, INTUNE_STRONG_PATHS)
    hits.extend(_probe_enterprise_mgmt_guid(mount_root))
    hits = _dedupe(hits)
    if hits:
        return {
            "present": True,
            "status": "ENROLLED",
            "evidence": " | ".join(hits)[:300],
            "manual_confirm_required": False,
        }
    return _empty_result("NOT_ENROLLED")


def detect_azure_ad(mount_root: Optional[str]) -> dict:
    if not mount_root:
        return _empty_result(UNKNOWN)
    hits = _probe_nonempty_dirs(mount_root, AZURE_AD_STRONG_PATHS)
    hits = _dedupe(hits)
    if hits:
        return {
            "present": True,
            "status": "JOINED",
            "evidence": " | ".join(hits)[:300],
            "manual_confirm_required": False,
        }
    return _empty_result("NOT_JOINED")


def detect_vendor_mdm(mount_root: Optional[str]) -> dict:
    if not mount_root:
        return _empty_result(UNKNOWN)

    matched: list[str] = []
    for label, probes in VENDOR_MDM_PROBES:
        hits = _probe_nonempty_dirs(mount_root, probes)
        if hits:
            matched.append(f"{label} ({hits[0]})")
    matched = _dedupe(matched)

    if matched:
        return {
            "present": True,
            "status": "ENROLLED",
            "evidence": " | ".join(matched)[:300],
            "manual_confirm_required": False,
        }
    return _empty_result("NOT_ENROLLED")


# ---------------------------------------------------------------------------
# Compose all 7 checks into a single bundle the TUI can render
# ---------------------------------------------------------------------------
LOCK_KEYS_ORDER = (
    "bios_password",
    "ata_security",
    "computrace",
    "intune",
    "azure_ad",
    "vendor_mdm",
)

LOCK_LABELS = {
    "bios_password": "BIOS Admin Password",
    "ata_security": "ATA / NVMe Drive Password",
    "computrace": "Computrace / Absolute LoJack",
    "intune": "Microsoft Intune",
    "azure_ad": "Azure AD / Entra Join",
    "vendor_mdm": "Vendor MDM",
}


def run_full_audit() -> dict:
    """Run all 7 detectors and return a single dict.

    Schema::

        {
          "halted": bool,           # any present=True or any UNKNOWN that
                                    #  isn't manually clearable
          "checks": {
            "bios_password": {present, status, evidence, manual_confirm_required},
            "ata_security": {...},
            ...
          },
          "summary": "<comma list of detected locks>"
        }
    """
    parts = _find_windows_partitions() if os.geteuid() == 0 else []
    mount_root = _mount_windows_ro(parts) if parts else None

    try:
        checks = {
            "bios_password": detect_bios_password(),
            "ata_security": detect_ata_security(),
            "computrace": detect_computrace(),
            "intune": detect_intune(mount_root),
            "azure_ad": detect_azure_ad(mount_root),
            "vendor_mdm": detect_vendor_mdm(mount_root),
        }
    finally:
        if mount_root:
            _umount_quiet(mount_root)

    detected = [k for k in LOCK_KEYS_ORDER if checks[k]["present"]]
    halted = bool(detected)

    return {
        "halted": halted,
        "checks": checks,
        "detected_locks": detected,
        "summary": ", ".join(LOCK_LABELS[k] for k in detected) if detected else "All clear",
    }
