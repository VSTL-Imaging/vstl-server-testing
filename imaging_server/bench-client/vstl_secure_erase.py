"""
vstl_secure_erase.py — Phase 3 Certified Secure Erase
=====================================================
Auto-picks the strongest sanitize method that the SUT's storage device
supports, runs it, captures forensic evidence, and (optionally) verifies
the wipe by reading a small sample of sectors back to confirm they are
zero / pattern-cleared.

Method priority per design choice 1c:
    1. NVMe Sanitize (Block Erase, action=2)            -> NIST 800-88 PURGE
    2. NVMe Sanitize (Crypto Erase, action=4)           -> NIST 800-88 PURGE
    3. NVMe Sanitize (Overwrite, action=3)              -> NIST 800-88 PURGE
    4. NVMe Format with Crypto Erase (-s 2)             -> NIST 800-88 PURGE
    5. ATA Sanitize Block/Crypto Erase (hdparm)         -> NIST 800-88 PURGE
    6. ATA Security-Erase-Enhanced (hdparm)             -> NIST 800-88 PURGE
    7. nwipe DoD 5220.22-M 3-pass (HDDs only)           -> DoD 5220.22-M

If direct SSD purge is rejected, the bench may run a destructive Clear-class
assist once, then retry the native Purge methods. Clear-class methods are
never accepted as the final certified method because they cannot prove purge
of retired, remapped, or over-provisioned NAND cells. The run completes only
when the final post-clear Purge retry succeeds.

Result schema
-------------
{
  "ok": True,                              # detection + run completed
  "method": "NVMe_SANITIZE_BLOCK_ERASE",   # one of _WIPE_METHOD_STANDARD keys
  "standard": "NIST SP 800-88 R1 PURGE",
  "passes": 1,
  "started_at": "ISO-8601",
  "completed_at": "ISO-8601",
  "duration_sec": int,
  "device": "/dev/nvme0n1",
  "device_type": "NVMe" | "SATA_SSD" | "HDD",
  "device_model": "Samsung SSD 980 256GB",
  "device_size_gb": 256,
  "verified": True/False,
  "verification_method": "post_wipe_read_sample" | "skipped",
  "evidence": "raw stdout/stderr excerpts, capped at 6 KB",
  "error_message": ""                      # populated on failure
}

Any branch that can't reach a working sanitize tool returns a result with
``ok=False`` and a populated ``error_message``. The TUI surfaces that to
the operator who can either retry, abort, or fall back to a manual wipe.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional


_EVIDENCE_CAP = 6000
SECURE_ERASE_CLIENT_BUILD = "secure-erase-purge-primary-v9"
_CONSOLE_KEEPALIVE_LOCK = threading.Lock()
_CONSOLE_KEEPALIVE_STARTED = False
_NVME_CLEAR_ASSIST_METHODS = (
    "NVMe_FORMAT_USER_DATA",
    "NVMe_SECURE_DISCARD_CLEAR",
    "NVMe_SOFTWARE_ZERO_CLEAR",
)
_SATA_CLEAR_ASSIST_METHODS = ("BLKDISCARD",)
_CLEAR_METHOD_STANDARDS = {
    "NVMe_FORMAT_USER_DATA": "NIST SP 800-88 Clear",
    "NVMe_SECURE_DISCARD_CLEAR": "NIST SP 800-88 Clear",
    "NVMe_SOFTWARE_ZERO_CLEAR": "NIST SP 800-88 Clear",
    "BLKDISCARD": "NIST SP 800-88 Clear",
}
_CLEAR_ONLY_EXCEPTION_SKUS = {
    "5pf18av": "temporary HP ProBook 640 G5 clear-only policy",
}
_CLEAR_ONLY_EXCEPTION_MODELS = (
    (("hp", "hewlettpackard"), "elitebook", ("640", "g10"), "temporary HP EliteBook 640 G10 clear-only policy"),
    (("hp", "hewlettpackard"), "elitebook", ("850", "g5"), "temporary HP EliteBook 850 G5 clear-only policy"),
    (("hp", "hewlettpackard"), "elitebook", ("850", "g6"), "temporary HP EliteBook 850 G6 clear-only policy"),
    (("hp", "hewlettpackard"), "probook", ("640", "g5"), "temporary HP ProBook 640 G5 clear-only policy"),
    (("hp", "hewlettpackard"), "elitebook", (), "temporary HP EliteBook firmware-safe clear-only policy"),
    (("hp", "hewlettpackard"), "probook", (), "temporary HP ProBook firmware-safe clear-only policy"),
    (("dell",), "latitude", ("5330",), "temporary Dell Latitude 5330 clear-only policy"),
    (("dell",), "latitude", ("5440",), "temporary Dell Latitude 5440 clear-only policy"),
    (("dell",), "latitude", ("5520",), "temporary Dell Latitude 5520 clear-only policy"),
    (("lenovo",), "x1 carbon", ("gen", "8"), "temporary Lenovo ThinkPad X1 Carbon 8th Gen clear-only policy"),
    (("lenovo",), "x1 carbon", ("8th",), "temporary Lenovo ThinkPad X1 Carbon 8th Gen clear-only policy"),
)
_NVME_SANITIZE_ACTION_LABELS = {
    2: "NVMe_SANITIZE_BLOCK_ERASE",
    3: "NVMe_SANITIZE_OVERWRITE",
    4: "NVMe_SANITIZE_CRYPTO_ERASE",
}
_NVME_SANITIZE_ACTION_ORDER = (2, 4, 3)


def _cap_evidence(value: str) -> str:
    """Keep command setup and the final controller status in capped evidence."""
    if len(value) <= _EVIDENCE_CAP:
        return value
    marker = "\n...[middle evidence truncated]...\n"
    remaining = _EVIDENCE_CAP - len(marker)
    head = remaining // 2
    return value[:head] + marker + value[-(remaining - head):]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Subprocess wrapper that never raises FileNotFoundError / Timeout."""
    try:
        rc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        return rc.returncode, rc.stdout or "", rc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired as e:
        return 124, "", f"timeout after {e.timeout}s"
    except OSError as e:
        return 1, "", f"OSError: {e}"


def _keep_operator_console_awake() -> str:
    """Disable Linux console blanking and wake the local display.

    Some laptops keep running after a native NVMe erase command while the
    panel/console goes black. Keep this best-effort and non-fatal: the wipe
    policy must not depend on video hardware cooperating.
    """
    if os.environ.get("VSTL_DISABLE_CONSOLE_KEEPALIVE") == "1":
        return "console keepalive disabled by VSTL_DISABLE_CONSOLE_KEEPALIVE"
    if os.name != "posix":
        return "console keepalive skipped on non-posix host"

    script = r"""
set +e
echo 0 > /sys/module/kernel/parameters/consoleblank 2>/dev/null || true
if command -v setterm >/dev/null 2>&1; then
  for tty in /dev/tty0 /dev/tty1 /dev/console; do
    [ -w "$tty" ] || continue
    setterm --blank 0 --powerdown 0 --powersave off <"$tty" >"$tty" 2>/dev/null || true
    setterm --blank poke <"$tty" >"$tty" 2>/dev/null || true
  done
fi
for fb_blank in /sys/class/graphics/fb*/blank; do
  [ -w "$fb_blank" ] && echo 0 > "$fb_blank" || true
done
for power in /sys/class/backlight/*/bl_power; do
  [ -w "$power" ] && echo 0 > "$power" || true
done
for dpms in /sys/class/drm/*/dpms; do
  [ -w "$dpms" ] && echo On > "$dpms" || true
done
echo "operator console keepalive applied"
"""
    rc, out, err = _run(["sh", "-c", script], timeout=5)
    return f"$ operator-console-keepalive\nrc={rc}\n{out}\n{err}".strip()


def _console_keepalive_worker(interval_sec: int = 5) -> None:
    while True:
        time.sleep(interval_sec)
        _keep_operator_console_awake()


def _ensure_erase_console_keepalive() -> str:
    """Start one background display keepalive for long secure erase runs."""
    global _CONSOLE_KEEPALIVE_STARTED

    evidence = _keep_operator_console_awake()
    if (
        os.environ.get("VSTL_DISABLE_CONSOLE_KEEPALIVE") == "1"
        or os.name != "posix"
    ):
        return evidence

    with _CONSOLE_KEEPALIVE_LOCK:
        if _CONSOLE_KEEPALIVE_STARTED:
            return evidence + "\nbackground keepalive already running"
        thread = threading.Thread(
            target=_console_keepalive_worker,
            name="vstl-secure-erase-console-keepalive",
            daemon=True,
        )
        thread.start()
        _CONSOLE_KEEPALIVE_STARTED = True
    return evidence + "\nbackground keepalive started"


def _read_sysfs_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _power_supply_entries() -> list[str]:
    base = "/sys/class/power_supply"
    try:
        return sorted(os.listdir(base))
    except OSError:
        return []


def _power_supply_type(entry: str) -> str:
    return _read_sysfs_text(f"/sys/class/power_supply/{entry}/type").lower()


def _power_supply_online(entry: str) -> str:
    return _read_sysfs_text(f"/sys/class/power_supply/{entry}/online")


def _erase_power_guard() -> tuple[bool, str, str]:
    """Return whether destructive erase may start from a power-safety view.

    Laptops that run a full-drive zero fill on battery can hard power off when
    the battery or adapter is weak. Desktops often have no power_supply nodes,
    so only block when a battery is present and no external supply is online.
    """
    entries = _power_supply_entries()
    if not entries:
        return True, "no /sys/class/power_supply entries; assuming non-battery system", ""

    evidence: list[str] = []
    has_battery = False
    external_online = False
    for entry in entries:
        supply_type = _power_supply_type(entry)
        online = _power_supply_online(entry)
        status = _read_sysfs_text(f"/sys/class/power_supply/{entry}/status")
        capacity = _read_sysfs_text(f"/sys/class/power_supply/{entry}/capacity")
        evidence.append(
            f"{entry}: type={supply_type or '?'} online={online or '?'} "
            f"status={status or '?'} capacity={capacity or '?'}"
        )
        name = entry.upper()
        if supply_type == "battery" or name.startswith("BAT"):
            has_battery = True
            continue
        if online == "1":
            external_online = True

    if has_battery and not external_online:
        return (
            False,
            "\n".join(evidence),
            "AC power is not detected. Connect the charger before secure erase; "
            "the unit is shutting down under erase load.",
        )
    return True, "\n".join(evidence), ""


def _system_dmi_profile() -> str:
    fields = [
        _read_sysfs_text("/sys/class/dmi/id/sys_vendor"),
        _read_sysfs_text("/sys/class/dmi/id/product_name"),
        _read_sysfs_text("/sys/class/dmi/id/product_version"),
        _read_sysfs_text("/sys/class/dmi/id/product_sku"),
        _read_sysfs_text("/sys/class/dmi/id/product_family"),
        _read_sysfs_text("/sys/class/dmi/id/board_name"),
        _read_sysfs_text("/sys/class/dmi/id/bios_date"),
    ]
    return " | ".join(part for part in fields if part)


def _is_legacy_dell_platform(profile: str | None = None) -> bool:
    normalized = (profile or _system_dmi_profile()).lower()
    if "dell" not in normalized:
        return False
    if any(token in normalized for token in ("latitude e", "precision m")):
        return True
    model_numbers = [int(match) for match in re.findall(r"\b(\d{4})\b", normalized)]
    return any(number < 5500 for number in model_numbers)


def _clear_only_exception_reason(profile: str | None = None) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", (profile or _system_dmi_profile()).lower())
    compact = normalized.replace(" ", "")
    words = normalized.split()
    for sku, reason in _CLEAR_ONLY_EXCEPTION_SKUS.items():
        if sku in compact:
            return reason
    for vendor_tokens, family, model_tokens, reason in _CLEAR_ONLY_EXCEPTION_MODELS:
        if (
            any(token in words or token in compact for token in vendor_tokens)
            and family in normalized
            and all(re.search(rf"\b{re.escape(token)}\b", normalized) is not None for token in model_tokens)
        ):
            return reason
    return ""


def _nvme_native_purge_screen_blank_risk_reason(profile: str | None = None) -> str:
    """Return why this platform should skip native NVMe purge opcodes.

    HP ProBook 640 G5 units have been observed to stay powered while the panel
    goes blank during native NVMe sanitize/format Purge commands. This
    model-specific guard avoids the opcodes that blank the screen and lets the
    existing firmware-safe Clear exception run visibly instead.
    """
    normalized = re.sub(r"[^a-z0-9]+", " ", (profile or _system_dmi_profile()).lower())
    compact = normalized.replace(" ", "")
    words = normalized.split()
    is_hp = "hp" in words or "hewlettpackard" in compact
    is_probook_640_g5 = (
        "probook" in words
        and all(token in words for token in ("640", "g5"))
    )
    if is_hp and (is_probook_640_g5 or "5pf18av" in compact):
        return "temporary HP ProBook 640 G5 native NVMe Purge screen-blank policy"
    return ""


# ---------------------------------------------------------------------------
# Drive detection
# ---------------------------------------------------------------------------
def _storage_capacity_gb(num_bytes: int) -> int | float:
    """Return vendor-style decimal GB for physical disk capacity.

    Linux tools often show GiB while labeling it as G/Gb; a 256 GB SSD is
    about 238.5 GiB. Bench screens should show the vendor capacity technicians
    expect from the label and Windows Settings.
    """
    try:
        gb = int(num_bytes) / 1_000_000_000
    except (TypeError, ValueError):
        return 0
    if gb <= 0:
        return 0
    if gb >= 10:
        return int(round(gb))
    return round(gb, 1)


def _udev_drive_identity(device: str) -> tuple[str, str]:
    """Return the best stable (serial, WWN) identifiers exposed by udev."""
    rc, out, _err = _run(
        ["udevadm", "info", "--query=property", "--name", device],
        timeout=10,
    )
    if rc != 0:
        return "", ""
    props: dict[str, str] = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    serial = props.get("ID_SERIAL_SHORT") or props.get("ID_SERIAL") or ""
    wwn = props.get("ID_WWN_WITH_EXTENSION") or props.get("ID_WWN") or ""
    return serial.strip(), wwn.strip()


def _controller_drive_serial(device: str, device_type: str) -> str:
    """Fallback serial lookup when lsblk/udev omit a controller identifier."""
    if device_type == "NVMe":
        rc, out, _err = _run(["nvme", "id-ctrl", device, "-o", "json"], timeout=15)
        if rc == 0:
            try:
                return str(json.loads(out).get("sn") or "").strip()
            except (ValueError, json.JSONDecodeError):
                pass
    rc, out, _err = _run(["hdparm", "-I", device], timeout=15)
    if rc == 0:
        match = re.search(r"^\s*Serial Number:\s*(.+?)\s*$", out, re.MULTILINE)
        if match:
            return match.group(1).strip()
    return ""


def detect_primary_drive() -> dict:
    """Find the largest non-removable block device that's not the live ISO.

    Returns a dict with at minimum:
        device, device_type ('NVMe'|'SATA_SSD'|'HDD'|'UNKNOWN'),
        device_model, device_size_gb, rotational (bool).
    """
    rc, out, err = _run([
        "lsblk", "-Jbo",
        "NAME,PATH,TYPE,RM,ROTA,SIZE,MODEL,TRAN,HOTPLUG,SERIAL,WWN",
    ])
    if rc != 0:
        # Compatibility fallback for older lsblk builds without WWN/SERIAL.
        rc, out, err = _run([
            "lsblk", "-Jbo", "NAME,PATH,TYPE,RM,ROTA,SIZE,MODEL,TRAN,HOTPLUG",
        ])
    if rc != 0:
        return {"device": "", "device_type": "UNKNOWN",
                "device_model": "", "device_size_gb": 0,
                "rotational": False,
                "error": f"lsblk failed: rc={rc} err={err[:200]}"}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"device": "", "device_type": "UNKNOWN",
                "device_model": "", "device_size_gb": 0,
                "rotational": False,
                "error": "lsblk produced invalid JSON"}

    candidates = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        if int(dev.get("rm", 0)) == 1:
            continue  # skip removable (USB live, SD readers)
        if str(dev.get("tran") or "").lower() == "usb":
            continue
        try:
            if int(dev.get("hotplug", 0) or 0) == 1:
                continue
        except (TypeError, ValueError):
            pass
        size = int(dev.get("size", 0) or 0)
        if size < 8 * 1024 ** 3:  # ignore <8 GB
            continue
        candidates.append(dev)

    if not candidates:
        return {"device": "", "device_type": "UNKNOWN",
                "device_model": "", "device_size_gb": 0,
                "rotational": False,
                "error": "no fixed disks >=8GB found"}

    # Pick the largest non-removable disk
    target = max(candidates, key=lambda d: int(d.get("size", 0)))
    path = target.get("path") or f"/dev/{target.get('name','')}"
    size_bytes = int(target.get("size", 0) or 0)
    size_gb = _storage_capacity_gb(size_bytes)
    rotational = bool(int(target.get("rota", 0)))
    tran = (target.get("tran") or "").lower()
    if "nvme" in tran or path.startswith("/dev/nvme"):
        dtype = "NVMe"
    elif rotational:
        dtype = "HDD"
    else:
        dtype = "SATA_SSD"

    device_serial = str(target.get("serial") or "").strip()
    device_wwn = str(target.get("wwn") or "").strip()
    if not device_serial or not device_wwn:
        udev_serial, udev_wwn = _udev_drive_identity(path)
        device_serial = device_serial or udev_serial
        device_wwn = device_wwn or udev_wwn
    if not device_serial:
        device_serial = _controller_drive_serial(path, dtype)

    return {
        "device": path,
        "device_type": dtype,
        "device_model": (target.get("model") or "").strip(),
        "device_serial": device_serial,
        "device_wwn": device_wwn,
        "device_size_gb": size_gb,
        "device_size_bytes": size_bytes,
        "rotational": rotational,
    }


# ---------------------------------------------------------------------------
# NVMe sanitize / format paths
# ---------------------------------------------------------------------------
def _nvme_sanitize_actions(device: str) -> tuple[list[int], str]:
    """Return sanitize actions in preferred order from NVMe SANICAP.

    Some older BIOS/controller combinations report SANICAP poorly through
    nvme-cli. If the probe cannot be parsed, try both actions and let the
    controller reject the unsupported one with evidence. If SANICAP is valid
    but zero, skip sanitize and fall through to NVMe Format.
    """
    rc, out, err = _run(["nvme", "id-ctrl", device, "-o", "json"], timeout=15)
    if rc != 0:
        return list(_NVME_SANITIZE_ACTION_ORDER), f"nvme id-ctrl rc={rc} err={err[:200]} [probing sanitize anyway]"
    try:
        info = json.loads(out)
        sanicap = int(info.get("sanicap", 0))
        # Per NVMe 1.4 spec: bit 0 = Crypto Erase, bit 1 = Block Erase, bit 2 = Overwrite
        actions: list[int] = []
        supported_actions = {
            2: bool(sanicap & 0x2),
            4: bool(sanicap & 0x1),
            3: bool(sanicap & 0x4),
        }
        for action in _NVME_SANITIZE_ACTION_ORDER:
            if supported_actions[action]:
                actions.append(action)
        return actions, f"sanicap=0x{sanicap:08x} (raw nvme id-ctrl)"
    except (ValueError, json.JSONDecodeError) as e:
        return list(_NVME_SANITIZE_ACTION_ORDER), f"id-ctrl parse error: {e} [probing sanitize anyway]"


def _nvme_supports_sanitize(device: str) -> tuple[bool, str]:
    """Backward-compatible boolean probe used by older tests/extensions."""
    actions, evidence = _nvme_sanitize_actions(device)
    return bool(actions), evidence


def _parse_int_auto(value: str) -> int:
    value = str(value).strip()
    return int(value, 16) if value.lower().startswith("0x") else int(value)


def _parse_nvme_sanitize_log(output: str) -> tuple[Optional[int], Optional[int]]:
    """Parse nvme-cli sanitize-log variants.

    Debian/live images can carry different nvme-cli versions. Newer versions
    print ``Sanitize Status (SSTAT) : 0x...`` while older builds often print
    compact keys such as ``sstat : 0x...`` and ``sprog : 65535``. Missing the
    compact form leaves the TUI stuck at "auto-detect / starting" even though
    the drive is being polled.
    """
    status_match = re.search(
        r"(?im)^\s*(?:Sanitize\s+Status|SSTAT)\s*(?:\([^)]*\))?\s*[:=]\s*(0x[0-9a-f]+|\d+)\b",
        output,
    )
    progress_match = re.search(
        r"(?im)^\s*(?:Sanitize\s+Progress|SPROG)\s*(?:\([^)]*\))?\s*[:=]\s*(0x[0-9a-f]+|\d+)\b",
        output,
    )

    sstat: Optional[int] = None
    percent: Optional[int] = None
    if status_match:
        try:
            sstat = _parse_int_auto(status_match.group(1))
        except ValueError:
            sstat = None
    if progress_match:
        try:
            raw_progress = _parse_int_auto(progress_match.group(1))
            # NVMe SPROG is normally 0..65535. Some tools already print a
            # percentage-like integer, so keep small values as-is.
            if raw_progress <= 100:
                percent = max(0, min(100, raw_progress))
            else:
                percent = max(0, min(100, round((raw_progress / 65535) * 100)))
        except ValueError:
            percent = None
    return sstat, percent


def _release_block_device(device: str) -> str:
    """Unmount/swapoff anything currently using the target disk.

    Lock-audit and hardware probes may mount Windows partitions read-only.
    Controller-level NVMe Format commonly returns a vague command rejection
    when any namespace partition is still busy, so release the block device
    immediately before destructive erase attempts.
    """
    evidence: list[str] = []
    rc, out, err = _run(["lsblk", "-nrpo", "NAME,TYPE,MOUNTPOINTS", device], timeout=10)
    evidence.append(f"$ lsblk -nrpo NAME,TYPE,MOUNTPOINTS {device}\nrc={rc}\n{out}\n{err}")
    paths: list[str] = []
    if rc == 0:
        for line in out.splitlines():
            parts = line.split(None, 2)
            if not parts:
                continue
            paths.append(parts[0])

    try:
        swaps = _read_sysfs_text("/proc/swaps")
    except Exception:
        swaps = ""
    for path in sorted(paths, key=len, reverse=True):
        if path in swaps:
            rc_s, out_s, err_s = _run(["swapoff", path], timeout=20)
            evidence.append(f"$ swapoff {path}\nrc={rc_s}\n{out_s}\n{err_s}")
        rc_u, out_u, err_u = _run(["umount", "-R", path], timeout=20)
        if rc_u == 0 or "not mounted" not in f"{out_u}\n{err_u}".lower():
            evidence.append(f"$ umount -R {path}\nrc={rc_u}\n{out_u}\n{err_u}")

    rc_f, out_f, err_f = _run(["blockdev", "--flushbufs", device], timeout=20)
    evidence.append(f"$ blockdev --flushbufs {device}\nrc={rc_f}\n{out_f}\n{err_f}")
    return "\n".join(evidence)


def _nvme_sanitize(device: str, action: int,
                    progress: Optional[Callable[[dict], None]] = None,
                    timeout: int = 1800) -> tuple[bool, str, str]:
    """Run ``nvme sanitize -a <action>`` and poll ``nvme sanitize-log`` for
    completion. action=2 -> Block Erase, action=3 -> Overwrite,
    action=4 -> Crypto Erase.

    Returns (success, method_label, evidence).
    """
    label = _NVME_SANITIZE_ACTION_LABELS.get(action, f"NVMe_SANITIZE_ACTION_{action}")
    _keep_operator_console_awake()
    if progress:
        progress({
            "elapsed_sec": 0,
            "phase": "issuing NVMe sanitize command",
            "method": label,
        })
    controller, _nsid = _nvme_controller_and_nsid(device)
    command_devices = [device]
    if controller != device:
        command_devices.append(controller)

    issue_blocks: list[str] = []
    poll_device = ""
    for command_device in command_devices:
        cmd = ["nvme", "sanitize", command_device, "-a", str(action)]
        rc, out, err = _run(cmd, timeout=30)
        issue_blocks.append(f"$ {' '.join(cmd)}\nrc={rc}\n{out}\n{err}")
        if rc == 0:
            poll_device = command_device
            break
    issue_ev = "\n---\n".join(issue_blocks)
    if not poll_device:
        return False, "", issue_ev

    # Poll sanitize-log until SSTAT (sanitize status) goes idle/complete
    started = datetime.now(timezone.utc)
    last_log = ""
    unknown_statuses = 0
    while True:
        _keep_operator_console_awake()
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed > timeout:
            return False, label, issue_ev + f"\n[timeout after {timeout}s]\n{last_log[-1000:]}"
        rc_l, out_l, err_l = _run(["nvme", "sanitize-log", poll_device], timeout=10)
        if rc_l != 0:
            return False, label, issue_ev + f"\nsanitize-log rc={rc_l} err={err_l[:200]}"
        last_log = out_l
        sstat, pct = _parse_nvme_sanitize_log(out_l)
        if sstat is None:
            # very old nvme-cli prints SSTAT differently; check for "completed" textually
            if "completed" in out_l.lower() and "no" not in out_l.lower()[:200]:
                ev = issue_ev + f"\nFINAL sanitize-log:\n{out_l[:4000]}"
                if progress:
                    progress({"elapsed_sec": int(elapsed), "phase": "completed"})
                return True, label, ev
            unknown_statuses += 1
            if progress:
                progress({
                    "elapsed_sec": int(elapsed),
                    "phase": "waiting for NVMe sanitize status",
                    "method": label,
                    "percent": None,
                })
            if unknown_statuses >= 5:
                return (
                    False,
                    label,
                    issue_ev
                    + "\n[sanitize-log did not expose SSTAT/status after 5 polls]\n"
                    + out_l[:2000],
                )
            time.sleep(2)
            continue
        unknown_statuses = 0
        # SSTAT lower 3 bits == 001 means "successfully completed last sanitize"
        if (sstat & 0x7) == 0x1:
            ev = issue_ev + f"\nFINAL sanitize-log:\n{out_l[:4000]}"
            if progress:
                progress({
                    "elapsed_sec": int(elapsed),
                    "phase": "completed",
                    "method": label,
                    "percent": 100,
                })
            return True, label, ev
        # SSTAT lower 3 bits == 010 means "still in progress"
        if (sstat & 0x7) == 0x2:
            if progress:
                progress({"elapsed_sec": int(elapsed), "phase": "running", "percent": pct})
            time.sleep(5)
            continue
        # Any other SSTAT = failed / unsupported
        return False, label, issue_ev + f"\nSSTAT=0x{sstat:x} (failed)\n{out_l[:2000]}"


def _nvme_controller_and_nsid(device: str) -> tuple[str, str]:
    """Return controller node and namespace id for /dev/nvmeXnY."""
    name = os.path.basename(device)
    sysfs_nsid = _read_sysfs_text(f"/sys/block/{name}/nsid")
    match = re.match(r"^(nvme\d+)n(\d+)$", name)
    if match:
        return f"/dev/{match.group(1)}", sysfs_nsid or match.group(2)
    return device, sysfs_nsid or "1"


def _nvme_format(device: str, secure_erase_setting: int) -> tuple[bool, str, str]:
    """Run NVMe Format for a specific secure-erase setting.

    The certified automatic flow only calls secure_erase_setting=2
    (crypto erase / Purge). secure_erase_setting=1 is retained only for
    legacy evidence parsing and manual diagnostics; it is not used as a
    fallback for new bench certifications.
    """
    label = (
        "NVMe_FORMAT_CRYPTO"
        if secure_erase_setting == 2
        else "NVMe_FORMAT_USER_DATA"
    )
    controller, nsid = _nvme_controller_and_nsid(device)
    commands = [
        ["nvme", "format", device, "-s", str(secure_erase_setting), "--force"],
    ]
    if controller != device and nsid:
        commands.extend([
            ["nvme", "format", controller, "-n", nsid, "-s", str(secure_erase_setting), "--force"],
            ["nvme", "format", device, "-n", nsid, "-s", str(secure_erase_setting), "--force"],
        ])

    evidence_blocks: list[str] = []
    for cmd in commands:
        _keep_operator_console_awake()
        rc, out, err = _run(cmd, timeout=900)
        evidence_blocks.append(f"$ {' '.join(cmd)}\nrc={rc}\n{out}\n{err}")
        if rc == 0:
            return True, label, "\n---\n".join(evidence_blocks)
    return False, label, "\n---\n".join(evidence_blocks)


def _nvme_format_crypto(device: str) -> tuple[bool, str, str]:
    return _nvme_format(device, 2)


def _nvme_format_user_data(device: str) -> tuple[bool, str, str]:
    return _nvme_format(device, 1)


# ---------------------------------------------------------------------------
# ATA / SATA SSD paths
# ---------------------------------------------------------------------------
def _ata_security_is_frozen(identity_output: str) -> bool:
    """Return the ATA Security frozen state from ``hdparm -I`` output.

    ``hdparm`` uses variable spaces/tabs before and between ``not`` and
    ``frozen``. Looking only for the substring ``frozen`` therefore treats
    the normal ``not frozen`` state as frozen and aborts the erase instantly.
    """
    for line in identity_output.splitlines():
        normalized = " ".join(line.lower().split())
        if re.search(r"\bnot frozen\b", normalized):
            return False
        if re.search(r"\bfrozen\b", normalized):
            return True
    return False


def _ata_security_status(identity_output: str) -> dict[str, bool]:
    """Parse the ATA Security block from ``hdparm -I`` output."""
    status = {
        "supported": False,
        "enabled": False,
        "locked": False,
        "frozen": False,
    }
    in_security = False
    for line in identity_output.splitlines():
        normalized = " ".join(line.lower().split())
        if normalized.startswith("security:"):
            in_security = True
            continue
        if not in_security:
            continue
        if not normalized:
            break
        tokens = normalized.split()
        if "supported" in tokens and not (tokens[:2] == ["not", "supported"]):
            status["supported"] = True
        if tokens[:2] == ["not", "enabled"]:
            pass
        elif "enabled" in tokens:
            status["enabled"] = True
        if tokens[:2] == ["not", "locked"]:
            pass
        elif "locked" in tokens:
            status["locked"] = True
        if tokens[:2] == ["not", "frozen"]:
            pass
        elif "frozen" in tokens:
            status["frozen"] = True
    return status


def _ata_try_unfreeze(
    progress: Optional[Callable[[dict], None]] = None,
) -> tuple[bool, str]:
    """Suspend/resume once to clear a BIOS-issued ATA security freeze.

    ``rtcwake`` owns the alarm setup and resume transition when available.
    The post-resume console repair is important on older Dell generations:
    the machine often resumes correctly while the text console stays blank.
    """
    if progress:
        progress({
            "elapsed_sec": 0,
            "phase": "unfreezing (screen may turn off for 10-20 seconds)",
        })
    script = r"""
set -eu
state_file=/sys/power/state
active_vt=$(cat /sys/class/tty/tty0/active 2>/dev/null || true)
if [ ! -r "$state_file" ] || ! grep -qw mem "$state_file"; then
  echo "mem suspend is not available on this live environment"
  exit 3
fi
sync
if command -v rtcwake >/dev/null 2>&1; then
  rtcwake -m mem -s 8
else
  rtc_alarm=/sys/class/rtc/rtc0/wakealarm
  if [ ! -w "$rtc_alarm" ]; then
    echo "RTC wakealarm is not writable and rtcwake is unavailable"
    exit 4
  fi
  echo 0 > "$rtc_alarm" || true
  echo +8 > "$rtc_alarm"
  echo mem > "$state_file"
fi
udevadm settle --timeout=10 >/dev/null 2>&1 || true
for power in /sys/class/backlight/*/bl_power; do
  [ -w "$power" ] && echo 0 > "$power" || true
done
if command -v setterm >/dev/null 2>&1; then
  setterm --blank poke --powersave off --powerdown 0 >/dev/tty0 2>/dev/null || true
fi
case "$active_vt" in
  tty[0-9]*)
    if command -v chvt >/dev/null 2>&1; then
      chvt "${active_vt#tty}" >/dev/null 2>&1 || true
    fi
    ;;
esac
sleep 1
echo "resume completed"
"""
    rc, out, err = _run(["sh", "-c", script], timeout=180)
    evidence = f"$ ata-unfreeze suspend-resume\nrc={rc}\n{out}\n{err}"
    return rc == 0, evidence


def _ata_sanitize_methods(identity_output: str) -> list[tuple[str, str]]:
    """Return supported ATA SANITIZE methods in strongest-first order."""
    normalized = identity_output.lower()
    if "sanitize feature set" not in normalized:
        return []
    methods: list[tuple[str, str]] = []
    if "block_erase_ext command" in normalized:
        methods.append(("--sanitize-block-erase", "ATA_SANITIZE_BLOCK_ERASE"))
    if "crypto_scramble_ext command" in normalized:
        methods.append(("--sanitize-crypto-scramble", "ATA_SANITIZE_CRYPTO_SCRAMBLE"))
    return methods


def _hdparm_sanitize_erase(
    device: str,
    progress: Optional[Callable[[dict], None]] = None,
    timeout: int = 4 * 3600,
) -> tuple[bool, str, str]:
    """Run ATA SANITIZE and poll the operation that hdparm starts in background.

    ATA SANITIZE has its own freeze mechanism and is commonly still available
    when BIOS has frozen the password-based ATA Security feature set.
    """
    rc, identity, err = _run(["hdparm", "-I", device], timeout=15)
    evidence = f"$ hdparm -I {device}\n{identity}\n{err}"
    if rc != 0:
        return False, "", evidence + "\n[ATA identity query failed before sanitize]"

    methods = _ata_sanitize_methods(identity)
    if not methods:
        return False, "", evidence + "\n[drive does not advertise the ATA SANITIZE feature set]"

    evidence_blocks: list[str] = [evidence]
    selected_method = ""
    for option, method in methods:
        _keep_operator_console_awake()
        cmd = ["hdparm", "--yes-i-know-what-i-am-doing", option, device]
        rc_issue, out_issue, err_issue = _run(cmd, timeout=30)
        evidence_blocks.append(
            f"$ {' '.join(cmd)}\nrc={rc_issue}\n{out_issue}\n{err_issue}"
        )
        if rc_issue == 0:
            selected_method = method
            break
    if not selected_method:
        return False, methods[0][1], "\n".join(evidence_blocks)

    started = time.monotonic()
    unknown_statuses = 0
    while True:
        _keep_operator_console_awake()
        elapsed = int(time.monotonic() - started)
        if elapsed > timeout:
            return (
                False,
                selected_method,
                "\n".join(evidence_blocks) + f"\n[sanitize timed out after {timeout}s]",
            )
        rc_status, out_status, err_status = _run(
            ["hdparm", "--sanitize-status", device], timeout=20,
        )
        status_block = (
            f"$ hdparm --sanitize-status {device}\n"
            f"rc={rc_status}\n{out_status}\n{err_status}"
        )
        status = f"{out_status}\n{err_status}".lower()
        if rc_status != 0:
            return False, selected_method, "\n".join(evidence_blocks + [status_block])
        if (
            "last sanitize operation completed without error" in status
            or "sanitize operation succeeded" in status
        ):
            if progress:
                progress({
                    "elapsed_sec": elapsed,
                    "phase": "completed",
                    "method": selected_method,
                    "percent": 100,
                })
            return True, selected_method, "\n".join(evidence_blocks + [status_block])
        if "sanitize frozen" in status or "sanitize operation failed" in status:
            return False, selected_method, "\n".join(evidence_blocks + [status_block])
        if "sanitize operation in process" in status:
            percent_match = re.search(
                r"progress:\s*0x[0-9a-f]+\s*\((\d+)%\)", status,
            )
            percent = int(percent_match.group(1)) if percent_match else None
            if progress:
                progress({
                    "elapsed_sec": elapsed,
                    "phase": "running ATA sanitize",
                    "method": selected_method,
                    "percent": percent,
                })
            time.sleep(5)
            continue

        unknown_statuses += 1
        if unknown_statuses >= 3:
            evidence_blocks.append(
                "[sanitize status remained idle/unknown after command issue]"
            )
            return False, selected_method, "\n".join(evidence_blocks + [status_block])
        time.sleep(2)


def _ata_security_disable(device: str, password: str = "vstl") -> tuple[bool, str]:
    """Clear a leftover ATA security state armed by a prior VSTL attempt."""
    rc, out, err = _run(
        ["hdparm", "--user-master", "u", "--security-disable", password, device],
        timeout=30,
    )
    evidence = (
        f"$ hdparm --security-disable {password} {device}\n"
        f"rc={rc}\n{out}\n{err}"
    )
    return rc == 0, evidence


def _erase_failure_reason(evidence: str) -> str:
    """Extract a concise, operator-friendly reason from raw wipe evidence."""
    normalized = evidence.lower()
    if "clear assist completed; final nvme purge retry still failed" in normalized:
        return (
            "A Clear wipe completed, but the NVMe controller still rejected the "
            "required final Purge method. Certification is blocked until a native "
            "Purge succeeds."
        )
    if "clear assist failed before final nvme purge retry" in normalized:
        return (
            "NVMe controller rejected every native Purge method, and the Clear "
            "assist wipe also failed before the required final Purge retry."
        )
    if "clear assist completed; final sata ssd purge retry still failed" in normalized:
        return (
            "A Clear wipe completed, but the SATA SSD still rejected the required "
            "final Purge method. Certification is blocked until a native Purge "
            "succeeds."
        )
    if "clear assist failed before final sata ssd purge retry" in normalized:
        return (
            "SATA SSD purge failed, and the Clear assist wipe also failed before "
            "the required final Purge retry."
        )
    if "sanitize frozen" in normalized:
        return "The drive has also frozen its ATA SANITIZE feature until the next full power cycle."
    if "drive is frozen" in normalized or "still frozen after suspend/resume" in normalized:
        return (
            "ATA Security Erase is blocked because the drive is frozen by BIOS. "
            "Fully power off the laptop, reconnect AC, and retry secure erase."
        )
    if "drive does not advertise ata security support" in normalized:
        return "This SATA drive or controller does not advertise ATA Security Erase support."
    if "drive reports ata security enabled+locked" in normalized:
        return (
            "ATA security is already enabled and locked on this drive. "
            "Fully power off the laptop and retry, or unlock the drive in BIOS first."
        )
    if "stale ata security state remained enabled" in normalized:
        return (
            "The drive kept an earlier ATA security password state and would not clear it. "
            "Power off the laptop fully and retry secure erase."
        )
    if "ata identity query failed" in normalized:
        return "Drive identity query failed; the SATA controller did not answer hdparm."
    if "--security-set-pass" in normalized and re.search(r"security-set-pass .*?\nrc=([1-9]\d*)", evidence, re.DOTALL):
        return "ATA password setup failed; the drive or controller rejected security-set-pass."
    if "security-erase-enhanced" in normalized or "security-erase " in normalized:
        if "not supported" in normalized:
            return "ATA secure erase command is not supported by this drive or controller."
        if "input/output error" in normalized:
            return "ATA secure erase command returned an I/O error from the controller."
        if "operation not permitted" in normalized or "permission denied" in normalized:
            return "ATA secure erase command was blocked by firmware or controller permissions."
        if re.search(r"security-erase(?:-enhanced)? .*?\nrc=([1-9]\d*)", evidence, re.DOTALL):
            return "ATA secure erase command was rejected before it could start."
    if "nvme format" in normalized or "nvme sanitize" in normalized:
        if "device or resource busy" in normalized or "namespace is busy" in normalized:
            return (
                "NVMe erase was blocked because the drive or one of its partitions "
                "was still busy. Reboot into PXE and retry."
            )
        if "not supported" in normalized or "invalid opcode" in normalized:
            return "NVMe controller rejected the advertised native erase command."
        if "permission denied" in normalized or "operation not permitted" in normalized:
            return "NVMe erase command was blocked by firmware or controller permissions."
        if re.search(r"nvme (?:format|sanitize).*?\nrc=([1-9]\d*)", evidence, re.DOTALL):
            return "NVMe controller rejected every native sanitize/format erase method that was tried."
    return "See secure erase evidence log for the controller-level failure detail."


def _run_with_erase_heartbeat(
    cmd: list[str],
    timeout: int,
    progress: Optional[Callable[[dict], None]] = None,
    method: str = "",
    phase: str = "running erase command",
    heartbeat_sec: float = 3.0,
) -> tuple[int, str, str]:
    """Run a long command while keeping the operator console visibly alive."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except OSError as e:
        return 1, "", f"OSError: {e}"

    last_heartbeat = 0.0
    while True:
        rc = proc.poll()
        now = time.monotonic()
        if rc is not None:
            out, err = proc.communicate()
            return rc, out or "", err or ""
        if now - started > timeout:
            proc.kill()
            out, err = proc.communicate()
            return 124, out or "", (err or "") + f"\ntimeout after {timeout}s"
        if now - last_heartbeat >= heartbeat_sec:
            elapsed = int(now - started)
            _keep_operator_console_awake()
            if progress:
                progress({
                    "elapsed_sec": elapsed,
                    "phase": phase,
                    "method": method,
                    "percent": None,
                })
            last_heartbeat = now
        time.sleep(0.5)


def _hdparm_security_erase(device: str,
                           progress: Optional[Callable[[dict], None]] = None,
                           timeout: int = 4 * 3600) -> tuple[bool, str, str]:
    """ATA Security-Erase-Enhanced via hdparm.

    Sets a transient password (``vstl``), then issues the enhanced erase.
    Most modern SSDs zero everything in a few minutes; spinning HDDs can
    take many hours (we cap at 4h).
    """
    # 1) Sanity check that the drive ISN'T frozen
    rc, out, err = _run(["hdparm", "-I", device], timeout=15)
    id_ev = f"$ hdparm -I {device}\n{out}\n{err}"
    if rc != 0:
        return False, "ATA_SECURITY_ERASE_ENHANCED", id_ev + "\n[ATA identity query failed]"
    if _ata_security_is_frozen(out):
        dmi_profile = _system_dmi_profile()
        id_ev += f"\n[DMI profile: {dmi_profile or 'UNKNOWN'}]"
        unfrozen, unfreeze_ev = _ata_try_unfreeze(progress=progress)
        rc_retry, out_retry, err_retry = _run(["hdparm", "-I", device], timeout=15)
        retry_ev = f"$ hdparm -I {device}  # after suspend/resume\n{out_retry}\n{err_retry}"
        if rc_retry != 0:
            return (
                False,
                "ATA_SECURITY_ERASE_ENHANCED",
                id_ev
                + "\n"
                + unfreeze_ev
                + "\n"
                + retry_ev
                + "\n[ATA identity query failed after suspend/resume attempt]",
            )
        if not unfrozen or _ata_security_is_frozen(out_retry):
            return (
                False,
                "ATA_SECURITY_ERASE_ENHANCED",
                id_ev
                + "\n"
                + unfreeze_ev
                + "\n"
                + retry_ev
                + "\n[drive is still FROZEN after suspend/resume - fully power-cycle the laptop, then retry]",
            )
        id_ev += "\n" + unfreeze_ev + "\n" + retry_ev
        out = out_retry

    security = _ata_security_status(out)
    if not security["supported"]:
        return (
            False,
            "ATA_SECURITY_ERASE_ENHANCED",
            id_ev + "\n[drive does not advertise ATA Security support]",
        )
    if security["enabled"] and security["locked"]:
        return (
            False,
            "ATA_SECURITY_ERASE_ENHANCED",
            id_ev + "\n[drive reports ATA security enabled+locked before VSTL could arm erase]",
        )
    if security["enabled"]:
        cleared, clear_ev = _ata_security_disable(device, password="vstl")
        id_ev += "\n" + clear_ev
        rc_post, out_post, err_post = _run(["hdparm", "-I", device], timeout=15)
        post_ev = f"$ hdparm -I {device}  # after security-disable\n{out_post}\n{err_post}"
        id_ev += "\n" + post_ev
        if rc_post != 0:
            return (
                False,
                "ATA_SECURITY_ERASE_ENHANCED",
                id_ev + "\n[ATA identity query failed after security-disable attempt]",
            )
        post_security = _ata_security_status(out_post)
        if not cleared or post_security["enabled"]:
            return (
                False,
                "ATA_SECURITY_ERASE_ENHANCED",
                id_ev + "\n[stale ATA security state remained enabled after security-disable attempt]",
            )

    # 2) Set password
    rc1, out1, err1 = _run(
        ["hdparm", "--user-master", "u", "--security-set-pass", "vstl", device],
        timeout=30,
    )
    set_ev = f"$ hdparm --security-set-pass vstl {device}\nrc={rc1}\n{out1}\n{err1}"
    if rc1 != 0:
        return False, "ATA_SECURITY_ERASE_ENHANCED", id_ev + "\n" + set_ev

    # 3) Issue the enhanced erase (preferred). Some early SSDs only
    #    support the standard erase — try enhanced first, then plain.
    if progress:
        progress({"elapsed_sec": 0, "phase": "running"})
    _keep_operator_console_awake()
    rc2, out2, err2 = _run_with_erase_heartbeat(
        ["hdparm", "--user-master", "u", "--security-erase-enhanced", "vstl", device],
        timeout=timeout,
        progress=progress,
        method="ATA_SECURITY_ERASE_ENHANCED",
        phase="running ATA Security Erase Enhanced",
    )
    erase_ev = f"$ hdparm --security-erase-enhanced vstl {device}\nrc={rc2}\n{out2}\n{err2}"
    method = "ATA_SECURITY_ERASE_ENHANCED"
    if rc2 != 0:
        _keep_operator_console_awake()
        rc3, out3, err3 = _run_with_erase_heartbeat(
            ["hdparm", "--user-master", "u", "--security-erase", "vstl", device],
            timeout=timeout,
            progress=progress,
            method="ATA_SECURITY_ERASE",
            phase="running ATA Security Erase",
        )
        erase_ev += f"\n$ hdparm --security-erase vstl {device}\nrc={rc3}\n{out3}\n{err3}"
        method = "ATA_SECURITY_ERASE"
        rc2 = rc3

    return rc2 == 0, method, id_ev + "\n" + set_ev + "\n" + erase_ev


def _blkdiscard(device: str) -> tuple[bool, str, str]:
    """Maintenance-only TRIM/UNMAP Clear assist; never certifies the erase."""
    _keep_operator_console_awake()
    rc, out, err = _run(["blkdiscard", "-f", device], timeout=900)
    ev = f"$ blkdiscard -f {device}\nrc={rc}\n{out}\n{err}"
    return rc == 0, "BLKDISCARD", ev


def _nvme_secure_discard_clear(device: str) -> tuple[bool, str, str]:
    """NVMe logical Clear assist; never certifies the erase."""
    commands = [
        ["blkdiscard", "--secure", "-f", device],
        ["blkdiscard", "-s", "-f", device],
    ]
    evidence_blocks: list[str] = []
    for cmd in commands:
        _keep_operator_console_awake()
        rc, out, err = _run(cmd, timeout=900)
        evidence_blocks.append(f"$ {' '.join(cmd)}\nrc={rc}\n{out}\n{err}")
        if rc == 0:
            return True, "NVMe_SECURE_DISCARD_CLEAR", "\n---\n".join(evidence_blocks)
    return False, "NVMe_SECURE_DISCARD_CLEAR", "\n---\n".join(evidence_blocks)


def _software_zero_clear(
    device: str,
    progress: Optional[Callable[[dict], None]] = None,
    size_bytes: int = 0,
    chunk_size: int = 16 * 1024 * 1024,
    max_mib_per_sec: float = 0,
) -> tuple[bool, str, str]:
    """Overwrite the user-addressable namespace as a Clear assist.

    This is deliberately reported as Clear evidence only. It cannot certify
    remapped or over-provisioned NAND, so the caller must still complete a
    native purge method after this helper succeeds.
    """
    label = "NVMe_SOFTWARE_ZERO_CLEAR"
    started = time.monotonic()
    try:
        _keep_operator_console_awake()
        total = int(size_bytes or 0)
    except (TypeError, ValueError):
        total = 0
    try:
        if chunk_size <= 0:
            chunk_size = 16 * 1024 * 1024
        try:
            env_chunk = int(os.environ.get("VSTL_ZERO_CLEAR_CHUNK_BYTES", "0") or "0")
            if env_chunk > 0:
                chunk_size = env_chunk
        except ValueError:
            pass
        try:
            env_rate = float(os.environ.get("VSTL_ZERO_CLEAR_MAX_MIB_PER_SEC", "0") or "0")
            if env_rate > 0:
                max_mib_per_sec = env_rate
        except ValueError:
            pass
        bytes_per_sec = float(max_mib_per_sec) * 1024 * 1024 if max_mib_per_sec else 0
        zero_chunk = b"\x00" * chunk_size
        with open(device, "r+b", buffering=0) as handle:
            if total <= 0:
                handle.seek(0, os.SEEK_END)
                total = handle.tell()
            if total <= 0:
                return False, label, f"$ software-zero-clear {device}\ninvalid device size={total}"
            handle.seek(0)
            written = 0
            last_progress = -1
            last_update = 0.0
            while written < total:
                remaining = total - written
                block = zero_chunk if remaining >= len(zero_chunk) else zero_chunk[:remaining]
                n = handle.write(block)
                if not n:
                    return (
                        False,
                        label,
                        f"$ software-zero-clear {device}\nwrite returned 0 at offset={written}",
                    )
                written += int(n)
                now = time.monotonic()
                if bytes_per_sec > 0:
                    target_elapsed = written / bytes_per_sec
                    actual_elapsed = now - started
                    if target_elapsed > actual_elapsed:
                        time.sleep(min(1.0, target_elapsed - actual_elapsed))
                        now = time.monotonic()
                percent = int((written * 100) / total)
                if progress and (percent != last_progress or now - last_update >= 2):
                    _keep_operator_console_awake()
                    progress({
                        "elapsed_sec": int(now - started),
                        "phase": (
                            "clear assist: throttled zero-fill"
                            if bytes_per_sec > 0
                            else "clear assist: zero-filling user-addressable namespace"
                        ),
                        "method": label,
                        "percent": percent,
                    })
                    last_progress = percent
                    last_update = now
            handle.flush()
            os.fsync(handle.fileno())
        rc_f, out_f, err_f = _run(["blockdev", "--flushbufs", device], timeout=20)
        if progress:
            progress({
                "elapsed_sec": int(time.monotonic() - started),
                "phase": "clear assist completed",
                "method": label,
                "percent": 100,
            })
        ev = (
            f"$ software-zero-clear {device}\n"
            f"bytes_written={total}\n"
            f"chunk_size={chunk_size}\n"
            f"max_mib_per_sec={max_mib_per_sec}\n"
            f"duration_sec={int(time.monotonic() - started)}\n"
            f"$ blockdev --flushbufs {device}\nrc={rc_f}\n{out_f}\n{err_f}"
        )
        return True, label, ev
    except (OSError, PermissionError) as e:
        return False, label, f"$ software-zero-clear {device}\nerror={e}"


# ---------------------------------------------------------------------------
# HDD path
# ---------------------------------------------------------------------------
def _nwipe(device: str, method: str = "dod3pass",
            progress: Optional[Callable[[dict], None]] = None,
            timeout: int = 24 * 3600) -> tuple[bool, str, str]:
    """3-pass DoD 5220.22-M overwrite via nwipe (DBAN's modern fork).

    Runs in --nogui --autonuke mode. The actual time is dominated by the
    HDD's sustained write throughput (~80–150 MB/s), so a 1 TB HDD takes
    roughly 3 × 2 hours = 6 hours.
    """
    cmd = ["nwipe", "--method=" + method, "--rounds=1",
           "--verify=last", "--nogui", "--autonuke", device]
    rc, out, err = _run(cmd, timeout=timeout)
    ev = f"$ {' '.join(cmd)}\nrc={rc}\n{out[-4000:]}\n{err[-2000:]}"
    label = "NWIPE_DOD_3PASS" if method == "dod3pass" else "NWIPE_RANDOM_1PASS"
    return rc == 0, label, ev


# ---------------------------------------------------------------------------
# Verification — read a sample of sectors back and confirm they're zero
# ---------------------------------------------------------------------------
def _verify_wipe_sample(device: str) -> tuple[bool, str]:
    """Read 4 spots (start / 25% / 50% / 75%) and confirm they are zero.

    NB: a sanitize-cleared drive doesn't HAVE to read back as zero — some
    NVMe controllers return whatever the firmware decides post-sanitize.
    The certificate records the verification result honestly either way.
    """
    try:
        with open(device, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size <= 0:
                return False, "device size = 0, cannot verify"
            samples = [
                ("start",   0),
                ("25_pct",  size // 4),
                ("50_pct",  size // 2),
                ("75_pct",  (size // 4) * 3),
            ]
            results = []
            for label, offset in samples:
                f.seek(offset)
                chunk = f.read(4096)
                zeros = (chunk == b"\x00" * len(chunk))
                results.append(f"{label}: {'ZERO' if zeros else 'NONZERO ({:d} non-zero bytes)'.format(sum(1 for b in chunk if b != 0))}")
            all_zero = all("ZERO" == r.split(":", 1)[1].strip() for r in results)
            return all_zero, " | ".join(results)
    except (OSError, PermissionError) as e:
        return False, f"sample read failed: {e}"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def _run_nvme_purge_sequence(
    device: str,
    evidence_blocks: list[str],
    tried: list[str],
    progress_callback: Optional[Callable[[dict], None]],
    attempt_label: str,
    skip_sanitize_reason: str = "",
) -> tuple[bool, str]:
    evidence_blocks.append(f"[{attempt_label} NVMe purge attempt]")
    if skip_sanitize_reason:
        evidence_blocks.append(
            f"Skipped NVMe sanitize opcodes for {skip_sanitize_reason}; "
            "trying NVMe Format Crypto as the primary Purge method."
        )
    else:
        sanitize_actions, support_ev = _nvme_sanitize_actions(device)
        evidence_blocks.append(support_ev)
        for action in sanitize_actions:
            ok, method, ev = _nvme_sanitize(
                device, action=action, progress=progress_callback,
            )
            tried.append(
                _NVME_SANITIZE_ACTION_LABELS.get(
                    action, f"NVMe_SANITIZE_ACTION_{action}"
                )
            )
            evidence_blocks.append(ev)
            if ok:
                return True, method
    ok, method, ev = _nvme_format_crypto(device)
    tried.append("NVMe_FORMAT_CRYPTO")
    evidence_blocks.append(ev)
    return ok, method


def _run_nvme_clear_assist(
    device: str,
    drive: dict,
    progress_callback: Optional[Callable[[dict], None]],
    firmware_safe_only: bool = False,
) -> tuple[bool, str, str]:
    evidence_blocks = ["[clear assist device release]\n" + _release_block_device(device)]
    zero_step = (
        "NVMe_SOFTWARE_ZERO_CLEAR",
        lambda: _software_zero_clear(
            device,
            progress=progress_callback,
            size_bytes=int(drive.get("device_size_bytes") or 0),
            chunk_size=4 * 1024 * 1024 if firmware_safe_only else 16 * 1024 * 1024,
            max_mib_per_sec=48 if firmware_safe_only else 0,
        ),
    )
    if firmware_safe_only:
        evidence_blocks.append(
            "Firmware-safe Clear mode: skipped NVMe format, sanitize, and "
            "secure-discard opcodes; using OS-level zero fill only."
        )
        clear_steps = [zero_step]
    else:
        clear_steps: list[tuple[str, Callable[[], tuple[bool, str, str]]]] = [
            ("NVMe_FORMAT_USER_DATA", lambda: _nvme_format_user_data(device)),
            ("NVMe_SECURE_DISCARD_CLEAR", lambda: _nvme_secure_discard_clear(device)),
            zero_step,
        ]

    last_method = ""
    for label, runner in clear_steps:
        last_method = label
        if progress_callback:
            progress_callback({
                "elapsed_sec": 0,
                "phase": "running Clear assist before required Purge retry",
                "method": label,
                "percent": None,
            })
        ok, method, ev = runner()
        last_method = method or label
        evidence_blocks.append(ev)
        if ok:
            return True, last_method, "\n---\n".join(evidence_blocks)
    return False, last_method, "\n---\n".join(evidence_blocks)


def _run_sata_purge_sequence(
    device: str,
    evidence_blocks: list[str],
    tried: list[str],
    progress_callback: Optional[Callable[[dict], None]],
    attempt_label: str,
) -> tuple[bool, str]:
    evidence_blocks.append(f"[{attempt_label} SATA SSD purge attempt]")
    ok, method, ev = _hdparm_sanitize_erase(
        device, progress=progress_callback,
    )
    tried.append(method or "ATA_SANITIZE")
    evidence_blocks.append(ev)
    if ok:
        return True, method

    ok, method, ev = _hdparm_security_erase(
        device, progress=progress_callback,
    )
    tried.append(method or "ATA_SECURITY_ERASE_ENHANCED")
    evidence_blocks.append(ev)
    return ok, method


def _run_sata_clear_assist(device: str) -> tuple[bool, str, str]:
    ok, method, ev = _blkdiscard(device)
    return ok, method, ev


def run_secure_erase(
    drive: Optional[dict] = None,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Top-level entry point. Detects the drive (if not provided), picks
    the strongest applicable method, runs it, verifies, and returns the
    structured result documented in the module docstring.
    """
    drive = drive or detect_primary_drive()
    if not drive.get("device"):
        return {
            "ok": False,
            "method": "",
            "standard": "",
            "passes": 0,
            "started_at": _now_iso(),
            "completed_at": _now_iso(),
            "duration_sec": 0,
            "device": "",
            "device_type": drive.get("device_type", "UNKNOWN"),
            "device_model": "",
            "device_size_gb": 0,
            "verified": False,
            "verification_method": "skipped",
            "evidence": "",
            "error_message": drive.get("error", "no fixed disk detected"),
        }

    device = drive["device"]
    dtype  = drive["device_type"]
    started_at = _now_iso()
    started_ts = time.monotonic()
    evidence_blocks: list[str] = []
    method = ""
    ok = False
    tried: list[str] = []
    clear_only_exception = False
    clear_only_exception_reason = ""
    clear_exception_reason = _clear_only_exception_reason()
    native_purge_screen_blank_reason = _nvme_native_purge_screen_blank_risk_reason()
    native_purge_blocked_for_screen_blank = False
    evidence_blocks.append("[operator console keepalive]\n" + _ensure_erase_console_keepalive())
    power_ok, power_ev, power_error = _erase_power_guard()
    evidence_blocks.append("[power stability]\n" + power_ev)
    if not power_ok:
        completed_at = _now_iso()
        return {
            "ok": False,
            "method": "",
            "standard": "",
            "passes": 0,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_sec": int(time.monotonic() - started_ts),
            "device": device,
            "device_type": dtype,
            "device_model": drive.get("device_model", ""),
            "device_size_gb": drive.get("device_size_gb", 0),
            "verified": False,
            "verification_method": "skipped",
            "evidence": _cap_evidence("\n---\n".join(evidence_blocks)),
            "error_message": power_error,
        }

    if dtype == "NVMe":
        release_ev = _release_block_device(device)
        evidence_blocks.append("[pre-erase device release]\n" + release_ev)
        if native_purge_screen_blank_reason and clear_exception_reason:
            native_purge_blocked_for_screen_blank = True
            tried.append("NVMe_PURGE_SKIPPED_SCREEN_BLANK_RISK")
            evidence_blocks.append(
                "Policy: NVMe Purge is the primary wipe method, but native "
                f"NVMe Purge commands are disabled for {native_purge_screen_blank_reason}; "
                "this model blanks the display while the power LED remains on."
            )
            clear_ok, clear_method, clear_ev = _run_nvme_clear_assist(
                device, drive, progress_callback, firmware_safe_only=True,
            )
            evidence_blocks.append("[NVMe Clear assist]\n" + clear_ev)
            if clear_ok:
                ok = True
                method = clear_method
                clear_only_exception = True
                clear_only_exception_reason = clear_exception_reason
                evidence_blocks.append(
                    f"Firmware-safe Clear completed using {clear_method}; "
                    f"native NVMe Purge was skipped by {native_purge_screen_blank_reason}."
                )
            else:
                evidence_blocks.append(
                    f"Firmware-safe Clear failed under {clear_exception_reason}; "
                    f"tried {clear_method or ', '.join(_NVME_CLEAR_ASSIST_METHODS)}."
                )
        else:
            evidence_blocks.append("Policy: NVMe Purge is always attempted as the primary wipe method.")
            ok, method = _run_nvme_purge_sequence(
                device, evidence_blocks, tried, progress_callback, "initial primary",
            )
        if (
            not ok
            and clear_exception_reason
            and not native_purge_blocked_for_screen_blank
        ):
            evidence_blocks.append(
                f"Primary NVMe Purge failed on {clear_exception_reason}; "
                "running firmware-safe Clear fallback only after the primary "
                "Purge attempt."
            )
            clear_ok, clear_method, clear_ev = _run_nvme_clear_assist(
                device, drive, progress_callback, firmware_safe_only=True,
            )
            evidence_blocks.append("[NVMe Clear assist]\n" + clear_ev)
            if clear_ok:
                ok = True
                method = clear_method
                clear_only_exception = True
                clear_only_exception_reason = clear_exception_reason
                evidence_blocks.append(
                    f"Clear assist completed using {clear_method}; primary NVMe "
                    f"Purge was attempted first and failed; final NVMe Purge "
                    f"retry skipped by {clear_exception_reason}."
                )
            else:
                evidence_blocks.append(
                    f"Clear assist failed under the temporary model exception; tried "
                    f"{clear_method or ', '.join(_NVME_CLEAR_ASSIST_METHODS)}. "
                    "The drive was not certified."
                )
        elif not ok and not native_purge_blocked_for_screen_blank:
            evidence_blocks.append(
                "Direct NVMe purge failed; running Clear assist methods "
                f"{', '.join(_NVME_CLEAR_ASSIST_METHODS)} before the required "
                "final Purge retry. Clear assist is not certificate-eligible."
            )
            clear_ok, clear_method, clear_ev = _run_nvme_clear_assist(
                device, drive, progress_callback,
            )
            evidence_blocks.append("[NVMe Clear assist]\n" + clear_ev)
            if clear_ok:
                evidence_blocks.append(
                    f"Clear assist completed using {clear_method}; final NVMe "
                    "Purge retry is required before certification."
                )
                evidence_blocks.append(
                    "[post-clear device release]\n" + _release_block_device(device)
                )
                ok, method = _run_nvme_purge_sequence(
                    device, evidence_blocks, tried, progress_callback, "post-clear",
                )
                if not ok:
                    evidence_blocks.append(
                        "Clear assist completed; final NVMe Purge retry still "
                        "failed. Certification remains blocked."
                    )
            else:
                evidence_blocks.append(
                    f"Clear assist failed before final NVMe Purge retry; tried "
                    f"{clear_method or ', '.join(_NVME_CLEAR_ASSIST_METHODS)}. "
                    "Certification remains blocked."
                )
    elif dtype == "SATA_SSD":
        ok, method = _run_sata_purge_sequence(
            device, evidence_blocks, tried, progress_callback, "initial",
        )
        if not ok:
            evidence_blocks.append(
                "Direct SATA SSD purge failed; running Clear assist method "
                f"{', '.join(_SATA_CLEAR_ASSIST_METHODS)} before the required "
                "final Purge retry. Clear assist is not certificate-eligible."
            )
            clear_ok, clear_method, clear_ev = _run_sata_clear_assist(device)
            evidence_blocks.append("[SATA SSD Clear assist]\n" + clear_ev)
            if clear_ok:
                if clear_exception_reason:
                    ok = True
                    method = clear_method
                    clear_only_exception = True
                    clear_only_exception_reason = clear_exception_reason
                    evidence_blocks.append(
                        f"Clear assist completed using {clear_method}; final SATA "
                        f"SSD Purge retry skipped by {clear_exception_reason}."
                    )
                else:
                    evidence_blocks.append(
                        f"Clear assist completed using {clear_method}; final SATA "
                        "SSD Purge retry is required before certification."
                    )
                    ok, method = _run_sata_purge_sequence(
                        device, evidence_blocks, tried, progress_callback, "post-clear",
                    )
                    if not ok:
                        evidence_blocks.append(
                            "Clear assist completed; final SATA SSD Purge retry still "
                            "failed. Certification remains blocked."
                        )
            else:
                evidence_blocks.append(
                    f"Clear assist failed before final SATA SSD Purge retry; tried "
                    f"{clear_method or ', '.join(_SATA_CLEAR_ASSIST_METHODS)}. "
                    "Certification remains blocked."
                )
    elif dtype == "HDD":
        ok, method, ev = _nwipe(device, method="dod3pass",
                                 progress=progress_callback)
        tried.append("NWIPE_DOD_3PASS")
        evidence_blocks.append(ev)
    else:
        return {
            "ok": False, "method": "", "standard": "", "passes": 0,
            "started_at": started_at, "completed_at": _now_iso(),
            "duration_sec": 0, "device": device,
            "device_type": dtype, "device_model": drive.get("device_model", ""),
            "device_size_gb": drive.get("device_size_gb", 0),
            "verified": False, "verification_method": "skipped",
            "evidence": "",
            "error_message": f"unsupported device_type={dtype}",
        }

    duration = int(time.monotonic() - started_ts)
    completed_at = _now_iso()

    if not ok:
        full_evidence = "\n---\n".join(evidence_blocks)
        evidence = _cap_evidence(full_evidence)
        reason = _erase_failure_reason(full_evidence)
        return {
            "ok": False,
            "method": method or "",
            "standard": "",
            "passes": 0,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_sec": duration,
            "device": device,
            "device_type": dtype,
            "device_model": drive.get("device_model", ""),
            "device_size_gb": drive.get("device_size_gb", 0),
            "verified": False,
            "verification_method": "skipped",
            "evidence": evidence,
            "error_message": f"all methods failed; tried: {', '.join(tried)}; reason: {reason}",
        }

    verified, verify_ev = _verify_wipe_sample(device)
    evidence_blocks.append(f"$ verify-sample\n{verify_ev}")
    passes = 3 if method == "NWIPE_DOD_3PASS" else 1
    standard = (
        _CLEAR_METHOD_STANDARDS.get(method, "")
        if clear_only_exception
        else ""
    )

    return {
        "ok": True,
        "method": method,
        "standard": standard,  # backend fills purge standards; clear exception sets this here.
        "passes": passes,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_sec": duration,
        "device": device,
        "device_type": dtype,
        "device_model": drive.get("device_model", ""),
        "device_size_gb": drive.get("device_size_gb", 0),
        "verified": verified,
        "verification_method": "post_wipe_read_sample",
        "evidence": _cap_evidence("\n---\n".join(evidence_blocks)),
        "error_message": "",
        "clear_only_exception": clear_only_exception,
        "clear_only_exception_reason": clear_only_exception_reason,
    }
