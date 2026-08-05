"""
vstl_hw_detect.py — Hardware identity & health detection for the bench TUI.

These functions wrap the standard Linux toolchain shipped on Clonezilla Live
(dmidecode, lscpu, lspci, free, lsblk, smartctl, upower) and return *clean*
stringly-typed values that match the field names already known to the VSTL
backend (see routers/imaging.py::ingest_imaging_data).

Design rules
------------
* Never crash. If a tool isn't installed, or a field can't be parsed, return
  the string "UNKNOWN" — the TUI can decide how to render it.
* Never exec the shell — every command goes through subprocess.run with a
  fixed argv list to avoid quoting issues on weird BIOS strings.
* All public detect_* functions return a small dict the TUI can render
  directly. They never print to stdout or exit.

Phase 1 covers detection only (read-only screens). Phases 2-4 layer
interactive QC tests, MDM/BIOS lock checks, burn/stress, secure-erase, and
image capture/restore on top of these primitives.
"""
from __future__ import annotations

import os
import json
import re
import subprocess
from typing import Optional

UNKNOWN = "UNKNOWN"


def _run(argv: list[str], timeout: int = 5) -> str:
    """Run a command, capture stdout, return "" on any failure."""
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


def _dmidecode(field: str) -> str:
    """Read a single dmidecode -s field. Empty string on failure."""
    if os.geteuid() != 0:
        # dmidecode requires root; bench Live ISO always boots as root so
        # this is mostly defensive for unit-test runs on dev machines.
        return ""
    return _run(["dmidecode", "-s", field])


# ---------------------------------------------------------------------------
# Brand / Model / Serial / SKU
# ---------------------------------------------------------------------------
_LENOVO_MTM_RE = re.compile(r"^[0-9A-Z]{4}[0-9A-Z]{3,}$", re.I)


def _lenovo_human_model_from_sku(sku: str) -> str:
    """Extract Lenovo's human model name from encoded DMI SKU strings.

    Lenovo often reports fields like:
    LENOVO_MT_20WL_BU_Think_FM_ThinkPad X13 Gen 2i

    In those cases the DMI "SKU Number" is not the SKU we want in reports;
    the useful model name is the value after _FM_.
    """
    text = (sku or "").strip()
    if not text:
        return ""
    match = re.search(r"(?:^|_)FM_(.+)$", text, re.I)
    if not match:
        return ""
    model = match.group(1).replace("_", " ").strip()
    return re.sub(r"\s+", " ", model)


def _looks_like_lenovo_mtm(value: str) -> bool:
    """Return True for Lenovo MTM/Product strings such as 20WLS1G400."""
    text = (value or "").strip().upper()
    if not text or text == UNKNOWN:
        return False
    return bool(_LENOVO_MTM_RE.fullmatch(text) and any(ch.isdigit() for ch in text))


def _normalize_lenovo_identity(brand: str, model: str, sku: str) -> tuple[str, str]:
    """Fix Lenovo DMI's swapped product/model fields without affecting others."""
    if "LENOVO" not in (brand or "").upper():
        return model, sku
    human_model = _lenovo_human_model_from_sku(sku)
    if not human_model:
        return model, sku
    normalized_model = human_model
    normalized_sku = model if _looks_like_lenovo_mtm(model) else sku
    return normalized_model, normalized_sku


def detect_identity() -> dict:
    """Brand + Model + Serial + SKU/Part#. All strings, never None.

    Returns
    -------
    {
      "brand": "HP",
      "model": "HP ProBook 640 G8",
      "serial_no": "5CG1234XYZ",
      "sku": "6KUFUP#ABA",  # or UNKNOWN
      "mac_id": "AA:BB:CC:DD:EE:FF"
    }
    """
    brand = _dmidecode("system-manufacturer") or UNKNOWN
    model = _dmidecode("system-product-name") or UNKNOWN
    serial_no = _dmidecode("system-serial-number") or UNKNOWN
    sku = _dmidecode("system-sku-number") or UNKNOWN

    # Some BIOS vendors return "To be filled by O.E.M." or "Default string"
    # for un-set fields — treat those as UNKNOWN so the UI doesn't lie.
    GARBAGE = {
        "to be filled by o.e.m.", "default string", "system manufacturer",
        "system product name", "not specified", "not applicable", "none",
        "system serial number", "system sku number", "0",
    }
    for key in ("brand", "model", "serial_no", "sku"):
        val = locals()[key]
        if isinstance(val, str) and val.strip().lower() in GARBAGE:
            if key == "brand":
                brand = UNKNOWN
            elif key == "model":
                model = UNKNOWN
            elif key == "serial_no":
                serial_no = UNKNOWN
            elif key == "sku":
                sku = UNKNOWN

    model, sku = _normalize_lenovo_identity(brand, model, sku)

    return {
        "brand": brand,
        "model": model,
        "serial_no": serial_no,
        "sku": sku,
        "mac_id": detect_mac(),
    }


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------
def detect_cpu() -> dict:
    """CPU model string + cores/threads."""
    raw = _run(["lscpu"])
    model_name = UNKNOWN
    cores = UNKNOWN
    threads = UNKNOWN

    for line in raw.splitlines():
        if line.startswith("Model name:"):
            # Trim Intel(R)/AMD(R) noise that operators don't want to read.
            model_name = (
                line.split(":", 1)[1]
                .strip()
                .replace("(R)", "")
                .replace("(TM)", "")
                .replace("CPU @", "@")
            )
            # Collapse double spaces left over from the replace().
            model_name = re.sub(r"\s+", " ", model_name)
        elif line.startswith("Core(s) per socket:"):
            cores = line.split(":", 1)[1].strip()
        elif line.startswith("Thread(s) per core:"):
            threads = line.split(":", 1)[1].strip()

    return {"cpu": model_name, "cores": cores, "threads_per_core": threads}


# ---------------------------------------------------------------------------
# GPU (discrete first, integrated fallback)
# ---------------------------------------------------------------------------
def _format_bytes_gb(num_bytes: int) -> str:
    if num_bytes <= 0:
        return UNKNOWN
    gb = num_bytes / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.0f} GB" if abs(gb - round(gb)) < 0.15 else f"{gb:.1f} GB"
    mb = num_bytes / (1024 ** 2)
    return f"{mb:.0f} MB"


def _parse_capacity_from_text(text: str) -> str:
    patterns = [
        r"\b(\d+(?:\.\d+)?)\s*GB\b",
        r"\b(\d+(?:\.\d+)?)\s*GiB\b",
        r"\b(\d+)\s*MB\b",
        r"\b(\d+)\s*MiB\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        value = float(match.group(1))
        if "M" in pattern:
            value = value / 1024
        return f"{value:.0f} GB" if abs(value - round(value)) < 0.15 else f"{value:.1f} GB"
    return UNKNOWN


def _sysfs_vram_capacity() -> str:
    base = "/sys/class/drm"
    try:
        entries = os.listdir(base)
    except OSError:
        return UNKNOWN

    totals: list[int] = []
    for entry in entries:
        path = os.path.join(base, entry, "device", "mem_info_vram_total")
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                raw = f.read().strip()
            if raw.isdigit():
                totals.append(int(raw))
        except OSError:
            continue
    if not totals:
        return UNKNOWN
    return _format_bytes_gb(max(totals))


def _nvidia_smi_vram_capacity() -> str:
    raw = _run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"], timeout=4)
    values: list[int] = []
    for line in raw.splitlines():
        line = line.strip()
        if line.isdigit():
            values.append(int(line))
    if not values:
        return UNKNOWN
    return _format_bytes_gb(max(values) * 1024 * 1024)


def _known_gpu_vram_capacity(gpu_name: str) -> str:
    """Fallback when the live Linux driver exposes model but not VRAM.

    Keep this intentionally conservative. If a model is sold in multiple VRAM
    variants, return UNKNOWN instead of guessing.
    """
    known_specs = [
        (r"\bGeForce\s+MX550\b", "2 GB"),
        (r"\bGeForce\s+MX450\b", "2 GB"),
        (r"\bGeForce\s+MX350\b", "2 GB"),
        (r"\bGeForce\s+MX330\b", "2 GB"),
        (r"\bGeForce\s+MX250\b", "2 GB"),
        (r"\bGeForce\s+MX230\b", "2 GB"),
        (r"\bRadeon\s+550X\b", "2 GB"),
        (r"\bRadeon\s+520\b", "2 GB"),
    ]
    for pattern, capacity in known_specs:
        if re.search(pattern, gpu_name, re.I):
            return capacity
    return UNKNOWN


def _detect_discrete_gpu_memory(gpu_name: str) -> str:
    for detected in (
        _sysfs_vram_capacity(),
        _nvidia_smi_vram_capacity(),
        _parse_capacity_from_text(gpu_name),
        _known_gpu_vram_capacity(gpu_name),
    ):
        if detected and detected != UNKNOWN:
            return f"DEDICATED {detected}"
    return "DEDICATED (capacity unknown)"


def detect_gpu() -> dict:
    """Returns the most prominent VGA / 3D / Display controller from lspci.

    If an NVIDIA / AMD / discrete entry exists, prefer that. Otherwise fall
    back to integrated graphics (Intel Iris / UHD / etc.).
    """
    raw = _run(["lspci"])
    discrete: list[str] = []
    integrated: list[str] = []
    for line in raw.splitlines():
        # lspci classes that map to displays
        if not re.search(r"\b(VGA compatible controller|3D controller|Display controller)\b", line, re.I):
            continue
        text = line.split(":", 2)[-1].strip()
        if re.search(r"\b(NVIDIA|GeForce|Quadro|RTX|GTX|AMD|Radeon|RX )", text, re.I):
            discrete.append(text)
        elif re.search(r"\b(Intel|Iris|UHD|HD Graphics)\b", text, re.I):
            integrated.append(text)
        else:
            integrated.append(text)

    if discrete:
        gpu = discrete[0]
        kind = "DISCRETE"
    elif integrated:
        gpu = integrated[0]
        kind = "INTEGRATED"
    else:
        gpu = UNKNOWN
        kind = UNKNOWN

    memory = _detect_discrete_gpu_memory(gpu) if kind == "DISCRETE" else "SHARED" if kind == "INTEGRATED" else UNKNOWN

    return {
        "gpu": gpu,
        "gpu_kind": kind,
        "gpu_memory": memory,
        "integrated_gpus": integrated,
        "discrete_gpus": discrete,
        "integrated_gpu": integrated[0] if integrated else UNKNOWN,
        "discrete_gpu": discrete[0] if discrete else UNKNOWN,
        "discrete_gpu_memory": memory if discrete else UNKNOWN,
    }


# ---------------------------------------------------------------------------
# RAM
# ---------------------------------------------------------------------------
def _clean_hw_field(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return UNKNOWN
    if text.lower() in {
        "unknown", "not specified", "not available", "to be filled by o.e.m.",
        "default string", "none", "no asset tag", "not present",
    }:
        return UNKNOWN
    return text


def _memory_type_label(memory_type: str, type_detail: str = "") -> str:
    text = f"{memory_type} {type_detail}".upper()
    if "DDR5" in text or "LPDDR5" in text:
        return "PC5"
    if "DDR4" in text or "LPDDR4" in text:
        return "PC4"
    if "DDR3" in text or "LPDDR3" in text:
        return "PC3"
    if "DDR2" in text:
        return "PC2"
    return _clean_hw_field(memory_type)


def _vendor_spare_part(value: str | None) -> str:
    """Return a vendor spare/part number when it is clearly not a product model.

    SMBIOS has a reliable module Part Number, but usually does not expose the
    OEM spare part. Avoid copying product/model values into the part-number
    column; blank/UNKNOWN is more accurate than a duplicated value.
    """
    text = _clean_hw_field(value)
    if text == UNKNOWN:
        return UNKNOWN
    if re.fullmatch(r"[A-Z0-9]{5,8}-[A-Z0-9]{3,5}", text, re.I):
        return text
    return UNKNOWN


def _vendor_ct_number(value: str | None) -> str:
    """Return an OEM CT/asset identifier without confusing it with a spare.

    CT identifiers are normally compact alphanumeric strings. Only call this
    helper for an explicitly labelled CT/asset field; product models and
    serial-number fields are intentionally not guessed here.
    """
    text = _clean_hw_field(value)
    if text == UNKNOWN or _vendor_spare_part(text) != UNKNOWN:
        return UNKNOWN
    compact = re.sub(r"[\s_-]+", "", text).upper()
    if (
        10 <= len(compact) <= 28
        and re.fullmatch(r"[A-Z0-9]+", compact)
        and re.search(r"[A-Z]", compact)
        and re.search(r"\d", compact)
    ):
        return compact
    return UNKNOWN


def _labelled_value(raw: str, *labels: str) -> str:
    for label in labels:
        match = re.search(
            rf"(?im)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$",
            raw or "",
        )
        if match:
            return _clean_hw_field(match.group(1))
    return UNKNOWN


def _memory_modules(raw: str) -> list[dict]:
    modules: list[dict] = []
    for block in re.split(r"\n\s*\n", raw):
        if "Memory Device" not in block:
            continue
        values: dict[str, str] = {}
        for line in block.splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
        size = values.get("Size", "")
        if not size or size in {"No Module Installed", "Not Installed"}:
            continue
        memory_type = _clean_hw_field(values.get("Type"))
        type_detail = _clean_hw_field(values.get("Type Detail"))
        product_number = _clean_hw_field(values.get("Part Number"))
        asset_tag = values.get("Asset Tag")
        modules.append({
            "size": size,
            "vendor": _clean_hw_field(values.get("Manufacturer")),
            "model_description": type_detail if type_detail != UNKNOWN else memory_type,
            "ram_type": _memory_type_label(memory_type, type_detail),
            "serial_number": _clean_hw_field(values.get("Serial Number")),
            "product_number": product_number,
            "ct_number": _vendor_ct_number(asset_tag),
            "part_number": _vendor_spare_part(asset_tag),
            "locator": _clean_hw_field(values.get("Locator") or values.get("Bank Locator")),
            "speed": _clean_hw_field(values.get("Configured Memory Speed") or values.get("Speed")),
        })
    return modules


def detect_ram() -> dict:
    """Total RAM (GB) + per-slot module breakdown (e.g. '1×16GB + 1×4GB')."""
    # Total: dmidecode is more accurate than free for installed capacity
    # because it ignores reserved memory.
    total_gb = UNKNOWN
    modules: list[dict] = []
    if os.geteuid() == 0:
        raw = _run(["dmidecode", "-t", "memory"])
        modules = _memory_modules(raw)
        sizes_mb: list[int] = []
        cur_size_mb = None
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("Size:"):
                val = line.split(":", 1)[1].strip()
                if val in ("No Module Installed", "Not Installed", ""):
                    cur_size_mb = None
                    continue
                # "16384 MB" / "16 GB"
                m = re.match(r"(\d+)\s*(MB|GB)", val, re.I)
                if m:
                    n = int(m.group(1))
                    unit = m.group(2).upper()
                    cur_size_mb = n * 1024 if unit == "GB" else n
                    sizes_mb.append(cur_size_mb)

        if sizes_mb:
            total_mb = sum(sizes_mb)
            total_gb_n = round(total_mb / 1024)
            # Build a "1×16GB + 1×4GB" string from the per-module list
            counts: dict[int, int] = {}
            for mb in sizes_mb:
                gb = mb // 1024 if mb >= 1024 else 0
                if gb:
                    counts[gb] = counts.get(gb, 0) + 1
            modules_str = " + ".join(
                f"{cnt}×{gb}GB" for gb, cnt in sorted(counts.items(), reverse=True)
            )
            total_gb = f"({modules_str}) {total_gb_n}GB" if modules_str else f"{total_gb_n}GB"

    if total_gb == UNKNOWN:
        # Fallback: /proc/meminfo (always works, but reads usable not installed)
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        total_gb = f"{round(kb / 1024 / 1024)}GB (approx)"
                        break
        except OSError:
            pass

    return {
        "ram": total_gb,
        "module_count": len(modules),
        "modules": modules,
    }


# ---------------------------------------------------------------------------
# Storage health
# ---------------------------------------------------------------------------
def _storage_size_label(num_bytes: int) -> str:
    """Return vendor-style decimal GB/TB label for physical disk capacity."""
    try:
        gb = int(num_bytes) / 1_000_000_000
    except (TypeError, ValueError):
        return UNKNOWN
    if gb <= 0:
        return UNKNOWN
    if gb >= 1000:
        tb = gb / 1000
        return f"{tb:.1f} TB" if abs(tb - round(tb)) >= 0.05 else f"{round(tb)} TB"
    return f"{round(gb)} GB" if gb >= 10 else f"{gb:.1f} GB"


def _lsblk_drives() -> list[dict]:
    """Return non-removable physical disks with name+size+rota fields.

    2026-05-13 Phase-2 fix: previously this rejected any `lsblk` row that
    had < 5 whitespace-separated fields. On some Dell laptops the `TRAN`
    column comes back empty for NVMe drives, collapsing the line to 4
    fields and causing the entire NVMe drive to be silently skipped —
    the bench then reported "no storage detected" on a perfectly healthy
    NVMe Dell. Now we accept 4-or-5-field rows AND fall back to scanning
    `/sys/block/` when `lsblk` returns nothing at all (PXE Live ISOs
    sometimes ship a minimal busybox lsblk that omits headers).
    """
    raw_json = _run(
        ["lsblk", "-Jbo", "NAME,SIZE,ROTA,TRAN,TYPE,RM,HOTPLUG"],
    )
    drives = []
    if raw_json:
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            data = {}
        for dev in data.get("blockdevices", []):
            if dev.get("type") != "disk":
                continue
            name = str(dev.get("name") or "")
            tran = str(dev.get("tran") or "")
            if name.startswith(("loop", "ram", "sr", "fd", "zram")):
                continue
            try:
                if int(dev.get("rm", 0) or 0) == 1:
                    continue
                if int(dev.get("hotplug", 0) or 0) == 1:
                    continue
            except (TypeError, ValueError):
                pass
            if tran.lower() == "usb":
                continue
            size_bytes = int(dev.get("size", 0) or 0)
            if not tran:
                if name.startswith("nvme"):
                    tran = "nvme"
                elif name.startswith("mmcblk"):
                    tran = "mmc"
                else:
                    tran = "sata"
            drives.append({
                "name": name,
                "size": _storage_size_label(size_bytes),
                "size_bytes": size_bytes,
                "rota": str(dev.get("rota", 0)),
                "tran": tran.upper(),
            })
    if drives:
        return drives

    raw = _run(
        ["lsblk", "-d", "-b", "-o", "NAME,SIZE,ROTA,TRAN,TYPE", "--noheadings"],
    )
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if len(parts) == 4:
            # TRAN column empty — common for some NVMe controllers.
            name, size_raw, rota, typ = parts
            tran = ""
        else:
            name, size_raw, rota, tran, typ = parts[:5]
        if typ != "disk":
            continue
        if tran.lower() == "usb":
            continue
        try:
            size_bytes = int(size_raw)
        except ValueError:
            size_bytes = 0
        size = _storage_size_label(size_bytes)
        # Infer transport from device name when lsblk left TRAN blank
        if not tran:
            if name.startswith("nvme"):
                tran = "nvme"
            elif name.startswith("mmcblk"):
                tran = "mmc"
            else:
                tran = "sata"
        drives.append({
            "name": name,
            "size": size,
            "size_bytes": size_bytes,
            "rota": rota,
            "tran": tran.upper(),
        })

    # 2026-05-13 fallback: if lsblk returned nothing (minimal busybox), walk /sys/block.
    if not drives and os.path.isdir("/sys/block"):
        for entry in sorted(os.listdir("/sys/block")):
            if entry.startswith(("loop", "ram", "sr", "fd")):
                continue
            removable_path = f"/sys/block/{entry}/removable"
            try:
                with open(removable_path) as f:
                    if f.read().strip() == "1":
                        continue
            except OSError:
                pass
            try:
                if "/usb" in os.path.realpath(f"/sys/block/{entry}/device").lower():
                    continue
            except OSError:
                pass
            # Read raw size in 512-byte sectors → bytes → human readable
            size_str = UNKNOWN
            try:
                with open(f"/sys/block/{entry}/size") as f:
                    sectors = int(f.read().strip())
                bytes_total = sectors * 512
                size_str = _storage_size_label(bytes_total)
            except (OSError, ValueError):
                bytes_total = 0
                pass
            tran = "NVME" if entry.startswith("nvme") else ("MMC" if entry.startswith("mmcblk") else "SATA")
            drives.append({
                "name": entry,
                "size": size_str,
                "size_bytes": bytes_total,
                "rota": "0",
                "tran": tran,
            })

    return drives


def _smart_health(dev: str, tran: str) -> tuple[str, str]:
    """Return (health_pct_str, drive_kind_str) for /dev/<dev>.

    NVMe: percent = 100 - (percentage_used). Non-NVMe SATA: rough estimate
    via Wear_Leveling_Count / Reallocated_Sector_Ct from smartctl -a.
    """
    if not os.geteuid() == 0:
        return UNKNOWN, tran or UNKNOWN

    devpath = f"/dev/{dev}"
    if dev.startswith("nvme"):
        # 2026-05-11 fix: older smartctl versions need explicit -d nvme for
        # NVMe drives, otherwise -A returns empty JSON and the drive shows
        # as "available but not detected" on the bench. Try with -d nvme
        # first; fall back to the bare command if smartctl rejects the flag.
        out = _run(["smartctl", "-d", "nvme", "-A", "-j", devpath])
        if not out or not out.startswith("{"):
            out = _run(["smartctl", "-A", "-j", devpath])
        # Try JSON first (smartmontools 7+), fallback to grep
        if out.startswith("{"):
            try:
                import json
                j = json.loads(out)
                used = (
                    j.get("nvme_smart_health_information_log", {})
                    .get("percentage_used")
                )
                if used is not None:
                    return f"{100 - int(used)}%", "M.2 NVMe"
            except (ValueError, TypeError):
                pass
        # Plain text fallback (also retry with -d nvme if first parse failed)
        m = re.search(r"Percentage Used:\s+(\d+)\s*%", out)
        if m:
            return f"{100 - int(m.group(1))}%", "M.2 NVMe"
        out2 = _run(["smartctl", "-d", "nvme", "-A", devpath])
        m = re.search(r"Percentage Used:\s+(\d+)\s*%", out2)
        if m:
            return f"{100 - int(m.group(1))}%", "M.2 NVMe"
        return UNKNOWN, "M.2 NVMe"

    # SATA / HDD
    out = _run(["smartctl", "-A", devpath])
    # Wear_Leveling_Count VALUE is health % for many SSDs
    for ln in out.splitlines():
        if "Wear_Leveling_Count" in ln or "Media_Wearout_Indicator" in ln:
            cols = ln.split()
            if len(cols) >= 4:
                try:
                    return f"{int(cols[3])}%", "SATA SSD"
                except ValueError:
                    pass
    # Spinning HDD has no wear indicator — only "PASSED/FAILED" health
    h = _run(["smartctl", "-H", devpath])
    if "PASSED" in h:
        return "OK", "HDD" if tran == "SATA" else (tran or UNKNOWN)
    if "FAILED" in h:
        return "FAILED", "HDD" if tran == "SATA" else (tran or UNKNOWN)

    return UNKNOWN, tran or UNKNOWN


def _storage_identity(dev: str) -> dict:
    devpath = f"/dev/{dev}"
    raw = _run(["smartctl", "-i", devpath], timeout=8)
    values: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().lower()] = value.strip()

    model = _clean_hw_field(
        values.get("model number")
        or values.get("device model")
        or values.get("product")
    )
    vendor = _clean_hw_field(values.get("vendor"))
    serial = _clean_hw_field(values.get("serial number"))
    firmware = _clean_hw_field(values.get("firmware version") or values.get("revision"))
    part_number = _vendor_spare_part(
        values.get("part number")
        or values.get("spare part number")
        or values.get("fru")
    )
    ct_number = _vendor_ct_number(
        values.get("ct number")
        or values.get("ct")
        or values.get("asset tag")
        or values.get("component id")
    )

    nvme_identity = ""
    if dev.lower().startswith("nvme"):
        nvme_identity = _run(["nvme", "id-ctrl", devpath, "-H"], timeout=8)
        if part_number == UNKNOWN:
            part_number = _vendor_spare_part(
                _labelled_value(
                    nvme_identity,
                    "Part Number",
                    "Spare Part Number",
                    "FRU",
                )
            )
        if ct_number == UNKNOWN:
            ct_number = _vendor_ct_number(
                _labelled_value(
                    nvme_identity,
                    "CT Number",
                    "CT",
                    "Asset Tag",
                    "Component ID",
                )
            )

    sysfs = f"/sys/block/{dev}/device"
    if model == UNKNOWN:
        try:
            with open(os.path.join(sysfs, "model"), encoding="utf-8", errors="ignore") as f:
                model = _clean_hw_field(f.read())
        except OSError:
            pass
    if vendor == UNKNOWN:
        try:
            with open(os.path.join(sysfs, "vendor"), encoding="utf-8", errors="ignore") as f:
                vendor = _clean_hw_field(f.read())
        except OSError:
            pass
    if serial == UNKNOWN:
        for candidate in (os.path.join(sysfs, "serial"), f"/sys/block/{dev}/serial"):
            try:
                with open(candidate, encoding="utf-8", errors="ignore") as f:
                    serial = _clean_hw_field(f.read())
                if serial != UNKNOWN:
                    break
            except OSError:
                continue
    return {
        "vendor": vendor,
        "model_description": model,
        "serial_number": serial,
        "product_number": model,
        "ct_number": ct_number,
        "part_number": part_number,
        "firmware": firmware,
    }


def _storage_type_label(dev: str, tran: str, rota: str, smart_kind: str) -> str:
    name = dev.lower()
    transport = (tran or "").upper()
    if name.startswith("nvme") or transport == "NVME" or "NVME" in smart_kind.upper():
        return "NVME"
    if name.startswith("mmcblk") or transport == "MMC":
        return "eMMC"
    if str(rota) == "1":
        return "HDD"
    if transport == "SATA" or "SATA" in smart_kind.upper():
        return "2.5 SATA"
    return transport or _clean_hw_field(smart_kind)


def detect_storage() -> dict:
    """All drives' size, type, and health %."""
    drives = _lsblk_drives()
    if not drives:
        return {"drives": [], "drive_count": 0, "primary_health": UNKNOWN, "low_health": False}

    enriched = []
    low_health = False
    for d in drives:
        health, kind = _smart_health(d["name"], d["tran"])
        identity = _storage_identity(d["name"])
        # Try to parse "86%" → int for the low-health flag
        try:
            pct = int(health.rstrip("%"))
            if pct < 80:
                low_health = True
        except (ValueError, AttributeError):
            pass
        enriched.append({
            "device": f"/dev/{d['name']}",
            "size": d["size"],
            "size_bytes": d.get("size_bytes", 0),
            "type": kind,
            "storage_type": _storage_type_label(d["name"], d["tran"], d.get("rota", ""), kind),
            "health": health,
            **identity,
        })

    return {
        "drives": enriched,
        "drive_count": len(enriched),
        "primary_health": enriched[0]["health"] if enriched else UNKNOWN,
        "low_health": low_health,
    }


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------
def _int_or_none(value: str | None) -> int | None:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def _energy_to_mwh(raw_uwh: str | None) -> str:
    value = _int_or_none(raw_uwh)
    if not value:
        return UNKNOWN
    return str(round(value / 1000))


def _charge_to_mwh(raw_uah: str | None, raw_uv: str | None) -> str:
    charge = _int_or_none(raw_uah)
    voltage = _int_or_none(raw_uv)
    if not charge or not voltage:
        return UNKNOWN
    return str(round((charge * voltage) / 1_000_000_000))


def _battery_health_percent(full: str, design: str) -> tuple[str, float | None]:
    """Return BatteryInfoView-style integer health, truncating decimals."""
    try:
        design_value = float(design)
        full_value = float(full)
        if design_value > 0 and full_value >= 0:
            pct = full_value / design_value * 100.0
            return f"{int(pct)}%", pct
    except (TypeError, ValueError):
        pass
    return UNKNOWN, None


def _battery_cache_path() -> str:
    return os.environ.get(
        "VSTL_BATTERY_HEALTH_CACHE",
        "/var/lib/vstl/battery-health-cache.json",
    )


def _battery_health_cache_enabled() -> bool:
    return os.name != "nt" or bool(os.environ.get("VSTL_BATTERY_HEALTH_CACHE"))


def _load_battery_health_cache() -> dict:
    if not _battery_health_cache_enabled():
        return {}
    try:
        with open(_battery_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_battery_health_cache(cache: dict) -> None:
    if not _battery_health_cache_enabled():
        return
    path = _battery_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, sort_keys=True)
    except OSError:
        pass


def _battery_cache_key(
    entry: str,
    manufacturer: str,
    model: str,
    serial: str,
    design_mwh: str,
) -> str:
    parts = [
        _clean_hw_field(manufacturer),
        _clean_hw_field(model),
        _clean_hw_field(serial),
        _clean_hw_field(design_mwh),
        entry,
    ]
    return "|".join(part for part in parts if part and part != UNKNOWN)


def _plausible_battery_full_capacity(value: int | None, design: int | None) -> int | None:
    if not value or value <= 0:
        return None
    if not design or design <= 0:
        return value if value >= 1000 else None
    if value < max(1000, round(design * 0.35)):
        return None
    if value > round(design * 1.15):
        return None
    return value


def _estimate_full_capacity_from_remaining_charge(
    now_mwh: str,
    charge_percent: str,
    design_mwh: str,
) -> str:
    """Estimate full-charge capacity from current value and percentage.

    BatteryInfoView keeps Current Capacity Value separate from Full Charged
    Capacity. Some Linux ACPI drivers expose only a current-value-like number
    in energy_full/charge_full while the charge drops. When remaining capacity
    and current percent are present, this estimate reconstructs the stable
    Full Charged Capacity used by BatteryInfoView's health formula.
    """
    now = _int_or_none(now_mwh)
    percent = _int_or_none(charge_percent)
    design = _int_or_none(design_mwh)
    if not now or percent is None or percent <= 5 or percent > 95:
        return UNKNOWN
    estimated = round(now * 100 / percent)
    plausible = _plausible_battery_full_capacity(estimated, design)
    return str(plausible) if plausible else UNKNOWN


def _reported_full_capacity_tracks_remaining(
    full: int | None,
    now: int | None,
    percent: int | None,
) -> bool:
    """True when a driver appears to publish remaining charge as full charge."""
    if not full or not now or percent is None:
        return False
    if percent <= 5 or percent >= 95:
        return False
    return abs(full - now) <= max(2, round(max(full, now) * 0.03))


def _stable_battery_full_capacity(
    entry: str,
    design_mwh: str,
    full_mwh: str,
    now_mwh: str,
    charge_percent: str,
    manufacturer: str,
    model: str,
    serial: str,
) -> str:
    """Return BatteryInfoView-style Full Charged Capacity, not live charge.

    Health must be Full Charged Capacity / Designed Capacity. The cache avoids
    missing values, but a reliable reported full-charge value always wins.
    """
    design = _int_or_none(design_mwh)
    now = _int_or_none(now_mwh)
    percent = _int_or_none(charge_percent)
    raw_full = _plausible_battery_full_capacity(_int_or_none(full_mwh), design)
    estimated = _plausible_battery_full_capacity(
        _int_or_none(_estimate_full_capacity_from_remaining_charge(now_mwh, charge_percent, design_mwh)),
        design,
    )
    raw_tracks_remaining = _reported_full_capacity_tracks_remaining(raw_full, now, percent)

    key = _battery_cache_key(entry, manufacturer, model, serial, design_mwh)
    previous = None
    if key:
        cache = _load_battery_health_cache()
        try:
            previous = int(cache.get(key, {}).get("full_charged_capacity_mwh"))
        except (TypeError, ValueError, AttributeError):
            previous = None
        previous = _plausible_battery_full_capacity(previous, design)
    else:
        cache = {}

    if raw_full and not raw_tracks_remaining:
        chosen = raw_full
    elif estimated:
        chosen = estimated
    elif previous:
        chosen = previous
    elif raw_full:
        chosen = raw_full
    else:
        return full_mwh if full_mwh != UNKNOWN else UNKNOWN

    if key:
        cache[key] = {
            "full_charged_capacity_mwh": chosen,
            "design_capacity_mwh": design_mwh,
        }
        _save_battery_health_cache(cache)
    return str(chosen)


def detect_battery() -> dict:
    """Battery health %, capacity (mWh), cycle count, count.

    Reads /sys/class/power_supply/BAT* directly — works with no extra
    daemons (UPower is *not* always running on Clonezilla Live).
    """
    base = "/sys/class/power_supply"
    if not os.path.isdir(base):
        return {"battery_count": 0, "batteries": [], "low_health": False}

    bats: list[dict] = []
    low_health = False
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        entries = []

    for entry in entries:
        if not entry.upper().startswith("BAT"):
            continue
        bdir = os.path.join(base, entry)

        def _read(name: str, default: str = "") -> str:
            try:
                with open(os.path.join(bdir, name)) as f:
                    return f.read().strip()
            except OSError:
                return default

        # Capacity values are usually uWh. Some batteries expose only uAh,
        # which needs design/now voltage to convert to mWh.
        energy_design = _read("energy_full_design")
        energy_full = _read("energy_full")
        energy_now = _read("energy_now")
        charge_design = _read("charge_full_design")
        charge_full = _read("charge_full")
        charge_now = _read("charge_now")
        charge_percent = _read("capacity")
        voltage_design = _read("voltage_min_design") or _read("voltage_now")
        voltage_now = _read("voltage_now") or voltage_design
        cycle = _clean_hw_field(_read("cycle_count"))
        manufacturer = _clean_hw_field(_read("manufacturer"))
        serial = _clean_hw_field(_read("serial_number"))
        model = _clean_hw_field(_read("model_name") or _read("technology"))

        design_mwh = _energy_to_mwh(energy_design)
        if design_mwh == UNKNOWN:
            design_mwh = _charge_to_mwh(charge_design, voltage_design)
        current_mwh = _energy_to_mwh(energy_full)
        if current_mwh == UNKNOWN:
            current_mwh = _charge_to_mwh(charge_full, voltage_design)
        remaining_mwh = _energy_to_mwh(energy_now)
        if remaining_mwh == UNKNOWN:
            remaining_mwh = _charge_to_mwh(charge_now, voltage_design)
        current_mwh = _stable_battery_full_capacity(
            entry, design_mwh, current_mwh, remaining_mwh, charge_percent,
            manufacturer, model, serial,
        )

        # UPower exposes normalized Wh values on systems whose ACPI driver
        # does not publish the equivalent sysfs energy/charge attributes.
        if design_mwh == UNKNOWN or current_mwh == UNKNOWN:
            upower = _run(["upower", "-i", f"/org/freedesktop/UPower/devices/battery_{entry}"])
            if design_mwh == UNKNOWN:
                match = re.search(r"energy-full-design:\s*([\d.]+)\s*Wh", upower, re.I)
                if match:
                    design_mwh = str(round(float(match.group(1)) * 1000))
            if current_mwh == UNKNOWN:
                match = re.search(r"energy-full:\s*([\d.]+)\s*Wh", upower, re.I)
                if match:
                    current_mwh = str(round(float(match.group(1)) * 1000))
                    current_mwh = _stable_battery_full_capacity(
                        entry, design_mwh, current_mwh, remaining_mwh, charge_percent,
                        manufacturer, model, serial,
                    )
            if cycle == UNKNOWN:
                match = re.search(r"charge-cycles:\s*(\d+)", upower, re.I)
                if match:
                    cycle = match.group(1)
        health_full = current_mwh
        health_design = design_mwh
        if _int_or_none(charge_design) and _int_or_none(charge_full):
            health_design = charge_design
            health_full = charge_full
            full = _int_or_none(charge_full)
            now = _int_or_none(charge_now)
            percent = _int_or_none(charge_percent)
            if _reported_full_capacity_tracks_remaining(full, now, percent):
                estimated = _estimate_full_capacity_from_remaining_charge(
                    charge_now, charge_percent, charge_design,
                )
                if estimated != UNKNOWN:
                    health_full = estimated

        health_pct, pct = _battery_health_percent(health_full, health_design)
        if pct is not None and pct < 80:
            low_health = True
        explicit_ct = _clean_hw_field(_read("ct_number") or _read("asset_tag"))
        explicit_part = _clean_hw_field(
            _read("part_number") or _read("spare_part_number")
        )
        ct_number = _vendor_ct_number(explicit_ct)
        part_number = _vendor_spare_part(explicit_part)

        bats.append({
            "name": entry,
            "health": health_pct,
            "capacity_mwh": design_mwh,
            "design_capacity_mwh": design_mwh,
            "current_capacity_mwh": current_mwh,
            "full_charged_capacity_mwh": current_mwh,
            "current_capacity_value_mwh": remaining_mwh,
            "current_capacity_percent": charge_percent,
            "cycle_count": cycle,
            "vendor": manufacturer,
            "model_description": model,
            "serial_number": serial,
            "ct_number": ct_number,
            "part_number": part_number,
        })

    return {
        "battery_count": len(bats),
        "batteries": bats,
        "low_health": low_health,
    }


# ---------------------------------------------------------------------------
# BIOS and embedded Windows licensing
# ---------------------------------------------------------------------------
def detect_bios() -> dict:
    return {
        "vendor": _dmidecode("bios-vendor") or UNKNOWN,
        "version": _dmidecode("bios-version") or UNKNOWN,
        "release_date": _dmidecode("bios-release-date") or UNKNOWN,
    }


def detect_system_board() -> dict:
    """Read baseboard identity and an explicitly exposed OEM CT number."""
    raw = _run(["dmidecode", "-t", "2"]) if os.geteuid() == 0 else ""
    oem_raw = _run(["dmidecode", "-t", "11"]) if os.geteuid() == 0 else ""
    asset_tag = _labelled_value(raw, "Asset Tag")
    ct_number = _vendor_ct_number(asset_tag)
    if ct_number == UNKNOWN:
        for line in oem_raw.splitlines():
            match = re.search(
                r"(?i)(?:system\s*board|baseboard|motherboard)?\s*"
                r"(?:ct(?:\s*number)?|component\s*tracking)\s*[:=]\s*([A-Z0-9_-]+)",
                line,
            )
            if match:
                ct_number = _vendor_ct_number(match.group(1))
                if ct_number != UNKNOWN:
                    break
    return {
        "vendor": _labelled_value(raw, "Manufacturer"),
        "model_description": _labelled_value(raw, "Product Name"),
        "serial_number": _labelled_value(raw, "Serial Number"),
        "ct_number": ct_number,
    }


def detect_windows_oem_key() -> str:
    """Return the firmware-embedded Windows OEM key when an MSDM table exists."""
    table = "/sys/firmware/acpi/tables/MSDM"
    try:
        with open(table, "rb") as f:
            raw = f.read()
    except OSError:
        return UNKNOWN
    text = raw.decode("latin-1", errors="ignore")
    match = re.search(r"\b[A-Z0-9]{5}(?:-[A-Z0-9]{5}){4}\b", text, re.I)
    return match.group(0).upper() if match else UNKNOWN


# ---------------------------------------------------------------------------
# Network identity (built-in LOM MAC, then passthrough MAC) — used to link to
# asset_master. Removable USB/Type-C Ethernet adapter MACs must never become
# the unit identity because one shared adapter can be used across many laptops.
# ---------------------------------------------------------------------------
_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
_VIRTUAL_IFACE_PREFIXES = ("docker", "veth", "virbr", "br-", "tap", "tun")
_EXTERNAL_NET_DRIVER_NAMES = {
    "asix",
    "ax88179_178a",
    "cdc_ether",
    "cdc_ncm",
    "dm9601",
    "ipheth",
    "kalmia",
    "lan78xx",
    "mos7720",
    "mcs7830",
    "pegasus",
    "r8152",
    "rtl8150",
    "smsc75xx",
    "smsc95xx",
    "sr9700",
    "usbnet",
}


def _normalize_mac(value: str) -> str:
    match = _MAC_RE.search(value or "")
    if not match:
        return ""
    octets = re.split(r"[:-]", match.group(0))
    mac = ":".join(part.upper() for part in octets)
    if mac == "00:00:00:00:00:00":
        return ""
    first_octet = int(mac.split(":", 1)[0], 16)
    if first_octet & 1:
        # Multicast/broadcast addresses are not valid unit identities.
        return ""
    return mac


def _read_mac_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return _normalize_mac(f.read().strip())
    except OSError:
        return ""


def _iface_driver_name(iface: str, base: str) -> str:
    driver_path = os.path.join(base, iface, "device", "driver")
    try:
        return os.path.basename(os.path.realpath(driver_path)).lower()
    except OSError:
        return ""


def _is_external_usb_or_typec_nic(iface: str, base: str) -> bool:
    """Return True for removable USB/Type-C Ethernet interfaces.

    The important rule is conservative identity: a USB/Type-C adapter may be
    physically carrying PXE traffic, but its own MAC does not belong to the
    laptop. If the adapter presents a BIOS passthrough MAC, we only accept that
    value from DMI/BIOS evidence, not from this interface address.
    """
    name = (iface or "").lower()
    if name.startswith(("enx", "usb")):
        return True

    device_path = os.path.join(base, iface, "device")
    try:
        real_device_path = os.path.realpath(device_path).lower()
    except OSError:
        real_device_path = ""
    path_parts = real_device_path.replace("\\", "/").split("/")
    if "usb" in path_parts or "thunderbolt" in path_parts:
        return True

    driver = _iface_driver_name(iface, base)
    return driver in _EXTERNAL_NET_DRIVER_NAMES


def _is_candidate_lom_iface(iface: str, base: str) -> bool:
    if iface == "lo" or iface.startswith(_VIRTUAL_IFACE_PREFIXES):
        return False
    if os.path.isdir(os.path.join(base, iface, "wireless")):
        return False
    if _is_external_usb_or_typec_nic(iface, base):
        return False
    return True


def _dmidecode_text() -> str:
    return _run(["dmidecode", "-t", "1", "-t", "2", "-t", "11"], timeout=8)


def _extract_dmi_mac_candidates(raw: str) -> tuple[list[str], list[str]]:
    """Return (lom_macs, passthrough_macs) parsed from BIOS/OEM strings."""
    lom: list[str] = []
    passthrough: list[str] = []
    for line in (raw or "").splitlines():
        mac = _normalize_mac(line)
        if not mac:
            continue
        label = line.lower()
        if re.search(r"pass[\s_-]*through|passthrough", label):
            if mac not in passthrough:
                passthrough.append(mac)
            continue
        if re.search(r"\b(lom|lan|onboard|on-board|integrated|internal|ethernet)\b", label):
            if mac not in lom:
                lom.append(mac)
    return lom, passthrough


def detect_mac(sys_class_net: str = "/sys/class/net") -> str:
    """Return built-in LOM MAC, then BIOS passthrough MAC.

    USB/Type-C adapter MAC addresses are intentionally excluded. Returning
    UNKNOWN is safer than linking many laptops to the same removable adapter.
    """
    base = sys_class_net
    try:
        for iface in sorted(os.listdir(base)):
            if not _is_candidate_lom_iface(iface, base):
                continue
            mac_path = os.path.join(base, iface, "address")
            mac = _read_mac_file(mac_path)
            if mac:
                return mac
    except OSError:
        pass

    dmi_lom, dmi_passthrough = _extract_dmi_mac_candidates(_dmidecode_text())
    if dmi_lom:
        return dmi_lom[0]
    if dmi_passthrough:
        return dmi_passthrough[0]
    return UNKNOWN


# ---------------------------------------------------------------------------
# Convenience: build the full Phase-1 ingest payload
# ---------------------------------------------------------------------------
def collect_phase1(technician_level: str, bench_id: Optional[str] = None) -> dict:
    """Compose the JSON dict that the TUI will POST to /imaging/ingest after
    the Phase-1 detection screens are confirmed (or auto-advance through).

    technician_level: "L1" or "L2" — recorded for downstream layer-rule
                       enforcement in later phases.
    """
    ident = detect_identity()
    cpu = detect_cpu()
    gpu = detect_gpu()
    ram = detect_ram()
    storage = detect_storage()
    battery = detect_battery()
    system_board = detect_system_board()
    bios = detect_bios()
    windows_oem_key = detect_windows_oem_key()
    mac = detect_mac()

    return {
        "technician_level": technician_level,
        "bench_id": bench_id or os.uname().nodename,
        "test_type": "phase1_detect",
        "status": "completed",
        # Identity
        "brand": ident["brand"],
        "model": ident["model"],
        "serial_no": ident["serial_no"],
        "sku": ident["sku"],
        "mac_id": mac,
        # CPU / GPU / RAM
        "cpu": cpu["cpu"],
        "gpu": gpu["gpu"],
        "gpu_memory": gpu["gpu_memory"],
        "ram": ram["ram"],
        "bios_version": bios["version"],
        "system_board_ct_number": system_board["ct_number"],
        "os_license_key": windows_oem_key,
        # Storage (primary disk health for the legacy column;
        # full breakdown lives in raw_data so Phase-3 can use it)
        "ssd": storage["drives"][0]["size"] if storage["drives"] else UNKNOWN,
        "ssd_health": storage["primary_health"],
        # Battery (single primary battery for the legacy column;
        # full per-cell breakdown also in raw_data)
        "battery_status": "OK" if not battery["low_health"] else "LOW HEALTH",
        "battery_health": (
            battery["batteries"][0]["health"] if battery["batteries"] else UNKNOWN
        ),
        # Full structured snapshot for downstream phases / forensics
        "raw_data": {
            "phase": 1,
            "technician_level": technician_level,
            "identity": ident,
            "cpu": cpu,
            "gpu": gpu,
            "ram": ram,
            "storage": storage,
            "battery": battery,
            "system_board": system_board,
            "bios": bios,
            "windows_license": {
                "source": "ACPI MSDM",
                "product_key": windows_oem_key,
            },
        },
    }
