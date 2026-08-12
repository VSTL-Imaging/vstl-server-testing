"""
vstl_qc_tests.py — Phase 2B Interactive QC test helpers.

Per design choice 1b, the test set is layer-gated:
    L1 = Display, Touch auto-detect, Keyboard, Camera, Fingerprint auto-detect,
         Speaker, Microphone, Wi-Fi / Bluetooth, Ports
    L2 = Display, Touch auto-detect, Keyboard, Camera, Fingerprint, Speaker,
         Microphone, Ports, Storage

Per design choice 2a (operational meaning of L1 flexible / L2 strict):
    L1 fail → remarks dialog + continue (unit moves forward)
    L2 fail → block + Status auto-flips to L2_Rework on the matching asset

This module exposes ONLY the presence-detection probes and per-test
result-struct factories. The actual interactive screens (curses-based
prompts, key capture, color cycling, audio playback) live in
vstl-imaging-tui.py because they need the stdscr handle.

Result schema (uniform for all 8 tests)
---------------------------------------
{
  "key": "display",                 # one of TEST_ORDER
  "label": "Display",
  "applicable": True,               # False if hardware not present (auto-skip)
  "ran": True,                      # False = skipped via L1 flexible-skip
  "result": "PASS" | "FAIL" | "SKIP" | "NA",
  "remarks": "operator-typed text or detector-derived note",
  "evidence": "lsusb / udev / amixer raw line, truncated"
}
"""
from __future__ import annotations

import os
import glob
import json
import re
import shutil
import subprocess
import time

# Public constants -----------------------------------------------------------
TEST_ORDER = (
    "display", "touchscreen", "keyboard", "camera", "fingerprint",
    "speaker", "microphone", "audio_jack", "wireless", "ports", "storage",
)

TEST_LABELS = {
    "display":     "Display",
    "driver_preflight": "Driver / Linux Support Preflight",
    "keyboard":    "Keyboard",
    "camera":      "Camera",
    "fingerprint": "Fingerprint",
    "speaker":     "Speaker",
    "microphone":  "Microphone",
    "audio_jack":  "Audio Jack",
    "wireless":    "Wi-Fi / Bluetooth",
    "touchscreen": "Touchscreen",
    "ports":       "Ports",
    "power_adapter": "Power Adapter",
    "storage":     "Storage",
}

# 2026-05-11 Phase 2 v2 — Cloudflare 1010 fix: Python's urllib defaults to
# User-Agent "Python-urllib/3.x" which Cloudflare's bot-fight mode blocks at
# the edge for emergent.host. Every bench HTTP call must set a real UA.
BENCH_USER_AGENT = "VSTL-Bench/2.0 (Linux; PXE; +https://vstl360.local)"


_AUDIO_DRIVER_PRIMED = False
_AUDIO_DRIVER_EVIDENCE = ""


# L1 = quick QC (touch display auto-detect, wireless, and ports included).
# L2 = full QC (all tests, with fingerprint + storage smoke test added).
L1_TESTS = (
    "display", "touchscreen", "keyboard", "camera", "fingerprint",
    "speaker", "microphone", "audio_jack", "wireless", "ports",
)
L2_TESTS = TEST_ORDER  # all tests, including ports + storage
HARDWARE_OPTIONAL_TESTS = ("fingerprint",)


def tests_for(layer: str) -> tuple[str, ...]:
    """Return the ordered tuple of tests for the given technician layer."""
    return L1_TESTS if str(layer).upper() == "L1" else L2_TESTS


def _run(argv: list[str], timeout: int = 5) -> str:
    """Identical to vstl_hw_detect/_run — kept private to this module."""
    try:
        out = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        return (out.stdout or "").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def _audio_dmi_identity() -> str:
    parts = []
    for path in (
        "/sys/class/dmi/id/sys_vendor",
        "/sys/class/dmi/id/product_name",
        "/sys/class/dmi/id/product_version",
        "/sys/class/dmi/id/product_sku",
    ):
        value = _read_sysfs_text(path)
        if value and value.strip().lower() not in {
            "none", "not specified", "to be filled by o.e.m.", "default string",
        }:
            parts.append(value.strip())
    return " ".join(dict.fromkeys(parts))


def _is_hp_elitebook_640_g10(profile: str | None = None) -> bool:
    text = profile if profile is not None else _audio_dmi_identity()
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower())
    compact = normalized.replace(" ", "")
    return (
        ("hp" in normalized.split() or "hewlettpackard" in compact)
        and "elitebook" in normalized
        and re.search(r"\b640\b", normalized) is not None
        and re.search(r"\bg10\b", normalized) is not None
    )


def _audio_pci_lines() -> list[str]:
    raw = _run(["lspci", "-nn"], timeout=5)
    return [
        line.strip() for line in raw.splitlines()
        if re.search(r"\b(audio|multimedia|cavs|sound)\b", line, re.I)
    ][:6]


def _audio_profile_from_hardware() -> str:
    """Choose the Linux audio-driver profile from DMI + PCI IDs.

    OEM Windows packages cannot be installed inside this Linux live image, so
    the practical "driver" choice is whether Intel audio should run through
    legacy HDA or SOF/DMIC. Old Latitude 54xx/55xx systems usually need HDA;
    newer Latitude/EliteBook/ThinkPad systems with Intel DSP/DMIC need SOF.
    """
    identity = _audio_dmi_identity().lower()
    pci = " ".join(_audio_pci_lines()).lower()

    old_latitude = re.search(r"\blatitude\s+5(?:4|5)(?:70|80|90|00)\b", identity)
    old_intel_audio_ids = (
        "8086:9d70", "8086:9d71", "8086:9d74", "8086:9d75",
        "8086:a170", "8086:a171", "8086:a2f0", "8086:a2f1",
    )
    sof_intel_audio_ids = (
        "8086:02c8", "8086:06c8", "8086:43c8", "8086:4b55",
        "8086:51c8", "8086:51c9", "8086:7ad0", "8086:7e28",
        "8086:a0c8", "8086:a171",
    )
    if old_latitude or any(device_id in pci for device_id in old_intel_audio_ids):
        return "legacy_hda"
    if any(device_id in pci for device_id in sof_intel_audio_ids):
        return "sof_dmic"
    if "sof" in pci or "cavs" in pci:
        return "sof_dmic"
    return "generic"


def _remove_loaded_audio_modules() -> None:
    if getattr(os, "geteuid", lambda: 0)() != 0:
        return
    if (os.environ.get("VSTL_AUDIO_RELOAD", "1") or "1").strip().lower() in {"0", "false", "no"}:
        return
    for module in (
        "snd_sof_pci_intel_tgl", "snd_sof_pci_intel_cnl", "snd_sof_pci_intel_icl",
        "snd_sof_pci_intel_apl", "snd_sof_pci", "snd_sof_intel_hda_common",
        "snd_soc_avs", "snd_hda_codec_hdmi", "snd_hda_codec_realtek",
        "snd_hda_codec_generic", "snd_hda_intel", "snd_intel_dspcfg",
    ):
        _run(["modprobe", "-r", module], timeout=4)


def _write_audio_modprobe_option(dsp_driver: str) -> None:
    """Persist the Intel DSP driver choice for any later module reloads."""
    if getattr(os, "geteuid", lambda: 0)() != 0:
        return
    try:
        os.makedirs("/etc/modprobe.d", exist_ok=True)
        with open("/etc/modprobe.d/vstl-audio-dsp.conf", "w", encoding="utf-8") as f:
            f.write(f"options snd_intel_dspcfg dsp_driver={dsp_driver}\n")
    except OSError:
        pass


def prime_audio_driver_for_model(force: bool = False) -> str:
    """Load the most suitable Linux audio driver profile before QC starts."""
    global _AUDIO_DRIVER_PRIMED, _AUDIO_DRIVER_EVIDENCE
    if _AUDIO_DRIVER_PRIMED and not force:
        return _AUDIO_DRIVER_EVIDENCE

    profile = os.environ.get("VSTL_AUDIO_PROFILE", "").strip().lower() or _audio_profile_from_hardware()
    if profile not in {"legacy_hda", "sof_dmic", "generic"}:
        profile = "generic"

    if profile == "legacy_hda":
        _write_audio_modprobe_option("1")
        _remove_loaded_audio_modules()
        _run(["modprobe", "snd_intel_dspcfg", "dsp_driver=1"], timeout=4)
        _run(["modprobe", "snd_hda_intel"], timeout=4)
        _run(["modprobe", "snd_hda_codec_realtek"], timeout=4)
    elif profile == "sof_dmic":
        _write_audio_modprobe_option("3")
        _remove_loaded_audio_modules()
        _run(["modprobe", "snd_intel_dspcfg", "dsp_driver=3"], timeout=4)
        for module in (
            "snd_sof_pci_intel_tgl", "snd_sof_pci_intel_cnl",
            "snd_sof_pci_intel_icl", "snd_sof_pci",
            "snd_sof_intel_hda_common", "snd_hda_intel",
        ):
            _run(["modprobe", module], timeout=4)
    else:
        for module in (
            "snd_hda_intel", "snd_hda_codec_realtek", "snd_sof_pci",
            "snd_soc_avs", "snd_usb_audio",
        ):
            _run(["modprobe", module], timeout=4)

    _run(["udevadm", "trigger", "--subsystem-match=sound", "--action=add"], timeout=4)
    _run(["udevadm", "settle", "--timeout=4"], timeout=5)
    _run(["alsactl", "init"], timeout=6)
    _AUDIO_DRIVER_EVIDENCE = f"audio_profile={profile}; model={_audio_dmi_identity() or 'UNKNOWN'}"
    _AUDIO_DRIVER_PRIMED = True
    return _AUDIO_DRIVER_EVIDENCE


def _read_sysfs_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return ""


def _parse_resolution_text(text: str) -> tuple[int, int] | None:
    """Parse common sysfs/fbset mode strings into (width, height)."""
    match = re.search(r"(?<!\d)(\d{3,5})\s*[x,]\s*(\d{3,5})(?!\d)", text or "")
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


def _format_display_resolution(width: int, height: int) -> str:
    """Return technician-friendly resolution text, e.g. 1920 x 1080 (FHD)."""
    if width <= 0 or height <= 0:
        return "UNKNOWN"
    names = {
        (3840, 2160): "UHD 4K",
        (3200, 1800): "QHD+",
        (2880, 1800): "2.8K",
        (2560, 1600): "WQXGA",
        (2560, 1440): "QHD",
        (2496, 1664): "2.5K",
        (2256, 1504): "2.2K",
        (2240, 1400): "2.2K",
        (2160, 1440): "2K",
        (1920, 1200): "WUXGA",
        (1920, 1080): "FHD",
        (1600, 900): "HD+",
        (1536, 864): "HD+",
        (1440, 900): "WXGA+",
        (1366, 768): "HD",
        (1360, 768): "HD",
        (1280, 800): "WXGA",
        (1280, 720): "HD",
    }
    name = names.get((width, height)) or names.get((height, width))
    suffix = f" ({name})" if name else ""
    return f"{width} x {height}{suffix}"


def _read_first_resolution(paths: list[str]) -> tuple[int, int] | None:
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                parsed = _parse_resolution_text(f.read())
            if parsed:
                return parsed
        except OSError:
            continue
    return None


def _drm_connected_resolution() -> tuple[int, int] | None:
    base = "/sys/class/drm"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None

    candidates: list[tuple[int, int]] = []
    for entry in entries:
        status_path = os.path.join(base, entry, "status")
        modes_path = os.path.join(base, entry, "modes")
        try:
            with open(status_path, "r", encoding="utf-8", errors="ignore") as f:
                if f.read().strip().lower() != "connected":
                    continue
        except OSError:
            continue
        try:
            with open(modes_path, "r", encoding="utf-8", errors="ignore") as f:
                modes = [_parse_resolution_text(line) for line in f]
        except OSError:
            continue
        candidates.extend(mode for mode in modes if mode)

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0] * item[1])


def display_resolution_size() -> tuple[int, int] | None:
    """Best-effort current panel resolution from framebuffer/DRM sysfs."""
    fb_paths = [
        "/sys/class/graphics/fb0/virtual_size",
        "/sys/class/graphics/fb0/modes",
    ]
    return _read_first_resolution(fb_paths) or _drm_connected_resolution()


def display_resolution_label() -> str:
    size = display_resolution_size()
    if not size:
        return "UNKNOWN"
    return _format_display_resolution(size[0], size[1])


def keyboard_system_profile() -> str:
    """Return DMI text useful for deciding whether the built-in keyboard has a numpad."""
    fields = [
        "/sys/class/dmi/id/sys_vendor",
        "/sys/class/dmi/id/product_family",
        "/sys/class/dmi/id/product_name",
        "/sys/class/dmi/id/product_version",
        "/sys/class/dmi/id/board_name",
    ]
    values = [_read_sysfs_text(path) for path in fields]
    return " | ".join(dict.fromkeys(v for v in values if v))


KEYBOARD_PHYSICAL_LAYOUTS = ("US ANSI", "UK ISO")
KEYBOARD_ARRANGEMENTS = ("QWERTY", "AZERTY", "QWERTZ")
KEYBOARD_PRINT_FORMATS = (
    "US",
    "UK",
    "GERMAN",
    "BELGIAN",
    "BRITISH",
    "SPANISH",
    "POLISH",
    "SWEDISH",
    "SWISS",
    "FRENCH",
    "US WITH ARABIC PRINT",
    "OTHER",
)


def normalize_keyboard_custom_format(value: str) -> str:
    """Normalize a technician-entered keyboard format for display/reporting."""
    text = re.sub(r"\s+", " ", str(value or "").strip().upper())
    text = re.sub(r"[^A-Z0-9 +&()/_-]", "", text).strip()
    return text[:40] or "OTHER"


def build_keyboard_profile(
    *,
    has_numpad: bool,
    physical_layout: str,
    arrangement: str,
    print_format: str,
    has_pointing_stick: bool,
    has_backlight: bool,
    custom_format: str = "",
) -> dict:
    """Build the canonical keyboard profile used by the QC map and report."""
    physical = str(physical_layout or "").strip().upper()
    if physical not in KEYBOARD_PHYSICAL_LAYOUTS:
        physical = "US ANSI"

    key_arrangement = str(arrangement or "").strip().upper()
    if key_arrangement not in KEYBOARD_ARRANGEMENTS:
        key_arrangement = "QWERTY"

    requested_format = str(print_format or "").strip().upper()
    if requested_format == "OTHER":
        printed = normalize_keyboard_custom_format(custom_format)
    elif requested_format in KEYBOARD_PRINT_FORMATS:
        printed = requested_format
    else:
        printed = normalize_keyboard_custom_format(requested_format)

    arabic_print = "ARABIC" in printed
    if arabic_print:
        region = re.sub(r"\s+WITH\s+ARABIC\s+PRINT.*$", "", printed).strip() or "US"
    else:
        region = printed

    name_parts = [region, key_arrangement]
    if arabic_print:
        name_parts.append("WITH ARABIC PRINT")
    name_parts.append("WITH BACK LIGHT" if has_backlight else "WITHOUT BACK LIGHT")
    profile_name = " ".join(part for part in name_parts if part)

    return {
        "name": profile_name,
        "has_numpad": bool(has_numpad),
        "physical_layout": physical,
        "arrangement": key_arrangement,
        "print_format": printed,
        "has_arabic_print": arabic_print,
        "has_pointing_stick": bool(has_pointing_stick),
        "has_backlight": bool(has_backlight),
        "backlight": "WITH BACK LIGHT" if has_backlight else "WITHOUT BACK LIGHT",
        "numeric": "WITH NUMERIC KEYPAD" if has_numpad else "WITHOUT NUMERIC KEYPAD",
        "pointing_stick": (
            "WITH POINTING STICK" if has_pointing_stick else "WITHOUT POINTING STICK"
        ),
    }


def keyboard_profile_evidence(profile: dict) -> str:
    """Compact stable evidence string for API payloads and QC reports."""
    return (
        f"profile={profile.get('name', 'UNKNOWN')}; "
        f"physical_layout={profile.get('physical_layout', 'UNKNOWN')}; "
        f"numeric={profile.get('numeric', 'UNKNOWN')}; "
        f"pointing_stick={profile.get('pointing_stick', 'UNKNOWN')}"
    )


def _keyboard_profile_numpad_hint(profile: str) -> bool | None:
    text = re.sub(r"\s+", " ", (profile or "").lower())
    if not text:
        return None

    no_numpad = [
        r"\bthinkpad\s+(t14|t13|x1|x13|x12|l13|l14|e14|p14)\b",
        r"\blatitude\s+(5[234]\d0|54\d0|55\d0|53\d0|52\d0|74\d0|73\d0|72\d0|34\d0|33\d0)\b",
        r"\bprobook\s+(430|440|445|640|645)\b",
        r"\belitebook\s+(830|835|840|845)\b",
        r"\bxps\s+13\b",
    ]
    for pattern in no_numpad:
        if re.search(pattern, text):
            return False

    has_numpad = [
        r"\bthinkpad\s+(t15|t16|l15|e15|p15|p16|p17)\b",
        r"\bprobook\s+(450|455|650|655)\b",
        r"\belitebook\s+(850|855|860|865)\b",
        r"\bprecision\s+(75|76|77)\d{2}\b",
        r"\binspiron\s+(15|16|17)\b",
        r"\bvostro\s+(15|16|17)\b",
        r"\bpavilion\s+(15|16|17)\b",
        r"\bhp\s+(250|255|470)\b",
    ]
    for pattern in has_numpad:
        if re.search(pattern, text):
            return True
    return None


def _keyboard_name_is_external(name: str) -> bool:
    lower = (name or "").lower()
    if not lower:
        return False
    internal_terms = (
        "at translated set 2", "ps/2", "serio", "isa0060",
        "laptop", "hotkeys", "extra buttons", "power button", "video bus",
    )
    if any(term in lower for term in internal_terms):
        return False
    return any(term in lower for term in ("usb", "wireless", "receiver", "keyboard", "keypad"))


def keyboard_numpad_policy(profile: str, keyboard_names: list[str], has_keypad_caps: bool) -> bool:
    """Decide whether to show numpad keys on the keyboard QC map.

    Built-in laptop keyboards often expose generic AT scancodes even when the
    physical deck has no numpad. Only show the numpad when DMI strongly says
    the model has one, or when an external/full-size keyboard device exposes
    keypad capabilities.
    """
    hint = _keyboard_profile_numpad_hint(profile)
    if hint is not None:
        return hint
    if has_keypad_caps and any(_keyboard_name_is_external(name) for name in keyboard_names):
        return True
    return False


PORT_KIND_LABELS = {
    "usb_a": "USB Type-A",
    "usb_c": "USB Type-C",
    "audio": "3.5mm Audio Jack",
    "hdmi": "HDMI",
    "vga": "VGA",
    "displayport": "DisplayPort",
    "dvi": "DVI",
    "ethernet": "Ethernet RJ45",
    "dc_power": "DC Power Jack",
}


def _profile_text(profile: str | None = None) -> str:
    return profile if profile is not None else keyboard_system_profile()


def _port_rows_for(kind: str, count: int, evidence: str) -> list[dict]:
    label = PORT_KIND_LABELS.get(kind, kind.replace("_", " ").title())
    rows = []
    for idx in range(1, max(0, int(count)) + 1):
        suffix = f" #{idx}" if count > 1 else ""
        rows.append({
            "id": f"{kind}_{idx}",
            "kind": kind,
            "label": f"{label}{suffix}",
            "evidence": evidence,
        })
    return rows


def port_rows_from_counts(
    counts: dict[str, int],
    evidence: str = "technician-confirmed inventory",
) -> list[dict]:
    """Build the port test rows from a confirmed per-kind inventory."""
    rows: list[dict] = []
    for kind in PORT_KIND_LABELS:
        rows.extend(_port_rows_for(kind, counts.get(kind, 0), evidence))
    return rows


def _dmidecode_connector_records(raw: str) -> list[dict[str, str]]:
    """Parse SMBIOS type-8 external port connector records."""
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in (raw or "").splitlines():
        if re.match(r"^Handle\s+\S+,\s+DMI type 8,", line):
            if current:
                records.append(current)
            current = {}
            continue
        if current is None:
            continue
        match = re.match(r"^\s*([^:]+):\s*(.*?)\s*$", line)
        if match:
            current[match.group(1).strip().lower()] = match.group(2).strip()
    if current:
        records.append(current)
    return records


def _connector_kind(record: dict[str, str]) -> str:
    external_type = record.get("external connector type", "")
    reference = record.get("external reference designator", "")
    port_type = record.get("port type", "")
    text = " ".join((external_type, reference, port_type)).lower()
    if not text.strip() or external_type.lower() in {"none", "unknown"}:
        return ""
    if re.search(r"\b(type[\s-]?c|usb[\s-]?c|thunderbolt)\b", text):
        return "usb_c"
    if "usb" in text or "access bus" in text:
        return "usb_generic"
    if "hdmi" in text:
        return "hdmi"
    if re.search(r"\b(vga|db-15)\b", text):
        return "vga"
    if "displayport" in text or re.search(r"\bdp\b", text):
        return "displayport"
    if "dvi" in text:
        return "dvi"
    if re.search(r"\b(rj-?45|ethernet|network)\b", text):
        return "ethernet"
    if re.search(r"\b(headphone|headset|audio|mini jack|mic(?:rophone)?)\b", text):
        return "audio"
    if re.search(r"\b(dc[\s-]?in|power|ac adapter)\b", text):
        return "dc_power"
    return ""


def _connector_record_is_internal(record: dict[str, str]) -> bool:
    """Reject SMBIOS connector rows that describe embedded laptop devices."""
    reference = record.get("external reference designator", "").lower()
    internal_terms = (
        "internal", "webcam", "camera", "bluetooth", "fingerprint",
        "card reader", "touch", "keyboard", "trackpad", "touchpad",
        "wireless", "wlan",
    )
    return any(term in reference for term in internal_terms)


def dmidecode_port_counts(raw: str | None = None) -> dict[str, int]:
    """Return physical connector counts reported by SMBIOS, without model guesses."""
    if raw is None:
        raw = _run(["dmidecode", "-t", "8"], timeout=5)
    counts: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    generic_usb = 0
    for index, record in enumerate(_dmidecode_connector_records(raw)):
        if _connector_record_is_internal(record):
            continue
        kind = _connector_kind(record)
        if not kind:
            continue
        reference = record.get("external reference designator", "").strip().lower()
        identity = (kind, reference or f"record-{index}")
        if identity in seen:
            continue
        seen.add(identity)
        if kind == "usb_generic":
            generic_usb += 1
        else:
            counts[kind] = counts.get(kind, 0) + 1

    # SMBIOS commonly reports all USB sockets as generic USB. Linux typec
    # class data can split that total into Type-C and Type-A without inventing
    # extra connectors.
    sysfs_typec = len(typec_ports())
    if generic_usb:
        missing_typec = max(0, sysfs_typec - counts.get("usb_c", 0))
        inferred_typec = min(generic_usb, missing_typec)
        if inferred_typec:
            counts["usb_c"] = counts.get("usb_c", 0) + inferred_typec
        counts["usb_a"] = counts.get("usb_a", 0) + generic_usb - inferred_typec
    elif sysfs_typec:
        counts["usb_c"] = max(counts.get("usb_c", 0), sysfs_typec)
    return counts


def wired_interfaces() -> list[str]:
    base = "/sys/class/net"
    try:
        ifaces = sorted(os.listdir(base))
    except OSError:
        return []
    results = []
    for iface in ifaces:
        if iface == "lo" or iface.startswith(("docker", "veth", "virbr", "br-", "tap", "tun")):
            continue
        if iface.startswith(("wl", "wlan")):
            continue
        if _read_sysfs_text(os.path.join(base, iface, "type")) == "1":
            results.append(iface)
    return results


def _netdev_device_path(iface: str) -> str:
    try:
        return os.path.realpath(os.path.join("/sys/class/net", iface, "device"))
    except OSError:
        return ""


def _netdev_is_usb_attached(iface: str) -> bool:
    path = _netdev_device_path(iface).lower()
    return "/usb" in path or "\\usb" in path


def built_in_wired_interfaces() -> list[str]:
    """Return wired NICs that look motherboard/internal, not USB dongles."""
    return [iface for iface in wired_interfaces() if not _netdev_is_usb_attached(iface)]


def current_wired_link_up() -> bool:
    for iface in built_in_wired_interfaces():
        _run(["ip", "link", "set", iface, "up"], timeout=2)
        carrier = _read_sysfs_text(f"/sys/class/net/{iface}/carrier")
        operstate = _read_sysfs_text(f"/sys/class/net/{iface}/operstate").lower()
        flags = _read_sysfs_text(f"/sys/class/net/{iface}/flags").lower()
        ethtool = _run(["ethtool", iface], timeout=2).lower()
        if (
            carrier == "1"
            or operstate == "up"
            or "link detected: yes" in ethtool
            or flags.startswith("0x") and (int(flags, 16) & 0x10000)
        ):
            return True
    return False


def typec_ports() -> list[str]:
    ports: set[str] = set()
    base = "/sys/class/typec"
    try:
        ports.update(p for p in os.listdir(base) if re.fullmatch(r"port\d+", p))
    except OSError:
        pass
    # Some platforms expose USB-C role-switch ports without /sys/class/typec.
    for fallback_base in ("/sys/class/usb_role", "/sys/class/dual_role_usb"):
        try:
            entries = os.listdir(fallback_base)
        except OSError:
            continue
        for idx, entry in enumerate(sorted(entries)):
            entry_l = entry.lower()
            if any(term in entry_l for term in ("typec", "type-c", "usb-c", "ucsi", "role-switch")):
                ports.add(f"port{idx}")
    # Lenovo/HP/Dell systems sometimes expose only UCSI/platform or
    # Thunderbolt controller nodes. Treat that as conservative Type-C evidence
    # so the technician gets a Type-C row instead of a missing port test.
    fallback_markers: list[str] = []
    for pattern in (
        "/sys/bus/platform/devices/*",
        "/sys/bus/acpi/devices/*",
        "/sys/bus/thunderbolt/devices/*",
    ):
        for path in glob.glob(pattern):
            name = os.path.basename(path).lower()
            real = os.path.realpath(path).lower()
            text = f"{name} {real}"
            if any(term in text for term in ("ucsi", "usbc", "typec", "type-c", "usb-c", "thunderbolt")):
                fallback_markers.append(path)
    if fallback_markers and not ports:
        ports.add("port0")
    if not ports and _is_hp_elitebook_640_g10():
        ports.add("port0")
    return sorted(ports)


def current_typec_partners() -> set[str]:
    """Return stable Type-C port IDs with a connected partner."""
    base = "/sys/class/typec"
    partners: set[str] = set()
    try:
        entries = os.listdir(base)
    except OSError:
        return partners

    # The kernel normally exposes partners beside the port:
    # /sys/class/typec/port0-partner, not inside /sys/class/typec/port0.
    for entry in entries:
        match = re.fullmatch(r"(port\d+)-partner(?:\..*)?", entry)
        if match:
            partners.add(match.group(1))

    # Keep compatibility with drivers that expose partner links below portN.
    for port in (p for p in entries if re.fullmatch(r"port\d+", p)):
        port_path = os.path.join(base, port)
        try:
            for entry in os.listdir(port_path):
                if "partner" in entry.lower():
                    partners.add(port)
        except OSError:
            continue
    return partners


def power_supply_mains_nodes() -> list[str]:
    base = "/sys/class/power_supply"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return []
    nodes = []
    for entry in entries:
        type_text = _read_sysfs_text(os.path.join(base, entry, "type")).lower()
        name = entry.upper()
        if type_text == "mains" or name.startswith(("AC", "ADP", "ACAD", "ACPI", "USBC")):
            nodes.append(entry)
    return nodes


def current_power_online() -> bool:
    for node in power_supply_mains_nodes():
        if _read_sysfs_text(f"/sys/class/power_supply/{node}/online") == "1":
            return True
    return False


def _power_node_is_usb_c(node: str) -> bool:
    name = (node or "").lower()
    type_text = _read_sysfs_text(f"/sys/class/power_supply/{node}/type").lower()
    usb_terms = ("usb", "typec", "type-c", "ucsi", "usbc")
    return any(term in name for term in usb_terms) or any(term in type_text for term in usb_terms)


def current_dc_power_online() -> bool:
    """True only for barrel/AC adapter style mains nodes, not USB-C power."""
    for node in power_supply_mains_nodes():
        if _power_node_is_usb_c(node):
            continue
        if _read_sysfs_text(f"/sys/class/power_supply/{node}/online") == "1":
            return True
    return False


_DISPLAY_CONNECTOR_MAP = (
    ("HDMI-A", "hdmi", "HDMI"),
    ("DP-", "displayport", "DisplayPort"),
    ("VGA-", "vga", "VGA"),
    ("DVI-", "dvi", "DVI"),
)


def _display_kind_from_name(name: str) -> str:
    lower = (name or "").lower()
    if "hdmi" in lower:
        return "hdmi"
    if "vga" in lower or "analog" in lower:
        return "vga"
    if re.search(r"(^|[-_/])dp([-_/0-9]|$)", lower) or "displayport" in lower:
        return "displayport"
    if "dvi" in lower:
        return "dvi"
    return ""


def external_display_connectors() -> list[dict]:
    base = "/sys/class/drm"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return []
    connectors = []
    for entry in entries:
        kind = _display_kind_from_name(entry)
        if not kind:
            continue
        connector_path = os.path.join(base, entry)
        if not (
            os.path.exists(os.path.join(connector_path, "status"))
            or os.path.exists(os.path.join(connector_path, "enabled"))
            or os.path.exists(os.path.join(connector_path, "edid"))
        ):
            continue
        status = _read_sysfs_text(os.path.join(connector_path, "status")).lower()
        enabled = _read_sysfs_text(os.path.join(connector_path, "enabled")).lower()
        edid_path = os.path.join(connector_path, "edid")
        try:
            has_edid = os.path.getsize(edid_path) >= 128
        except OSError:
            has_edid = False
        connectors.append({
            "name": entry,
            "kind": kind,
            "label": PORT_KIND_LABELS.get(kind, kind.upper()),
            "connected": status == "connected" or enabled == "enabled" or has_edid,
            "status": status,
            "enabled": enabled,
            "has_edid": has_edid,
        })
    return connectors


def current_connected_display_connectors() -> dict[str, str]:
    """Return connected physical connector names mapped to normalized kinds."""
    connectors = {
        str(c["name"]): str(c["kind"])
        for c in external_display_connectors()
        if c.get("connected") and c.get("name") and c.get("kind")
    }
    xrandr = _run(["xrandr", "--query"], timeout=3).lower()
    for line in xrandr.splitlines():
        if " connected" not in line:
            continue
        name = line.split(None, 1)[0]
        kind = _display_kind_from_name(name)
        if kind:
            connectors.setdefault(f"xrandr:{name}", kind)

    # KMS systems running without X often expose connector state only through
    # libdrm's modetest output.
    modetest = _run(["modetest", "-c"], timeout=4).lower()
    for line in modetest.splitlines():
        if " connected " not in f" {line} ":
            continue
        kind = _display_kind_from_name(line)
        if kind:
            fields = line.split()
            connector_name = next(
                (field for field in fields if _display_kind_from_name(field)),
                line.strip(),
            )
            connectors.setdefault(f"modetest:{connector_name}", kind)
    return connectors


def current_connected_display_kinds() -> set[str]:
    return set(current_connected_display_connectors().values())


def _audio_codec_headphone_matches() -> list[str]:
    proc_base = "/proc/asound"
    matches: list[str] = []
    try:
        card_dirs = [d for d in os.listdir(proc_base) if d.startswith("card")]
    except OSError:
        card_dirs = []
    for card in card_dirs:
        card_path = os.path.join(proc_base, card)
        try:
            codec_files = [f for f in os.listdir(card_path) if f.startswith("codec#")]
        except OSError:
            continue
        for codec in codec_files:
            path = os.path.join(card_path, codec)
            text = _read_sysfs_text(path)
            for block in re.split(r"(?=^Node\s+0x[0-9a-f]+)", text, flags=re.I | re.M):
                lower = block.lower()
                headphone = any(term in lower for term in (
                    "headphone", "headset", "hp out", "line out jack",
                    'control: name="headphone playback',
                ))
                jack = "[jack]" in lower or " jack" in lower or "pincap" in lower
                if headphone and jack:
                    first = next((line.strip() for line in block.splitlines() if line.strip()), codec)
                    matches.append(f"{card}/{codec} {first}")
                    break
    return list(dict.fromkeys(matches))


def _pulse_headphone_port_probe() -> tuple[list[str], bool | None]:
    matches: list[str] = []
    states: list[bool] = []
    for command in (["pactl", "list", "sinks"], ["pactl", "list", "cards"]):
        text = _run(command, timeout=4)
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if "analog-output-headphones" not in line.lower():
                continue
            window = " ".join(part.strip() for part in lines[index:index + 8] if part.strip())
            lower = window.lower()
            state: bool | None = None
            if re.search(r"(?:availability|available):\s*(?:yes|available)\b", lower):
                state = True
            elif re.search(r"(?:availability|available):\s*(?:no|not available)\b", lower):
                state = False
            if state is not None:
                states.append(state)
            matches.append(
                "pactl analog-output-headphones "
                + ("available" if state is True else "not-available" if state is False else "unknown")
            )
    state = True if any(states) else (False if states else None)
    return list(dict.fromkeys(matches)), state


def probe_audio_jack() -> dict:
    """Detect a physical 3.5 mm/headphone output without guessing by model."""
    codec_matches = _audio_codec_headphone_matches()
    pulse_matches, pulse_state = _pulse_headphone_port_probe()
    controls = _run(["amixer", "scontrols"], timeout=3)
    control_matches = [
        line.strip() for line in controls.splitlines()
        if any(term in line.lower() for term in (
            "headphone", "headset", "line out", "line-out",
        ))
    ]
    applicable = bool(codec_matches or pulse_matches or control_matches)
    plugged = current_audio_jack_active() if applicable else False
    state = True if plugged else pulse_state
    evidence_parts = codec_matches + pulse_matches + control_matches[:3]
    return {
        "applicable": applicable,
        "plugged": state,
        "evidence": " | ".join(dict.fromkeys(evidence_parts))[:400]
        or "no headphone pin/port/control detected",
    }


def audio_jack_present() -> bool:
    return bool(probe_audio_jack().get("applicable"))


def current_audio_jack_active() -> bool:
    jack_terms = (
        "headphone", "headset", "line out", "line-out", "line in",
        "external mic", "mic jack", "microphone jack",
    )
    for card in [None, *_alsa_card_indices()]:
        contents = _amixer(card, ["contents"], timeout=3, quiet=False).lower()
        for block in contents.split("numid="):
            if "jack" not in block:
                continue
            if "jack mode" in block:
                continue
            if not any(term in block for term in jack_terms):
                continue
            if re.search(r"values=(on|yes|true|1)\b", block):
                return True

    # Many HDA drivers publish jack state as an input switch instead of an
    # ALSA mixer control. evtest --query returns 10 when the switch is active.
    input_base = "/sys/class/input"
    try:
        input_entries = sorted(os.listdir(input_base))
    except OSError:
        input_entries = []
    for entry in input_entries:
        if not re.fullmatch(r"event\d+", entry):
            continue
        name = _read_sysfs_text(os.path.join(input_base, entry, "device", "name")).lower()
        props = _udev_input_properties(os.path.join("/dev/input", entry))
        switch_capability = _read_sysfs_text(
            os.path.join(input_base, entry, "device", "capabilities", "sw")
        )
        has_switch_capability = any(
            int(word, 16) != 0
            for word in switch_capability.split()
            if re.fullmatch(r"[0-9a-fA-F]+", word)
        )
        if (
            not any(term in name for term in jack_terms)
            and props.get("ID_INPUT_SWITCH") != "1"
            and not has_switch_capability
        ):
            continue
        event_path = os.path.join("/dev/input", entry)
        for switch in ("SW_HEADPHONE_INSERT", "SW_MICROPHONE_INSERT", "SW_LINEOUT_INSERT"):
            try:
                query = subprocess.run(
                    ["evtest", "--query", event_path, "EV_SW", switch],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    check=False,
                )
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                break
            if query.returncode == 10:
                return True

    proc_base = "/proc/asound"
    try:
        card_dirs = [d for d in os.listdir(proc_base) if d.startswith("card")]
    except OSError:
        card_dirs = []
    codec_terms = (*jack_terms, "hp out")
    for card in card_dirs:
        card_path = os.path.join(proc_base, card)
        try:
            codec_files = [f for f in os.listdir(card_path) if f.startswith("codec#")]
        except OSError:
            continue
        for codec in codec_files:
            lines = _read_sysfs_text(os.path.join(card_path, codec)).lower().splitlines()
            for idx, line in enumerate(lines):
                if "pin sense:" not in line:
                    continue
                sense_match = re.search(r"pin sense:\s*0x([0-9a-f]+)", line)
                if not sense_match or not (int(sense_match.group(1), 16) & 0x80000000):
                    continue
                window = "\n".join(lines[max(0, idx - 14): idx + 2])
                if any(term in window for term in codec_terms):
                    return True
    return False


def _usb_physical_port_key(entry: str) -> str:
    """Collapse a USB device/hub path to its stable external root connector."""
    return (entry or "").split(".", 1)[0]


def usb_connector_fingerprint(port_key: str) -> str:
    """Return a stable hardware path for one physical USB insertion point."""
    key = _usb_physical_port_key(port_key)
    sysfs_path = os.path.join("/sys/bus/usb/devices", key)
    properties = _run(
        ["udevadm", "info", "--query=property", "--path", sysfs_path],
        timeout=3,
    )
    for line in properties.splitlines():
        if line.startswith("ID_PATH="):
            value = line.split("=", 1)[1].strip()
            return re.sub(r":\d+\.\d+$", "", value) or key
    try:
        real_path = os.path.realpath(sysfs_path)
    except OSError:
        real_path = ""
    return real_path if real_path and real_path != sysfs_path else key


def usb_physical_port_kind(port_key: str) -> str:
    """Classify a USB insertion from physical connector path evidence."""
    key = _usb_physical_port_key(port_key)
    sysfs_path = os.path.join("/sys/bus/usb/devices", key)
    evidence: list[str] = [key]

    evidence.append(
        _run(
            ["udevadm", "info", "--query=property", "--path", sysfs_path],
            timeout=3,
        )
    )
    try:
        evidence.append(os.path.realpath(sysfs_path))
    except OSError:
        pass

    for relative in (
        "connector",
        "port/connector",
        "typec",
        "typec_port",
        "usb_power_delivery",
    ):
        candidate = os.path.join(sysfs_path, relative)
        try:
            if os.path.lexists(candidate):
                evidence.append(os.path.realpath(candidate))
        except OSError:
            continue

    joined = "\n".join(evidence).lower()
    if any(
        marker in joined
        for marker in ("typec", "type-c", "usb-c", "ucsi", "thunderbolt")
    ):
        return "usb_c"
    if _is_hp_elitebook_640_g10() and re.search(r"(?:pci-|/)0000:00:0d\.", joined):
        return "usb_c"
    return "usb_a"


def current_usb_device_keys() -> set[str]:
    """Return stable physical root-port keys for present non-hub USB devices."""
    base = "/sys/bus/usb/devices"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return set()
    devices: set[str] = set()
    for entry in entries:
        if ":" in entry or entry.startswith("usb"):
            continue
        path = os.path.join(base, entry)
        vendor = _read_sysfs_text(os.path.join(path, "idVendor"))
        product = _read_sysfs_text(os.path.join(path, "idProduct"))
        if not vendor or not product:
            continue
        dev_class = _read_sysfs_text(os.path.join(path, "bDeviceClass")).lower()
        if dev_class == "09":
            continue
        devices.add(_usb_physical_port_key(entry))
    return devices


def _usb_root_port_hint() -> int:
    raw = _run(["lsusb", "-t"], timeout=3)
    ports: list[int] = []
    for line in raw.splitlines():
        match = re.search(r"root_hub,\s*Driver=[^,]+,\s*(\d+)p", line)
        if match:
            ports.append(int(match.group(1)))
    if ports:
        # Do not over-promise every internal hub port. This is just fallback.
        return max(1, min(4, max(ports)))
    raw = _run(["lsusb"], timeout=3)
    return 1 if raw else 0


def external_usb_port_count(base: str = "/sys/bus/usb/devices") -> int:
    """Count physical hot-plug USB sockets exposed by the kernel.

    Modern xHCI systems can expose one physical socket through USB2 and USB3
    companion ports. The sysfs ``peer`` link lets us collapse those into one
    connector, which is substantially more accurate than SMBIOS type-8 data.
    Prefer explicit ``connect_type=hotplug`` ports; fall back to ``unknown``
    only on kernels that do not expose useful connect_type metadata.
    """
    candidates: set[str] = set()
    for pattern in ("usb*/usb*-port*", "usb*-port*"):
        candidates.update(glob.glob(os.path.join(base, pattern)))

    hotplug_physical: set[tuple[str, ...]] = set()
    unknown_physical: set[tuple[str, ...]] = set()
    for path in candidates:
        connect_type = _read_sysfs_text(os.path.join(path, "connect_type")).lower()
        if connect_type and connect_type not in {"hotplug", "unknown"}:
            continue
        real_path = os.path.realpath(path)
        peer_path = os.path.realpath(os.path.join(path, "peer"))
        if not real_path:
            continue
        if peer_path and peer_path != os.path.join(path, "peer"):
            key = tuple(sorted((real_path, peer_path)))
        else:
            key = (real_path,)
        if connect_type == "hotplug":
            hotplug_physical.add(key)
        else:
            unknown_physical.add(key)
    return len(hotplug_physical or unknown_physical)


def _merge_port_rows(rows: list[dict]) -> list[dict]:
    seen_ids = set()
    seen_single_kinds = set()
    merged = []
    for row in rows:
        key = row.get("id")
        kind = row.get("kind")
        if not key or key in seen_ids:
            continue
        # Single-row fallbacks such as hdmi_1 should not duplicate a model
        # row, but multi-row USB profiles must keep every numbered port.
        if key.endswith("_1") and kind in seen_single_kinds:
            continue
        seen_ids.add(key)
        seen_single_kinds.add(kind)
        merged.append(row)
    return merged


def detect_port_profile(profile: str | None = None) -> list[dict]:
    """Return the physical ports the technician should exercise.

    Only hardware-reported connectors are listed. Model-name guesses caused
    nonexistent USB/RJ45/audio rows on configurable business laptops.
    """
    del profile
    rows: list[dict] = []
    counts = dmidecode_port_counts()
    typec_count = len(typec_ports())
    kernel_usb_count = external_usb_port_count()
    if kernel_usb_count:
        # Firmware can include optional chassis connectors while xHCI can
        # expose internal hub paths. When both inventories exist, use the
        # smaller physical total so the checklist does not invent sockets.
        firmware_total = int(counts.get("usb_a", 0)) + int(counts.get("usb_c", 0))
        physical_total = min(kernel_usb_count, firmware_total) if firmware_total else kernel_usb_count
        firmware_typec = int(counts.get("usb_c", 0))
        reliable_typec = min(
            typec_count,
            firmware_typec if firmware_typec else typec_count,
            physical_total,
        )
        counts["usb_c"] = reliable_typec
        counts["usb_a"] = max(0, physical_total - reliable_typec)
    has_builtin_rj45 = bool(built_in_wired_interfaces())
    has_audio_jack = audio_jack_present()
    has_dc_jack = any(
        not _power_node_is_usb_c(node)
        for node in power_supply_mains_nodes()
    )
    for kind, count in counts.items():
        if kind == "usb_c":
            if not typec_count:
                continue
            count = min(count, typec_count)
        elif kind in {"hdmi", "vga", "displayport", "dvi"}:
            # SMBIOS often lists optional chassis ports that are not populated.
            # DRM exposes real external connectors even when unplugged.
            continue
        elif kind == "ethernet" and not has_builtin_rj45:
            continue
        elif kind == "audio" and not has_audio_jack:
            continue
        elif kind == "dc_power" and not has_dc_jack:
            continue
        source = (
            "kernel USB connector topology"
            if kernel_usb_count and kind in {"usb_a", "usb_c"}
            else "SMBIOS external connector"
        )
        rows.extend(_port_rows_for(kind, count, source))

    existing_kinds = {row["kind"] for row in rows}
    for item in external_display_connectors():
        if item["kind"] not in existing_kinds:
            rows.extend(_port_rows_for(
                item["kind"], 1, f"DRM connector {item['name']}"
            ))
            existing_kinds.add(item["kind"])
    return _merge_port_rows(rows)


def wifi_interfaces() -> list[str]:
    raw = _run(["iw", "dev"], timeout=3)
    found: list[str] = []
    current = ""
    current_type = ""
    for line in raw.splitlines() + ["Interface __end__"]:
        match = re.match(r"^\s*Interface\s+(\S+)", line)
        if match:
            if current and current_type not in {"P2P-device", "monitor"}:
                found.append(current)
            current = match.group(1)
            current_type = ""
            continue
        type_match = re.match(r"^\s*type\s+(\S+)", line)
        if type_match:
            current_type = type_match.group(1)
    if found:
        return sorted(set(found))

    base = "/sys/class/net"
    try:
        found = sorted(
            name for name in os.listdir(base)
            if (
                not name.startswith("p2p-")
                and (
                    name.startswith(("wl", "wlan"))
                    or os.path.isdir(os.path.join(base, name, "wireless"))
                )
            )
        )
    except OSError:
        found = []
    if found:
        return found

    nmcli = _run(
        ["nmcli", "--terse", "--fields", "DEVICE,TYPE", "device", "status"],
        timeout=5,
    )
    for line in nmcli.splitlines():
        fields = _split_nmcli_line(line)
        if len(fields) >= 2 and fields[1].strip().lower() == "wifi":
            device = fields[0].strip()
            if device and not device.startswith("p2p-"):
                found.append(device)
    return sorted(set(found))


def _parse_wifi_scan(raw: str) -> tuple[set[str], int]:
    ssids: set[str] = set()
    bss_ids = set(re.findall(
        r"^\s*(?:BSS\s+|Cell\s+\d+\s+-\s+Address:\s*)([0-9A-Fa-f:]{17})",
        raw or "",
        re.M,
    ))
    for line in (raw or "").splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            value = line.split("SSID:", 1)[1].strip()
            if value and value not in {"\\x00", "<hidden>"}:
                ssids.add(value)
        elif "ESSID:" in line:
            value = line.split("ESSID:", 1)[1].strip().strip('"')
            if value and value.lower() not in {"off/any", "<hidden>"}:
                ssids.add(value)
    return ssids, max(len(bss_ids), len(ssids))


def _split_nmcli_line(line: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line.rstrip("\n"):
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def _parse_nmcli_wifi_scan(raw: str) -> tuple[set[str], int]:
    ssids: set[str] = set()
    count = 0
    for line in (raw or "").splitlines():
        fields = _split_nmcli_line(line)
        if len(fields) < 3:
            continue
        ssid = fields[0].strip()
        bssid = fields[1].strip()
        signal = fields[2].strip()
        if ssid:
            ssids.add(ssid)
        if bssid or ssid or signal:
            count += 1
    return ssids, max(count, len(ssids))


def _nmcli_scan_wifi(iface: str | None, timeout: int) -> str:
    if _run(["sh", "-c", "command -v nmcli"], timeout=3) == "":
        return ""
    _run(["systemctl", "start", "NetworkManager"], timeout=20)
    _run(["nmcli", "networking", "on"], timeout=5)
    _run(["nmcli", "radio", "wifi", "on"], timeout=5)
    if iface:
        _run(["nmcli", "device", "set", iface, "managed", "yes"], timeout=5)
        _run(
            ["nmcli", "--wait", "12", "device", "wifi", "rescan", "ifname", iface],
            timeout=max(timeout, 15),
        )
    else:
        _run(
            ["nmcli", "--wait", "12", "device", "wifi", "rescan"],
            timeout=max(timeout, 15),
        )
    time.sleep(0.5)
    cmd = [
        "nmcli", "--terse", "--escape", "yes",
        "--fields", "SSID,BSSID,SIGNAL",
        "device", "wifi", "list",
    ]
    if iface:
        cmd.extend(["ifname", iface])
    cmd.extend(["--rescan", "yes"])
    return _run(cmd, timeout=max(timeout, 20))


def _interface_has_default_route(iface: str) -> bool:
    raw = _run(["ip", "route", "show", "default"], timeout=3)
    return any(
        re.search(rf"\bdev\s+{re.escape(iface)}\b", line)
        for line in raw.splitlines()
    )


def _prepare_wifi_scan_interface(iface: str) -> None:
    """Put the Wi-Fi interface into a clean scan-ready state."""
    _run(["rfkill", "unblock", "all"], timeout=4)
    _run(["rfkill", "unblock", "wifi"], timeout=3)
    _run(["nmcli", "device", "set", iface, "managed", "yes"], timeout=5)
    if not _interface_has_default_route(iface):
        _run(["ip", "link", "set", iface, "down"], timeout=3)
        time.sleep(0.2)
    _run(["ip", "link", "set", iface, "up"], timeout=3)
    _run(["iw", "dev", iface, "set", "power_save", "off"], timeout=3)
    _run(["udevadm", "settle", "--timeout=5"], timeout=6)


def _scan_wifi_with_iw(iface: str, timeout: int) -> tuple[set[str], int, set[str], str]:
    evidence_parts: list[str] = []
    network_ids: set[str] = set()
    best_ssids: set[str] = set()
    best_count = 0
    for label, argv in (
        ("iw active", ["iw", "dev", iface, "scan"]),
        ("iw passive", ["iw", "dev", iface, "scan", "passive"]),
        ("iwlist", ["iwlist", iface, "scanning"]),
    ):
        raw = _run(argv, timeout=timeout)
        ssids, count = _parse_wifi_scan(raw)
        ids = set(re.findall(
            r"^\s*(?:BSS\s+|Cell\s+\d+\s+-\s+Address:\s*)([0-9A-Fa-f:]{17})",
            raw or "",
            re.M,
        ))
        network_ids.update(ids)
        best_ssids.update(ssids)
        best_count = max(best_count, count, len(ids))
        evidence_parts.append(f"{label}={max(count, len(ids), len(ssids))}")
        if best_count:
            break
    return best_ssids, best_count, network_ids, ", ".join(evidence_parts)


def ensure_wireless_ready(timeout_sec: float = 10.0) -> list[str]:
    """Load Wi-Fi support and wait for late USB/PCI adapters to appear."""
    _run(["rfkill", "unblock", "all"], timeout=4)
    for module in ("cfg80211", "mac80211", "iwlwifi", "iwlmvm"):
        _run(["modprobe", module], timeout=4)
    _run(["udevadm", "trigger", "--subsystem-match=net", "--action=add"], timeout=4)
    _run(["udevadm", "settle", "--timeout=6"], timeout=7)
    _run(["systemctl", "start", "NetworkManager"], timeout=20)
    _run(["nmcli", "networking", "on"], timeout=5)
    _run(["nmcli", "radio", "wifi", "on"], timeout=5)

    deadline = time.monotonic() + max(0.0, timeout_sec)
    while True:
        interfaces = wifi_interfaces()
        if interfaces or time.monotonic() >= deadline:
            return interfaces
        time.sleep(0.4)


def scan_wifi_networks(timeout: int = 10, attempts: int = 3) -> dict:
    interfaces = ensure_wireless_ready()
    visible_ssids: set[str] = set()
    network_ids: set[str] = set()
    observed_network_count = 0
    scan_attempts = 0
    scan_evidence: list[str] = []

    _run(["rfkill", "unblock", "wifi"], timeout=3)
    _run(["udevadm", "settle", "--timeout=5"], timeout=6)
    for iface in interfaces:
        _prepare_wifi_scan_interface(iface)
        for attempt in range(max(1, attempts)):
            scan_attempts += 1
            ssids, network_count, ids, iw_evidence = _scan_wifi_with_iw(iface, timeout)
            network_ids.update(ids)
            if not network_count:
                nmcli_raw = _nmcli_scan_wifi(iface, timeout)
                nmcli_ssids, nmcli_count = _parse_nmcli_wifi_scan(nmcli_raw)
                ssids.update(nmcli_ssids)
                network_count = max(network_count, nmcli_count)
                scan_evidence.append(
                    f"{iface} attempt {attempt + 1}: {iw_evidence}; nmcli-iface={nmcli_count}"
                )
            else:
                scan_evidence.append(f"{iface} attempt {attempt + 1}: {iw_evidence}")
            observed_network_count = max(observed_network_count, network_count)
            visible_ssids.update(ssids)
            if network_count:
                break
            if attempt + 1 < max(1, attempts):
                _prepare_wifi_scan_interface(iface)
                time.sleep(1)

    if not observed_network_count:
        nmcli_raw = _nmcli_scan_wifi(None, timeout)
        nmcli_ssids, nmcli_count = _parse_nmcli_wifi_scan(nmcli_raw)
        visible_ssids.update(nmcli_ssids)
        observed_network_count = max(observed_network_count, nmcli_count)
        scan_evidence.append(f"nmcli-global={nmcli_count}")

    network_count = max(observed_network_count, len(network_ids), len(visible_ssids))
    return {
        "interfaces": interfaces,
        "ssids": sorted(visible_ssids),
        "network_count": network_count,
        "hidden_count": max(0, network_count - len(visible_ssids)),
        "attempts": scan_attempts,
        "scan_evidence": "; ".join(scan_evidence[-8:]),
    }


def scan_wifi_ssids(timeout: int = 10) -> list[str]:
    return scan_wifi_networks(timeout=timeout)["ssids"]


def bluetooth_adapters() -> list[str]:
    base = "/sys/class/bluetooth"
    try:
        return sorted(n for n in os.listdir(base) if n.startswith("hci"))
    except OSError:
        return []


def bluetooth_status() -> tuple[bool, str]:
    adapters = bluetooth_adapters()
    if adapters:
        evidence = []
        for adapter in adapters:
            addr = _read_sysfs_text(f"/sys/class/bluetooth/{adapter}/address")
            if addr:
                evidence.append(f"{adapter} {addr}")
            else:
                evidence.append(adapter)
        return True, ", ".join(evidence)
    raw = _run(["bluetoothctl", "list"], timeout=5)
    if raw.strip():
        return True, "bluetoothctl: " + raw.strip().replace("\n", " | ")[:160]
    return False, "no Bluetooth HCI adapter detected"


def run_wireless_check() -> dict:
    wifi = scan_wifi_networks()
    ssids = wifi["ssids"]
    bt_ok, bt_evidence = bluetooth_status()
    wifi_ok = bool(wifi["interfaces"] and wifi["network_count"])
    network_summary = ", ".join(ssids[:10])
    if wifi["hidden_count"]:
        hidden = f"{wifi['hidden_count']} hidden"
        network_summary = f"{network_summary}, {hidden}" if network_summary else hidden
    if not network_summary:
        network_summary = "none"
    return {
        "wifi_ok": wifi_ok,
        "bluetooth_ok": bt_ok,
        "ok": wifi_ok and bt_ok,
        "ssids": ssids[:10],
        "wifi_interfaces": wifi["interfaces"],
        "wifi_network_count": wifi["network_count"],
        "wifi_hidden_count": wifi["hidden_count"],
        "wifi_scan_attempts": wifi["attempts"],
        "evidence": (
            f"Wi-Fi interfaces: {', '.join(wifi['interfaces']) if wifi['interfaces'] else 'none'}; "
            f"networks: {network_summary}; attempts: {wifi['attempts']}; "
            f"scan: {wifi.get('scan_evidence') or 'none'}; "
            f"Bluetooth: {bt_evidence}"
        ),
    }


# ---------------------------------------------------------------------------
# Presence probes — each returns {applicable: bool, evidence: str}
# ---------------------------------------------------------------------------
def probe_display() -> dict:
    """Display is always applicable on a bench laptop — there's a screen."""
    return {"applicable": True, "evidence": "framebuffer console attached"}


def probe_keyboard() -> dict:
    """A built-in keyboard is always present; we just record the device path
    of whatever first input device looks like a keyboard for debug evidence.
    """
    raw = _run(["cat", "/proc/bus/input/devices"])
    keyboards = re.findall(r"Name=\"(.*[Kk]eyboard.*)\"", raw)
    return {
        "applicable": True,
        "evidence": (keyboards[0] if keyboards else "AT keyboard (assumed)"),
    }


def probe_camera() -> dict:
    """Camera = any /dev/video* device. UVC webcams always show up here."""
    devices = []
    if os.path.isdir("/dev"):
        devices = [d for d in os.listdir("/dev") if d.startswith("video")]
    return {
        "applicable": bool(devices),
        "evidence": ", ".join(f"/dev/{d}" for d in sorted(devices)) or "no /dev/video* present",
    }


# Known USB VID:PID prefixes for laptop fingerprint readers
FPR_VENDORS = (
    "138a",  # Validity Sensors / Synaptics
    "06cb",  # Synaptics Inc.
    "147e",  # Upek (legacy)
    "08ff",  # AuthenTec
    "1c7a",  # LighTuning Tech / Egis
    "27c6",  # Goodix
    "0483",  # STMicro (some readers)
    "04f3",  # ELAN (requires known PID or fingerprint descriptor)
    "1188", "10a5", "298d", "2808", "04ca", "045e",
)

FPR_USB_ID_PREFIXES = (
    "04f3:0c",  # ELAN fingerprint readers used in newer HP/Dell laptops
    "04f3:0d",
)

FPR_POSSIBLE_USB_ID_PREFIXES = (
    *FPR_USB_ID_PREFIXES,
    "0a5c:58",  # Dell ControlVault/Broadcom security device; not always FPR.
)


def _fingerprint_name_matches(text: str, props: dict[str, str] | None = None) -> bool:
    """Return True for input/udev names that describe a fingerprint reader."""
    lower = (text or "").lower()
    if any(skip in lower for skip in ("touchpad", "trackpad", "clickpad", "keyboard", "mouse")):
        return False
    if props and any("FINGERPRINT" in key or "FINGERPRINT" in value.upper() for key, value in props.items()):
        return True
    terms = (
        "fingerprint", "finger print", "biometric", "validity", "authentec",
        "upek", "egis", "goodix fingerprint", "synaptics wbd", "fpc",
        "elan fingerprint", "moc fingerprint", "fingerprint reader",
        "wbdi", "tod fingerprint", "goodix_fp", "goodix fp",
        "syna fp", "elan fp",
    )
    return any(term in lower for term in terms)


def _fingerprint_bus_id_matches(text: str) -> bool:
    """Match embedded fingerprint HIDs without treating every touchpad as FPR."""
    upper = (text or "").upper()
    if re.search(r"\b(?:FPC|FTE)[A-Z0-9_-]*\b", upper):
        return True
    if re.search(r"\bGDIX0000\b", upper):
        return True
    if (
        re.search(r"\bGDIX[0-9A-F]{4,}\b", upper)
        and re.search(r"FINGER|BIOMETRIC|\bFP\b", upper)
    ):
        return True
    # SYNA*/ELAN* prefixes are shared with touchpads and touchscreens. Require
    # explicit fingerprint/biometric context for those vendors.
    return bool(
        re.search(r"\b(?:SYNA|ELAN)[A-Z0-9_-]*\b", upper)
        and re.search(r"FINGER|BIOMETRIC|\bFP\b|WBDI", upper)
    )


def _fingerprint_usb_id_matches(
    vid: str,
    pid: str,
    desc: str = "",
    *,
    strict: bool = False,
) -> bool:
    vid = (vid or "").lower()
    pid = (pid or "").lower()
    ident = f"{vid}:{pid}"
    if any(ident.startswith(prefix) for prefix in FPR_USB_ID_PREFIXES):
        return True
    if _fingerprint_name_matches(desc):
        return True
    if strict:
        return False
    if any(ident.startswith(prefix) for prefix in FPR_POSSIBLE_USB_ID_PREFIXES):
        return True
    if vid in FPR_VENDORS:
        return True
    return False


def _fingerprint_udev_database_matches() -> list[str]:
    """Find biometric devices that do not expose regular input events."""
    raw = _run(["udevadm", "info", "--export-db"], timeout=5)
    matches: list[str] = []
    for block in re.split(r"\n\s*\n", raw):
        if not block.strip():
            continue
        text = " ".join(line.strip() for line in block.splitlines())
        props = {}
        for line in block.splitlines():
            if line.startswith("E: ") and "=" in line:
                key, value = line[3:].split("=", 1)
                props[key] = value
        vid = props.get("ID_VENDOR_ID", "").lower()
        pid = props.get("ID_MODEL_ID", "").lower()
        desc = " ".join(
            props.get(key, "")
            for key in ("ID_VENDOR", "ID_MODEL", "ID_MODEL_FROM_DATABASE", "HID_NAME", "NAME")
        )
        if _fingerprint_usb_id_matches(vid, pid, desc, strict=True) or _fingerprint_name_matches(text, props):
            device = props.get("DEVNAME") or props.get("DEVPATH") or desc or text[:80]
            matches.append(f"udev {device}".strip())
    return list(dict.fromkeys(m for m in matches if m))


def _fingerprint_sysfs_matches() -> list[str]:
    """Scan HID/I2C/SPI/ACPI names for built-in fingerprint readers."""
    roots = (
        "/sys/bus/hid/devices",
        "/sys/bus/i2c/devices",
        "/sys/bus/spi/devices",
        "/sys/bus/acpi/devices",
    )
    files = ("name", "modalias", "uevent", "hid")
    matches: list[str] = []
    for root in roots:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(root, entry)
            chunks = [entry]
            for name in files:
                value = _read_sysfs_text(os.path.join(path, name)).strip()
                if value:
                    chunks.append(value)
            text = " ".join(chunks)
            if _fingerprint_name_matches(text) or _fingerprint_bus_id_matches(text):
                matches.append(f"sysfs {entry} {chunks[1] if len(chunks) > 1 else ''}".strip())
    return list(dict.fromkeys(m for m in matches if m))


def _fingerprint_kernel_matches() -> list[str]:
    matches: list[str] = []
    dmesg = _run(["dmesg"], timeout=5)
    for line in dmesg.splitlines():
        if _fingerprint_name_matches(line) or _fingerprint_bus_id_matches(line):
            matches.append(f"dmesg {line.strip()}")
    lspci = _run(["lspci", "-nn"], timeout=5)
    for line in lspci.splitlines():
        if re.search(r"fingerprint|biometric|\bfpc\b|goodix[_ -]?fp", line, re.I):
            matches.append(f"pci {line.strip()}")
    return list(dict.fromkeys(matches))[:8]


def _fingerprint_fprintd_probe() -> list[str]:
    """Ask fprintd briefly; timeout after device selection means a reader exists."""
    binary = shutil.which("fprintd-enroll")
    if not binary:
        return []
    user = os.environ.get("USER") or "user"
    try:
        rc = subprocess.run(
            ["timeout", "2", binary, user],
            input="",
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    text = ((rc.stdout or "") + "\n" + (rc.stderr or "")).strip()
    lower = text.lower()
    if any(term in lower for term in ("no devices available", "no device available", "list_devices failed")):
        return []
    if rc.returncode == 124 or any(term in lower for term in (
        "using device", "enrolling", "enroll result", "finger", "fingerprint",
    )):
        line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "fprintd reader present")
        return [f"fprintd {line}"]
    return []


def _hidraw_usb_ids(props: dict[str, str], text: str) -> tuple[str, str]:
    vid = (props.get("ID_VENDOR_ID") or "").lower()
    pid = (props.get("ID_MODEL_ID") or "").lower()
    if vid and pid:
        return vid, pid
    hid_id = props.get("HID_ID") or ""
    if not hid_id:
        match = re.search(r"HID_ID=([^\s]+)", text)
        hid_id = match.group(1) if match else ""
    match = re.search(r":([0-9a-fA-F]{4,8}):([0-9a-fA-F]{4,8})", hid_id)
    if match:
        return match.group(1)[-4:].lower(), match.group(2)[-4:].lower()
    return vid, pid


def fingerprint_hidraw_devices() -> list[str]:
    """Return raw HID fingerprint devices for sensors without evdev events."""
    devices: list[str] = []
    try:
        names = sorted(name for name in os.listdir("/dev") if name.startswith("hidraw"))
    except OSError:
        names = []
    for name in names:
        path = f"/dev/{name}"
        props = _udev_input_properties(path)
        sysfs_path = os.path.join("/sys/class/hidraw", name, "device")
        chunks = [
            props.get("ID_VENDOR", ""),
            props.get("ID_MODEL", ""),
            props.get("ID_MODEL_FROM_DATABASE", ""),
            props.get("HID_NAME", ""),
            _read_sysfs_text(os.path.join(sysfs_path, "uevent")),
            _read_sysfs_text(os.path.join(sysfs_path, "name")),
        ]
        text = " ".join(chunk for chunk in chunks if chunk)
        vid, pid = _hidraw_usb_ids(props, text)
        if _fingerprint_usb_id_matches(vid, pid, text, strict=True) or _fingerprint_name_matches(text, props):
            label = text.splitlines()[0].strip() if text.strip() else f"{vid}:{pid}"
            devices.append(f"{path} {label}".strip())
    return list(dict.fromkeys(d for d in devices if d))


def fingerprint_event_devices() -> list[str]:
    """Return fingerprint input event devices, when the kernel exposes them."""
    devices: list[str] = []
    try:
        from evdev import InputDevice, list_devices  # type: ignore

        paths = set(list_devices())
        try:
            paths.update(
                os.path.join("/dev/input", name)
                for name in os.listdir("/dev/input")
                if name.startswith("event")
            )
        except OSError:
            pass
        for path in sorted(paths):
            try:
                dev = InputDevice(path)
                try:
                    props = _udev_input_properties(path)
                    text = " ".join(
                        value
                        for value in (
                            dev.name or "",
                            props.get("ID_MODEL", ""),
                            props.get("ID_VENDOR", ""),
                        )
                        if value
                    )
                    if _fingerprint_name_matches(text, props):
                        devices.append(f"{path} {dev.name}".strip())
                finally:
                    dev.close()
            except (OSError, PermissionError):
                continue
    except ImportError:
        pass
    return list(dict.fromkeys(d for d in devices if d))


def probe_fingerprint() -> dict:
    """Detect fingerprint hardware separately from a Linux touch path."""
    raw = _run(["lsusb"])
    hardware_matches: list[str] = []
    possible_matches: list[str] = []
    for line in raw.splitlines():
        m = re.search(r"ID\s+([0-9a-f]{4}):([0-9a-f]{4})\s+(.+)$", line, re.I)
        if not m:
            continue
        vid, pid, desc = m.group(1).lower(), m.group(2), m.group(3)
        label = f"{vid}:{pid} {desc.strip()}"
        if _fingerprint_usb_id_matches(vid, pid, desc, strict=True):
            hardware_matches.append(label)
        elif _fingerprint_usb_id_matches(vid, pid, desc, strict=False):
            possible_matches.append(label)
    event_matches = fingerprint_event_devices()
    hidraw_matches = fingerprint_hidraw_devices()
    fprintd_matches = _fingerprint_fprintd_probe()
    hardware_matches.extend(_fingerprint_udev_database_matches())
    hardware_matches.extend(_fingerprint_sysfs_matches())
    hardware_matches.extend(_fingerprint_kernel_matches())
    hardware_matches.extend(event_matches)
    hardware_matches.extend(hidraw_matches)
    hardware_matches.extend(fprintd_matches)
    hardware_matches = list(dict.fromkeys(m for m in hardware_matches if m))
    touch_matches = list(dict.fromkeys(event_matches + hidraw_matches + fprintd_matches))
    possible_matches = list(dict.fromkeys(
        m for m in possible_matches if m and m not in hardware_matches
    ))
    touch_capable = bool(touch_matches)
    if hardware_matches:
        evidence = " | ".join(hardware_matches)[:400]
    elif possible_matches:
        evidence = (
            "possible biometric/security device ignored until Linux confirms fingerprint: "
            + " | ".join(possible_matches)
        )[:400]
    else:
        evidence = "no fingerprint reader detected"
    return {
        "applicable": bool(hardware_matches),
        "touch_capable": touch_capable,
        "driver_available": touch_capable,
        "possible": bool(possible_matches),
        "possible_evidence": " | ".join(possible_matches)[:400],
        "evidence": evidence,
    }


def _list_alsa_cards(kind: str) -> list[str]:
    """kind = 'playback' | 'capture'. Returns aplay/arecord -l names."""
    cmd = "aplay" if kind == "playback" else "arecord"
    raw = _run([cmd, "-l"])
    cards: list[str] = []
    for line in raw.splitlines():
        m = re.match(r"card\s+(\d+):\s+([^,]+),", line)
        if m:
            cards.append(f"card{m.group(1)} {m.group(2).strip()}")
    return cards


def _list_alsa_pcm_devices(kind: str) -> list[dict]:
    """Return parsed ALSA PCM devices from aplay/arecord -l."""
    cmd = "aplay" if kind == "playback" else "arecord"
    raw = _run([cmd, "-l"])
    devices: list[dict] = []
    for line in raw.splitlines():
        match = re.match(
            r"card\s+(\d+):\s+([^,]+),\s+device\s+(\d+):\s+([^\[]+)(?:\[(.+)\])?",
            line.strip(),
            re.I,
        )
        if not match:
            continue
        card, card_name, device, device_name, bracket_name = match.groups()
        card_id = re.split(r"[\s\[]", card_name.strip(), maxsplit=1)[0]
        label = " ".join(part.strip() for part in (card_name, device_name, bracket_name or "") if part)
        devices.append({
            "card": int(card),
            "device": int(device),
            "card_id": card_id,
            "card_name": card_name.strip(),
            "device_name": device_name.strip(),
            "label": label,
            "alsa": f"plughw:{card},{device}",
        })
    return devices


def _alsa_kernel_cards() -> list[str]:
    """Return ALSA cards from proc/sysfs even when aplay parsing is unavailable."""
    cards: list[str] = []
    raw = _read_sysfs_text("/proc/asound/cards")
    for line in raw.splitlines():
        match = re.match(r"\s*(\d+)\s+\[([^\]]+)\]\s*:\s*(.+)", line)
        if match:
            cards.append(
                f"card{match.group(1)} {match.group(2).strip()} {match.group(3).strip()}"
            )
    try:
        sysfs_cards = sorted(
            name for name in os.listdir("/sys/class/sound")
            if re.fullmatch(r"card\d+", name)
        )
    except OSError:
        sysfs_cards = []
    for card in sysfs_cards:
        cards.append(card)
    return list(dict.fromkeys(cards))


def _alsa_has_pcm_direction(kind: str) -> bool:
    direction = "playback" if kind == "playback" else "capture"
    return direction in _read_sysfs_text("/proc/asound/pcm").lower()


def ensure_audio_ready(timeout_sec: float = 8.0) -> str:
    """Give kernel/udev time to expose laptop audio before QC starts."""
    profile_evidence = prime_audio_driver_for_model()
    deadline = time.monotonic() + max(0.0, timeout_sec)
    attempts = 0
    while True:
        playback = _list_alsa_pcm_devices("playback")
        capture = _list_alsa_pcm_devices("capture")
        cards = _alsa_kernel_cards()
        if playback or capture or cards:
            return (
                f"audio ready: playback={len(playback)} capture={len(capture)} "
                f"cards={len(cards)}; {profile_evidence}"
            )
        if time.monotonic() >= deadline:
            return f"audio initialization timed out: no ALSA cards or PCM devices; {profile_evidence}"
        if attempts == 0:
            for module in (
                "snd_hda_intel",
                "snd_hda_codec_realtek",
                "snd_hda_codec_hdmi",
                "snd_sof_pci",
                "snd_soc_avs",
                "snd_usb_audio",
            ):
                _run(["modprobe", module], timeout=4)
            _run(
                ["udevadm", "trigger", "--subsystem-match=sound", "--action=add"],
                timeout=4,
            )
            _run(["udevadm", "settle", "--timeout=4"], timeout=5)
            _run(["alsactl", "init"], timeout=6)
        attempts += 1
        time.sleep(0.4)


def _rank_capture_device(item: dict) -> tuple[int, str]:
    text = f"{item.get('card_name', '')} {item.get('device_name', '')} {item.get('label', '')}".lower()
    if any(term in text for term in ("hdmi", "display", "monitor", "output")):
        return (80, text)
    score = 40
    if any(term in text for term in ("mic", "microphone", "dmic", "array")):
        score -= 30
    if any(term in text for term in ("analog", "alc", "realtek", "hda", "pch", "sof", "acp")):
        score -= 20
    return (score, text)


def _capture_device_needs_software_route(item: dict) -> bool:
    text = f"{item.get('card_name', '')} {item.get('device_name', '')} {item.get('label', '')}".lower()
    return any(term in text for term in ("sof", "dmic", "digital mic", "microphone array", "mic array"))


def _list_alsa_capture_names() -> list[str]:
    """Return useful arecord -L capture names, with noisy helpers filtered."""
    raw = _run(["arecord", "-L"], timeout=4)
    names: list[str] = []
    skip_terms = (
        "hdmi", "monitor", "output", "null", "lavrate", "samplerate",
        "speex", "jack", "dsnoop",
    )
    prefer_terms = ("default", "sysdefault", "pulse", "pipewire", "plughw", "hw")
    for line in raw.splitlines():
        name = line.strip()
        if not name or name.startswith((" ", "\t")) or ":" not in name and name not in ("default", "pulse", "pipewire"):
            if name != "default":
                continue
        lower = name.lower()
        if any(term in lower for term in skip_terms):
            continue
        if not any(term in lower for term in prefer_terms):
            continue
        names.append(name)
    return list(dict.fromkeys(names))


def _capture_device_args() -> list[list[str]]:
    """Preferred arecord arguments, with direct internal hardware first.

    Old HDA/Realtek systems are more reliable through ``plughw`` than through
    sysdefault/dsnoop. The latter may open an unused jack or a software monitor
    and still return success, producing static that looks louder than speech.
    Newer SOF/DMIC microphone arrays still need the hardware DMIC PCM first;
    sysdefault remains a fallback because on some distros it routes through
    the DSP topology correctly.
    """
    devices = sorted(_list_alsa_pcm_devices("capture"), key=_rank_capture_device)
    args: list[list[str]] = []
    seen = set()
    compatibility: list[str] = []
    for item in devices:
        raw_card_id = item.get("card_id") or re.split(
            r"[\s\[]", item.get("card_name", "").strip(), maxsplit=1,
        )[0]
        card_name = re.sub(r"[^A-Za-z0-9_]", "_", raw_card_id).strip("_")
        direct = []
        if card_name:
            plughw = f"plughw:CARD={card_name},DEV={item['device']}"
            sysdefault = f"sysdefault:CARD={card_name}"
            if _capture_device_needs_software_route(item):
                direct.extend([plughw, item["alsa"], sysdefault])
                compatibility.append(f"hw:CARD={card_name},DEV={item['device']}")
            else:
                direct.append(plughw)
                compatibility.extend([
                    f"hw:CARD={card_name},DEV={item['device']}",
                    sysdefault,
                ])
        direct.append(item["alsa"])
        compatibility.append(f"hw:{item['card']},{item['device']}")
        for dev in direct:
            if dev in seen:
                continue
            seen.add(dev)
            args.append(["-D", dev])
    for dev in compatibility:
        if dev not in seen:
            seen.add(dev)
            args.append(["-D", dev])
    # Software audio servers are last-resort fallbacks in the live image.
    # They can expose monitor sources which contain playback instead of mic.
    for dev in ("default", "pipewire", "pulse"):
        if dev not in seen:
            seen.add(dev)
            args.append(["-D", dev])
    for dev in _list_alsa_capture_names():
        if dev in seen:
            continue
        seen.add(dev)
        args.append(["-D", dev])
    args.append([])
    return args


def _rank_playback_device(item: dict) -> tuple[int, str]:
    text = f"{item.get('card_name', '')} {item.get('device_name', '')} {item.get('label', '')}".lower()
    if any(term in text for term in ("hdmi", "displayport", "display audio", "monitor")):
        return (90, text)
    score = 40
    if any(term in text for term in ("analog", "alc", "realtek", "hda", "pch", "speaker")):
        score -= 25
    if any(term in text for term in ("sof", "acp", "audio")):
        score -= 10
    return (score, text)


def _playback_device_args() -> list[list[str]]:
    """Preferred playback devices, keeping old Dell analog speakers before HDMI."""
    devices = sorted(_list_alsa_pcm_devices("playback"), key=_rank_playback_device)
    args: list[list[str]] = []
    seen = set()
    for dev in ("default", "pipewire", "pulse"):
        if dev not in seen:
            seen.add(dev)
            args.append(["-D", dev])
    for item in devices:
        raw_card_id = item.get("card_id") or re.split(
            r"[\s\[]", item.get("card_name", "").strip(), maxsplit=1,
        )[0]
        card_name = re.sub(r"[^A-Za-z0-9_]", "_", raw_card_id).strip("_")
        named = []
        if card_name:
            named.extend([
                f"sysdefault:CARD={card_name}",
                f"plughw:CARD={card_name},DEV={item['device']}",
                f"hw:CARD={card_name},DEV={item['device']}",
            ])
        for dev in named + [item["alsa"], f"hw:{item['card']},{item['device']}"]:
            if dev in seen:
                continue
            seen.add(dev)
            args.append(["-D", dev])
    args.append([])
    return args


def _headphone_playback_device_args() -> list[list[str]]:
    """Preferred headphone devices, restricted to real non-HDMI playback PCMs.

    Some old Dell/Lenovo codecs accept a stream on the default Pulse/PipeWire
    PCM while the analog jack path only emits a small pop. For the dedicated
    headphone test, try the real analog HDA/Realtek PCM devices first so a
    successful ``aplay`` means we actually exercised the headphone codec. Do
    not add default/pulse/pipewire fallbacks here; those are exactly the paths
    that can produce the misleading knocking sound reported on the bench.
    """
    devices = sorted(_list_alsa_pcm_devices("playback"), key=_rank_playback_device)
    args: list[list[str]] = []
    seen = set()

    def add(dev: str) -> None:
        if dev in seen:
            return
        seen.add(dev)
        args.append(["-D", dev])

    compatibility: list[str] = []
    for item in devices:
        text = f"{item.get('card_name', '')} {item.get('device_name', '')} {item.get('label', '')}".lower()
        if any(term in text for term in ("hdmi", "displayport", "display audio", "monitor")):
            continue
        raw_card_id = item.get("card_id") or re.split(
            r"[\s\[]", item.get("card_name", "").strip(), maxsplit=1,
        )[0]
        card_name = re.sub(r"[^A-Za-z0-9_]", "_", raw_card_id).strip("_")
        if card_name:
            add(f"plughw:CARD={card_name},DEV={item['device']}")
            add(f"sysdefault:CARD={card_name}")
            compatibility.append(f"hw:CARD={card_name},DEV={item['device']}")
        add(item["alsa"])
        compatibility.append(f"hw:{item['card']},{item['device']}")

    for dev in compatibility:
        add(dev)
    return args


def probe_speaker() -> dict:
    pcm = _list_alsa_pcm_devices("playback")
    cards = [
        f"card{d['card']} device{d['device']} {d['label']}".strip()
        for d in pcm
    ]
    if not cards:
        cards = _list_alsa_cards("playback")
    if not cards:
        cards = _alsa_kernel_cards()
    applicable = bool(cards or _alsa_has_pcm_direction("playback"))
    return {
        "applicable": applicable,
        "evidence": " | ".join(cards)[:200] or "no ALSA playback cards found",
    }


def probe_microphone() -> dict:
    devices = _list_alsa_pcm_devices("capture")
    cards = [f"card{d['card']} device{d['device']} {d['label']}".strip() for d in devices]
    if not cards:
        cards = _list_alsa_cards("capture")
    if not cards:
        cards = _alsa_kernel_cards()
    applicable = bool(cards or _alsa_has_pcm_direction("capture"))
    return {
        "applicable": applicable,
        "evidence": " | ".join(cards)[:200] or "no ALSA capture cards found",
    }


def _driver_preflight_identity() -> str:
    values = []
    for path in (
        "/sys/class/dmi/id/sys_vendor",
        "/sys/class/dmi/id/product_name",
        "/sys/class/dmi/id/product_version",
        "/sys/class/dmi/id/product_sku",
    ):
        value = _read_sysfs_text(path)
        if value and value.lower() not in {"none", "not specified", "to be filled by o.e.m."}:
            values.append(value)
    return " ".join(dict.fromkeys(values)) or "model=UNKNOWN"


def _driver_preflight_lspci_audio() -> list[str]:
    return _audio_pci_lines()[:4]


def _driver_preflight_lsusb_biometrics() -> list[str]:
    raw = _run(["lsusb"], timeout=5)
    lines = []
    for line in raw.splitlines():
        match = re.search(r"ID\s+([0-9a-f]{4}):([0-9a-f]{4})\s+(.+)$", line, re.I)
        if not match:
            continue
        vid, pid, desc = match.group(1).lower(), match.group(2).lower(), match.group(3)
        if _fingerprint_usb_id_matches(vid, pid, desc):
            lines.append(f"{vid}:{pid} {desc.strip()}")
    return lines[:4]


def _driver_preflight_status() -> tuple[str, str, str]:
    """Return (result, remarks, evidence) for Linux driver readiness.

    Vendor repositories mostly publish Windows driver installers. The VSTL
    live boot can safely use Linux kernel modules, firmware packages, ALSA,
    and libfprint/fprintd; it cannot install those Windows packages at runtime.
    """
    audio_ready = ensure_audio_ready(timeout_sec=6)
    speaker = probe_speaker()
    mic = probe_microphone()
    fingerprint = probe_fingerprint()
    fprint_evidence = fingerprint.get("evidence", "")
    fprint_supported = bool(fingerprint.get("touch_capable"))
    typec = typec_ports()
    partners = current_typec_partners()
    audio_ids = _driver_preflight_lspci_audio()
    fingerprint_ids = _driver_preflight_lsusb_biometrics()

    issues = []
    warnings = []
    if not (speaker.get("applicable") and mic.get("applicable")):
        issues.append("audio driver/capture path not ready")
    if fingerprint.get("possible") and not fingerprint.get("applicable"):
        warnings.append("possible fingerprint/security hardware hidden from QC until Linux confirms it")
    elif fingerprint.get("applicable") and not fprint_supported:
        warnings.append("fingerprint present but no live Linux touch driver; QC records N/A")

    result = "PASS" if not issues else "FAIL"
    if issues:
        remarks = "; ".join(issues) + "; vendor Windows driver download not supported in Linux live boot"
    else:
        remarks = "Linux drivers ready; vendor Windows driver download not required"
    if warnings:
        remarks += "; " + "; ".join(warnings)
    evidence_parts = [
        f"model={_driver_preflight_identity()}",
        f"audio={audio_ready}",
        f"speaker={'yes' if speaker.get('applicable') else 'no'}",
        f"microphone={'yes' if mic.get('applicable') else 'no'}",
        f"fingerprint_test_shown={'yes' if fingerprint.get('applicable') else 'no'}",
        f"fingerprint_possible={'yes' if fingerprint.get('possible') else 'no'}",
        f"fingerprint_linux={'yes' if fprint_supported else 'no'}",
        f"typec_ports={len(typec)}",
        f"typec_partners={len(partners)}",
    ]
    if audio_ids:
        evidence_parts.append("audio_ids=" + " | ".join(audio_ids))
    if fingerprint_ids:
        evidence_parts.append("fingerprint_ids=" + " | ".join(fingerprint_ids))
    evidence_parts.append("fingerprint_probe=" + fprint_evidence)
    return result, remarks, "; ".join(part for part in evidence_parts if part)[:900]


def driver_preflight_result() -> dict:
    result, remarks, evidence = _driver_preflight_status()
    return make_result(
        "driver_preflight",
        applicable=True,
        ran=True,
        result=result,
        remarks=remarks,
        evidence=evidence,
    )


def _touch_name_is_direct_panel(name: str) -> bool:
    lower = (name or "").lower()
    if any(skip in lower for skip in ("touchpad", "trackpad", "clickpad")):
        return False
    direct_terms = (
        "touchscreen", "touch screen", "multi-touch", "multitouch",
        "digitizer", "wacom", "n-trig", "maxtouch", "goodix",
        "capacitive touch",
    )
    return any(term in lower for term in direct_terms)


def _udev_input_properties(path: str) -> dict[str, str]:
    raw = _run(["udevadm", "info", "--query=property", "--name", path])
    props: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    return props


def _touch_device_classification(
    name: str,
    *,
    has_mt_xy: bool,
    has_abs_xy: bool,
    has_touch_key: bool,
    input_prop_direct: bool,
    input_prop_pointer: bool,
    udev_touchscreen: bool,
    udev_touchpad: bool,
) -> tuple[bool, str]:
    """Classify a direct-touch panel without confusing it with a touchpad."""
    lower = (name or "").lower()
    if udev_touchpad or any(skip in lower for skip in ("touchpad", "trackpad", "clickpad")):
        return False, "touchpad"
    if udev_touchscreen:
        return True, "udev ID_INPUT_TOUCHSCREEN=1"
    if input_prop_direct and (has_mt_xy or has_abs_xy):
        return True, "evdev INPUT_PROP_DIRECT"
    if input_prop_pointer:
        return False, "indirect pointer"
    if has_mt_xy and has_touch_key:
        return True, "multitouch touch coordinates"
    if has_mt_xy and (has_touch_key or _touch_name_is_direct_panel(name)):
        return True, "multitouch coordinates"
    if has_abs_xy and has_touch_key and _touch_name_is_direct_panel(name):
        return True, "absolute touch coordinates"
    return False, "not a direct touch panel"


def touchscreen_devices() -> list[str]:
    """Return touch-panel input devices as '<path> <name>' strings.

    Prefer evdev capabilities so panels whose name is just HID/I2C still
    detect correctly. Fall back to /proc/bus/input/devices name matching.
    """
    devices: list[str] = []
    try:
        from evdev import InputDevice, ecodes, list_devices  # type: ignore

        for path in list_devices():
            try:
                dev = InputDevice(path)
                name = dev.name or ""
                caps = dev.capabilities()
                key_codes = set(caps.get(ecodes.EV_KEY, []))
                abs_codes = set(caps.get(ecodes.EV_ABS, []))
                has_mt_xy = (
                    ecodes.ABS_MT_POSITION_X in abs_codes
                    and ecodes.ABS_MT_POSITION_Y in abs_codes
                )
                has_abs_xy = ecodes.ABS_X in abs_codes and ecodes.ABS_Y in abs_codes
                finger_code = getattr(ecodes, "BTN_TOOL_FINGER", -1)
                has_touch_key = ecodes.BTN_TOUCH in key_codes or finger_code in key_codes
                try:
                    input_props = set(dev.input_props())
                except (OSError, AttributeError):
                    input_props = set()
                direct_code = getattr(ecodes, "INPUT_PROP_DIRECT", -1)
                pointer_code = getattr(ecodes, "INPUT_PROP_POINTER", -1)
                udev_props = _udev_input_properties(path)
                is_touch, reason = _touch_device_classification(
                    name,
                    has_mt_xy=has_mt_xy,
                    has_abs_xy=has_abs_xy,
                    has_touch_key=has_touch_key,
                    input_prop_direct=direct_code in input_props,
                    input_prop_pointer=pointer_code in input_props,
                    udev_touchscreen=udev_props.get("ID_INPUT_TOUCHSCREEN") == "1",
                    udev_touchpad=udev_props.get("ID_INPUT_TOUCHPAD") == "1",
                )
                if is_touch:
                    devices.append(f"{path} {name} [{reason}]".strip())
            except (OSError, PermissionError):
                continue
    except ImportError:
        pass

    # Some HID-over-I2C panels are tagged correctly by udev before python-evdev
    # can read their capabilities. Trust the kernel/udev touchscreen property.
    input_root = "/dev/input"
    try:
        event_names = sorted(name for name in os.listdir(input_root) if name.startswith("event"))
    except OSError:
        event_names = []
    for event_name in event_names:
        path = os.path.join(input_root, event_name)
        props = _udev_input_properties(path)
        if props.get("ID_INPUT_TOUCHPAD") == "1":
            continue
        if props.get("ID_INPUT_TOUCHSCREEN") != "1":
            continue
        sysfs_name = _read_sysfs_text(f"/sys/class/input/{event_name}/device/name")
        devices.append(f"{path} {sysfs_name} [udev ID_INPUT_TOUCHSCREEN=1]".strip())

    raw = _run(["cat", "/proc/bus/input/devices"])
    for block in re.split(r"\n\s*\n", raw):
        name_match = re.search(r'N:\s+Name="([^"]+)"', block)
        if not name_match:
            continue
        name = name_match.group(1)
        if not _touch_name_is_direct_panel(name):
            continue
        handler_match = re.search(r"H:\s+Handlers=([^\n]+)", block)
        event = ""
        if handler_match:
            for token in handler_match.group(1).split():
                if token.startswith("event"):
                    event = f"/dev/input/{token}"
                    break
        devices.append(f"{event} {name}".strip())

    return list(dict.fromkeys(d for d in devices if d))


def probe_touchscreen() -> dict:
    """Touchscreen = a direct touch panel detected from evdev capabilities."""
    matches = touchscreen_devices()
    return {
        "applicable": bool(matches),
        "evidence": " | ".join(matches)[:200] or "display_type=non-touch; no touchscreen detected",
    }


def probe_wireless() -> dict:
    wifi = ensure_wireless_ready(timeout_sec=8)
    bt = bluetooth_adapters()
    evidence = []
    evidence.append("wifi=" + (", ".join(wifi) if wifi else "none"))
    evidence.append("bluetooth=" + (", ".join(bt) if bt else "none"))
    return {
        "applicable": bool(wifi or bt),
        "evidence": "; ".join(evidence),
    }


def probe_ports() -> dict:
    """Return the detected physical connector checklist for the ports test."""
    ports = detect_port_profile()
    labels = [p["label"] for p in ports]
    return {
        "applicable": bool(ports),
        "evidence": " | ".join(labels)[:240] or "no external ports detected",
    }


def probe_power_adapter() -> dict:
    nodes = power_supply_mains_nodes()
    return {
        "applicable": bool(nodes),
        "evidence": "AC nodes: " + (", ".join(nodes) if nodes else "none"),
    }


def _block_device_is_internal_disk(dev: dict) -> bool:
    if dev.get("type") != "disk":
        return False
    name = str(dev.get("name") or "")
    if name.startswith(("loop", "ram", "sr", "fd", "zram")):
        return False
    tran = str(dev.get("tran") or "").lower()
    if tran == "usb":
        return False
    try:
        if int(dev.get("rm", 0) or 0) == 1:
            return False
    except (TypeError, ValueError):
        pass
    try:
        if int(dev.get("hotplug", 0) or 0) == 1:
            return False
    except (TypeError, ValueError):
        pass
    mounts = dev.get("mountpoints") or []
    if isinstance(mounts, str):
        mounts = [mounts]
    live_mount_terms = ("/run/live", "/lib/live/mount", "/run/archiso", "/cdrom")
    for mount in mounts:
        if mount and any(str(mount).startswith(term) for term in live_mount_terms):
            return False
    return True


def _candidate_storage_disks() -> list[dict]:
    raw = _run([
        "lsblk", "-Jbo",
        "NAME,PATH,SIZE,TYPE,TRAN,RM,HOTPLUG,MOUNTPOINTS",
    ])
    if raw:
        try:
            data = json.loads(raw)
            disks = [
                dev for dev in data.get("blockdevices", [])
                if _block_device_is_internal_disk(dev)
            ]
            if disks:
                return disks
        except (json.JSONDecodeError, TypeError):
            pass

    fallback = []
    text = _run(["lsblk", "-dno", "NAME,SIZE,TYPE,TRAN,RM,HOTPLUG"])
    for ln in text.splitlines():
        parts = ln.split()
        if len(parts) < 3:
            continue
        name, size, typ = parts[:3]
        tran = parts[3] if len(parts) > 3 else ""
        rm = parts[4] if len(parts) > 4 else "0"
        hotplug = parts[5] if len(parts) > 5 else "0"
        dev = {
            "name": name,
            "path": f"/dev/{name}",
            "size": size,
            "type": typ,
            "tran": tran,
            "rm": rm,
            "hotplug": hotplug,
            "mountpoints": [],
        }
        if _block_device_is_internal_disk(dev):
            fallback.append(dev)
    return fallback


def _storage_size_human(size: object) -> str:
    try:
        num = int(size)
    except (TypeError, ValueError):
        return str(size or "?")
    gb = num / 1_000_000_000
    if gb >= 1000:
        tb = gb / 1000
        return f"{tb:.1f}TB" if abs(tb - round(tb)) >= 0.05 else f"{round(tb)}TB"
    return f"{round(gb)}GB" if gb >= 10 else f"{gb:.1f}GB"


def probe_storage() -> dict:
    """Storage probe: enumerate every block device (NVMe + SATA).
    The actual QC gate is a quick read-throughput test via hdparm.
    """
    drives = []
    for dev in _candidate_storage_disks():
        path = dev.get("path") or f"/dev/{dev.get('name', '')}"
        tran = dev.get("tran") or "?"
        drives.append(f"{path} ({_storage_size_human(dev.get('size'))}, {tran})")
    return {
        "applicable": bool(drives),
        "evidence": " | ".join(drives) or "no block devices detected",
    }


PROBES = {
    "display":     probe_display,
    "keyboard":    probe_keyboard,
    "camera":      probe_camera,
    "fingerprint": probe_fingerprint,
    "speaker":     probe_speaker,
    "microphone":  probe_microphone,
    "audio_jack":  probe_audio_jack,
    "wireless":    probe_wireless,
    "touchscreen": probe_touchscreen,
    "ports":       probe_ports,
    "power_adapter": probe_power_adapter,
    "storage":     probe_storage,
}


def applicable_tests_for(layer: str) -> tuple[str, ...]:
    """Return QC tests that should be shown for this layer and hardware."""
    keys: list[str] = []
    for key in tests_for(layer):
        if key in HARDWARE_OPTIONAL_TESTS:
            probe = PROBES.get(key)
            if probe and not probe().get("applicable"):
                continue
        keys.append(key)
    return tuple(keys)


# ---------------------------------------------------------------------------
# Result-struct factories
# ---------------------------------------------------------------------------
def make_result(key: str, *, applicable: bool, evidence: str = "",
                ran: bool = False, result: str = "NA",
                remarks: str = "") -> dict:
    """Build a uniform per-test result row."""
    return {
        "key": key,
        "label": TEST_LABELS[key],
        "applicable": applicable,
        "ran": ran,
        "result": result,        # PASS | FAIL | SKIP | NA
        "remarks": remarks,
        "evidence": evidence,
    }


def na_result(key: str, evidence: str) -> dict:
    """Used when a probe says hardware not present — auto-skip."""
    return make_result(key, applicable=False, evidence=evidence,
                       ran=False, result="NA",
                       remarks="auto-skip: hardware not detected")


# ---------------------------------------------------------------------------
# Audio test helpers (used by the TUI screens directly)
# ---------------------------------------------------------------------------
def _alsa_card_indices() -> list[int]:
    raw = _read_sysfs_text("/proc/asound/cards")
    cards: list[int] = []
    for line in raw.splitlines():
        match = re.match(r"\s*(\d+)\s+\[", line)
        if match:
            cards.append(int(match.group(1)))
    return cards or [0]


def _amixer(card: int | None, args: list[str], timeout: int = 2, quiet: bool = True) -> str:
    cmd = ["amixer"]
    if quiet:
        cmd.append("-q")
    if card is not None:
        cmd.extend(["-c", str(card)])
    cmd.extend(args)
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        return ((out.stdout or "") + (out.stderr or "")).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def _pulse_prepare_audio(playback_percent: int = 100, capture_percent: int = 100) -> None:
    """Raise Pulse/PipeWire defaults when available; harmless on ALSA-only boots."""
    commands = (
        ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"],
        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{playback_percent}%"],
        ["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "0"],
        ["pactl", "set-source-volume", "@DEFAULT_SOURCE@", f"{capture_percent}%"],
    )
    for cmd in commands:
        try:
            subprocess.run(cmd, capture_output=True, timeout=2, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return


def _alsa_control_is_headphone_candidate(name: str) -> bool:
    lower = (name or "").lower()
    if any(term in lower for term in ("mic", "capture", "input", "boost")):
        return False
    return any(
        term in lower
        for term in ("headphone", "headset", "hp", "line out", "line-out", "speaker+lo", "headphone+lo")
    )


def _pulse_set_headphone_route() -> list[str]:
    """Ask PipeWire/Pulse to route output through the headphone port."""
    evidence: list[str] = []
    default_result = _run(
        ["pactl", "set-sink-port", "@DEFAULT_SINK@", "analog-output-headphones"],
        timeout=3,
    )
    evidence.append(
        "pactl:@DEFAULT_SINK@=headphones"
        + (f" ({default_result[:60]})" if default_result else "")
    )
    sinks = _run(["pactl", "list", "short", "sinks"], timeout=4)
    for line in sinks.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        sink = fields[1]
        result = _run(
            ["pactl", "set-sink-port", sink, "analog-output-headphones"],
            timeout=3,
        )
        evidence.append(
            f"pactl:{sink}=headphones" + (f" ({result[:60]})" if result else "")
        )
    return evidence


def prepare_audio_jack_output() -> str:
    """Route generated test audio to headphones and mute laptop speakers."""
    _alsa_prepare_audio(100, 70)
    evidence: list[str] = []
    for card in _alsa_card_indices():
        controls = _alsa_simple_controls(card)
        headphone_controls = list(dict.fromkeys(
            ["Headphone", "Headphone Playback Switch", "Headphone+LO", "Headset", "Line Out"]
            + [name for name in controls if _alsa_control_is_headphone_candidate(name)]
        ))
        for control in ("Auto-Mute Mode", "Independent HP"):
            output = _amixer(card, ["sset", control, "Disabled"], quiet=False)
            if output and "unable to find simple control" not in output.lower():
                evidence.append(f"card{card}:{control}=disabled")
        for control in ("Master", "PCM", "Front"):
            output = _amixer(card, ["sset", control, "100%", "unmute"], quiet=False)
            if output and "unable to find simple control" not in output.lower():
                evidence.append(f"card{card}:{control}=unmuted")
        for control in headphone_controls:
            for args in (
                ["sset", control, "100%", "unmute"],
                ["sset", control, "100%,100%", "unmute"],
                ["sset", control, "on"],
            ):
                output = _amixer(card, args, quiet=False)
                if output and "unable to find simple control" not in output.lower():
                    evidence.append(f"card{card}:{control}=headphone-on")
                    break
        speaker_output = _amixer(card, ["sset", "Speaker", "mute"], quiet=False)
        if speaker_output and "unable to find simple control" not in speaker_output.lower():
            evidence.append(f"card{card}:Speaker=muted")
    evidence.extend(_pulse_set_headphone_route())
    return " | ".join(dict.fromkeys(evidence))[:300] or "headphone route requested via ALSA/Pulse"


def _play_generated_sine_test(
    channels: int = 2,
    loops: int = 3,
    frequency: int = 440,
    device_args: list[str] | None = None,
) -> bool:
    """Use speaker-test's built-in sine generator, not sample WAV files."""
    try:
        rc = subprocess.run(
            [
                "speaker-test", *(device_args or []), "-c", str(channels),
                "-t", "sine", "-f", str(frequency), "-l", str(loops),
            ],
            capture_output=True,
            text=True,
            timeout=max(12, loops * 5),
            check=False,
        )
        return rc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def play_audio_jack_sample() -> bool:
    """Play the same Front Left/Front Right spoken labels through headphones."""
    ensure_audio_ready(timeout_sec=4)
    prepare_audio_jack_output()
    sample_path = "/tmp/vstl_audio_jack_test.wav"
    headphone_devices = _headphone_playback_device_args()
    if not headphone_devices:
        return False
    try:
        if _write_amplified_spoken_speaker_labels(sample_path):
            if _play_audio_file(sample_path, timeout=10, device_args_list=headphone_devices):
                return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    finally:
        try:
            os.unlink(sample_path)
        except OSError:
            pass

    return False


def speaker_playback_percent() -> int:
    """Volume target for the speaker-only QC test."""
    try:
        requested = int(os.environ.get("VSTL_SPEAKER_VOLUME", "250") or "250")
    except ValueError:
        requested = 250
    return min(300, max(100, requested))


def speaker_sample_gain() -> float:
    """Software preamp for the speaker-only QC spoken-label sample."""
    env_value = (
        os.environ.get("VSTL_SPEAKER_LABEL_GAIN")
        or os.environ.get("VSTL_SPEAKER_SAMPLE_GAIN")
        or "4.0"
    )
    try:
        requested = float(env_value)
    except ValueError:
        requested = 4.0
    return min(8.0, max(1.0, requested))


def _parse_amixer_scontrols(text: str) -> list[str]:
    controls: list[str] = []
    for line in (text or "").splitlines():
        match = re.search(r"Simple mixer control '([^']+)'", line)
        if match:
            controls.append(match.group(1))
    return list(dict.fromkeys(controls))


def _alsa_simple_controls(card: int | None) -> list[str]:
    return _parse_amixer_scontrols(_amixer(card, ["scontrols"], timeout=3, quiet=False))


def _alsa_control_is_playback_candidate(name: str) -> bool:
    lower = (name or "").lower()
    if any(term in lower for term in ("mic", "capture", "input", "boost")):
        return False
    return any(
        term in lower
        for term in ("master", "speaker", "headphone", "pcm", "front", "line out", "dac", "digital")
    )


def _alsa_control_is_capture_candidate(name: str) -> bool:
    lower = (name or "").lower()
    return any(
        term in lower
        for term in ("capture", "mic", "internal mic", "boost", "record", "gain", "adc")
    )


def _alsa_set_playback_volume(card: int | None, control: str, percent: int) -> None:
    for args in (
        ["sset", control, f"{percent}%", "unmute"],
        ["sset", control, f"{percent}%,{percent}%", "unmute"],
        ["sset", control, "on"],
    ):
        _amixer(card, args)


def _alsa_set_capture_volume(card: int | None, control: str, percent: int) -> None:
    for args in (
        ["sset", control, f"{percent}%", "cap"],
        ["sset", control, f"{percent}%,{percent}%", "cap"],
        ["sset", control, f"{percent}%", "unmute"],
        ["sset", control, f"{percent}%,{percent}%", "unmute"],
        ["sset", control, "cap"],
        ["sset", control, "on"],
    ):
        _amixer(card, args)


def _parse_amixer_enum_items(text: str) -> list[str]:
    """Parse enum choices printed by ``amixer sget`` on old HDA codecs."""
    items: list[str] = []
    for line in (text or "").splitlines():
        if "Items:" in line:
            items.extend(re.findall(r"'([^']+)'", line.split("Items:", 1)[1]))
        match = re.search(r"Item\s+#\d+\s+'([^']+)'", line)
        if match:
            items.append(match.group(1))
    return list(dict.fromkeys(items))


def _rank_capture_source(name: str) -> tuple[int, str]:
    lower = (name or "").strip().lower()
    if any(term in lower for term in ("internal", "built-in", "builtin", "mic array")):
        return (0, lower)
    if lower in ("dmic", "digital mic") or "digital microphone" in lower:
        return (5, lower)
    if any(term in lower for term in ("dock", "headset", "external", "rear", "front", "line")):
        return (80, lower)
    if "mic" in lower:
        return (30, lower)
    return (60, lower)


def _alsa_select_internal_capture_source(card: int | None, control: str) -> str:
    """Select one internal microphone enum without ending on a dock/jack input."""
    details = _amixer(card, ["sget", control], timeout=3, quiet=False)
    choices = _parse_amixer_enum_items(details)
    if choices:
        selected = min(choices, key=_rank_capture_source)
        _amixer(card, ["sset", control, selected])
        return selected

    # Do not write guessed enum values to a missing/non-enum control. Some
    # legacy codecs accept an unexpected value by changing another jack path.
    return ""


def _capture_source_profiles() -> list[tuple[int | None, str, str]]:
    """Return ALSA source settings worth trying on legacy HDA codecs."""
    profiles: list[tuple[int | None, str, str]] = []
    seen = set()
    source_controls = (
        "Input Source", "Capture Source", "Input Select", "Input Mux",
        "Digital Input Source",
    )
    for card in [None] + _alsa_card_indices():
        for control in source_controls:
            details = _amixer(card, ["sget", control], timeout=3, quiet=False)
            # Do not invent profiles for controls the codec does not expose.
            # A failed sset leaves the previous (possibly dock/headset) source
            # active while the capture command still succeeds with noise.
            choices = _parse_amixer_enum_items(details)
            if not choices:
                continue
            ordered = sorted(choices, key=_rank_capture_source)
            for source in ordered:
                key = (card, control, source)
                if key in seen:
                    continue
                seen.add(key)
                profiles.append(key)
    return profiles


def _alsa_apply_capture_source_profile(profile: tuple[int | None, str, str] | None) -> None:
    if not profile:
        return
    card, control, source = profile
    _amixer(card, ["sset", control, source])


_ALSA_INITIALIZED = False


def _capture_control_percent(control: str, requested: int) -> int | None:
    """Return a conservative level for one capture control.

    Old Dell Realtek codecs distort badly when Capture and Mic Boost are both
    forced to 100 percent. External jack controls are left untouched; the
    internal source mux is selected separately.
    """
    lower = (control or "").lower()
    if any(term in lower for term in ("dock", "headset", "front mic", "rear mic")):
        return None
    if "boost" in lower:
        return 0 if requested <= 40 else min(max(0, requested), 20)
    if any(term in lower for term in ("capture", "record", "input gain", "adc")):
        return min(max(0, requested), _microphone_capture_percent())
    if any(term in lower for term in ("internal mic", "digital mic", "dmic", "microphone", "mic")):
        return min(max(0, requested), _microphone_capture_percent())
    return min(max(0, requested), _microphone_capture_percent())


def _alsa_prepare_audio(playback_percent: int = 100, capture_percent: int = 100) -> None:
    """Force the real ALSA mixer paths high enough for speaker and mic QC.

    Some laptops expose volume through controls other than Master/Speaker
    (Front, PCM, DAC, Headphone, Mic Boost). We touch default plus every
    enumerated card, ignore failures, and let unsupported mixer settings fall
    through harmlessly.
    """
    global _ALSA_INITIALIZED
    _pulse_prepare_audio(playback_percent, min(capture_percent, 80))

    if not _ALSA_INITIALIZED:
        try:
            subprocess.run(["alsactl", "init"], capture_output=True, timeout=5, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        _ALSA_INITIALIZED = True

    card_targets: list[int | None] = [None] + _alsa_card_indices()
    for card in card_targets:
        controls = _alsa_simple_controls(card)
        playback_controls = list(dict.fromkeys(
            ["Master", "Speaker", "Headphone", "PCM", "Front", "Line Out", "DAC", "Digital"]
            + [name for name in controls if _alsa_control_is_playback_candidate(name)]
        ))
        capture_controls = list(dict.fromkeys(
            [
                "Capture", "Capture Volume", "Mic", "Internal Mic", "Internal Mic 1",
                "Internal Mic 2", "Digital Mic", "DMIC",
                "Mic Boost", "Internal Mic Boost", "Record Gain", "Input Gain",
                "ADC", "ADC Boost",
            ]
            + [name for name in controls if _alsa_control_is_capture_candidate(name)]
        ))

        for control in playback_controls:
            _alsa_set_playback_volume(card, control, playback_percent)
        for control in capture_controls:
            level = _capture_control_percent(control, capture_percent)
            if level is not None:
                _alsa_set_capture_volume(card, control, level)

        for source_control in (
            "Input Source", "Capture Source", "Input Select", "Input Mux",
            "Digital Input Source",
        ):
            _alsa_select_internal_capture_source(card, source_control)
        # Do not force headset/jack modes here. On older Dell Realtek codecs
        # those controls can replace the built-in microphone with an empty
        # analog jack that still returns a successful, noisy capture stream.
        _amixer(card, ["sset", "Auto-Mute Mode", "Disabled"])
        _amixer(card, ["sset", "Loopback Mixing", "Disabled"])

    _pulse_prepare_audio(playback_percent, min(capture_percent, 80))


def _speaker_label_sample_paths() -> list[tuple[str, str]]:
    """Return ALSA spoken-label WAV samples in left/right playback order."""
    bundled = os.path.join(os.path.dirname(__file__), "sounds", "alsa")
    directories = [
        bundled,
        "/opt/vstl/sounds/alsa",
        os.environ.get("VSTL_ALSA_SOUNDS_DIR", ""),
        "/usr/share/sounds/alsa",
        "/usr/local/share/sounds/alsa",
    ]
    names = (("Front_Left.wav", "left"), ("Front_Right.wav", "right"))
    samples: list[tuple[str, str]] = []
    for directory in directories:
        if not directory:
            continue
        for filename, channel in names:
            path = os.path.join(directory, filename)
            if os.path.exists(path):
                samples.append((path, channel))
        if samples:
            break
    return samples


def _read_pcm16_mono_wav(path: str) -> tuple[int, list[int]] | None:
    import array
    import wave

    try:
        with wave.open(path, "rb") as wav:
            if wav.getsampwidth() != 2:
                return None
            channels = max(1, wav.getnchannels())
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
        if not frames:
            return None
        samples = array.array("h")
        samples.frombytes(frames)
        if channels == 1:
            return rate, [int(sample) for sample in samples]
        mono = []
        for index in range(0, len(samples), channels):
            chunk = samples[index:index + channels]
            mono.append(int(sum(chunk) / max(1, len(chunk))))
        return rate, mono
    except (OSError, EOFError, wave.Error):
        return None


def _write_amplified_spoken_speaker_labels(path: str) -> bool:
    """Build a loud WAV from ALSA's spoken Front Left/Right label samples."""
    import array
    import math
    import wave

    labels = _speaker_label_sample_paths()
    if not labels:
        return False

    output = array.array("h")
    output_rate = 0
    gain = speaker_sample_gain()

    def append_pause(rate: int, seconds: float = 0.20) -> None:
        output.extend([0, 0] * max(1, int(rate * seconds)))

    def amplified(sample: int) -> int:
        # Soft-limit instead of hard clipping so the word labels stay clear.
        value = math.tanh((float(sample) / 32768.0) * gain) * 32767.0
        return max(-32768, min(32767, int(round(value))))

    for sample_path, channel in labels:
        loaded = _read_pcm16_mono_wav(sample_path)
        if not loaded:
            continue
        rate, mono = loaded
        if output_rate and rate != output_rate:
            continue
        output_rate = rate
        append_pause(output_rate)
        for sample in mono:
            value = amplified(sample)
            if channel == "left":
                output.extend([value, 0])
            else:
                output.extend([0, value])
        append_pause(output_rate, 0.25)

    if not output or not output_rate:
        return False

    try:
        with wave.open(path, "wb") as wav:
            wav.setnchannels(2)
            wav.setsampwidth(2)
            wav.setframerate(output_rate)
            wav.writeframes(output.tobytes())
        return True
    except (OSError, wave.Error):
        return False


def _empty_audio_activity() -> dict[str, float]:
    return {
        "peak": 0.0,
        "rms": 0.0,
        "envelope": 0.0,
        "score": 0.0,
        "quality": 0.0,
        "clipping": 0.0,
        "zcr": 0.0,
        "crest": 0.0,
        "channel": 0.0,
        "channels": 0.0,
    }


def _high_pass_pcm16(values, rate: int) -> list[float]:
    """Remove DC and sub-80 Hz rumble without touching normal speech."""
    import math

    if not values:
        return []
    mean = sum(values) / len(values)
    alpha = math.exp(-2.0 * math.pi * 80.0 / max(1, rate))
    previous_input = float(values[0] - mean)
    previous_output = 0.0
    filtered: list[float] = []
    for sample in values:
        current_input = float(sample - mean)
        current_output = alpha * (previous_output + current_input - previous_input)
        filtered.append(current_output)
        previous_input = current_input
        previous_output = current_output
    return filtered


def _pcm16_channel_activity(
    values,
    rate: int,
    channel: int,
    channels: int,
) -> dict[str, float]:
    import math

    filtered = _high_pass_pcm16(values, rate)
    if not filtered:
        return _empty_audio_activity()
    peak = max(abs(sample) for sample in filtered)
    rms = math.sqrt(sum(sample * sample for sample in filtered) / len(filtered))
    clipping = sum(1 for sample in values if abs(sample) >= 32700) / len(values)

    deadband = max(4.0, rms * 0.03)
    previous_sign = 0
    crossings = 0
    active = 0
    for sample in filtered:
        sign = 1 if sample > deadband else (-1 if sample < -deadband else 0)
        if not sign:
            continue
        active += 1
        if previous_sign and sign != previous_sign:
            crossings += 1
        previous_sign = sign
    zcr = crossings / max(1, active - 1)

    window = max(1, int(rate * 0.05))
    window_rms = []
    for start in range(0, len(filtered), window):
        chunk = filtered[start:start + window]
        if chunk:
            window_rms.append(math.sqrt(sum(sample * sample for sample in chunk) / len(chunk)))
    envelope = 0.0
    if len(window_rms) > 1:
        mean_window = sum(window_rms) / len(window_rms)
        spread = math.sqrt(
            sum((value - mean_window) ** 2 for value in window_rms) / len(window_rms)
        )
        envelope = spread / max(1.0, mean_window)

    crest = peak / max(1.0, rms)
    quality = rms * (1.0 + min(2.0, envelope * 2.0))
    quality *= max(0.45, min(2.0, crest / 3.0))
    if zcr > 0.35:
        quality *= max(0.08, 1.0 - ((zcr - 0.35) * 2.4))
    if clipping > 0.002:
        quality *= max(0.03, 1.0 - (clipping * 30.0))
    if zcr < 0.002 and envelope < 0.03:
        quality *= 0.15
    score = max(peak, quality * 10.0)
    return {
        "peak": float(round(peak, 2)),
        "rms": float(round(rms, 2)),
        "envelope": float(round(envelope, 3)),
        "score": float(round(score, 2)),
        "quality": float(round(quality, 2)),
        "clipping": float(round(clipping, 6)),
        "zcr": float(round(zcr, 4)),
        "crest": float(round(crest, 3)),
        "channel": float(channel),
        "channels": float(channels),
    }


def _pcm16_wav_activity(path: str) -> dict[str, float]:
    import array
    import wave

    try:
        with wave.open(path, "rb") as wav:
            if wav.getsampwidth() != 2:
                return _empty_audio_activity()
            channels = max(1, wav.getnchannels())
            rate = max(1, wav.getframerate())
            frames = wav.readframes(wav.getnframes())
        if not frames:
            return _empty_audio_activity()
        samples = array.array("h")
        samples.frombytes(frames)
        per_channel = [
            _pcm16_channel_activity(samples[channel::channels], rate, channel, channels)
            for channel in range(channels)
        ]
        # Old HDA mic arrays often expose one useful channel and one static or
        # unused channel. Prefer speech-like modulation over raw loudness.
        return max(
            per_channel,
            key=lambda stats: (
                stats.get("quality", 0.0),
                -stats.get("clipping", 0.0),
                -stats.get("zcr", 0.0),
            ),
        )
    except (OSError, EOFError, wave.Error):
        return _empty_audio_activity()


def _normalize_pcm16_wav(path: str, boosted_path: str) -> str:
    """Create clean mono playback without turning analog hiss into loud noise."""
    import array
    import wave

    try:
        stats = _pcm16_wav_activity(path)
        with wave.open(path, "rb") as wav:
            channels = max(1, wav.getnchannels())
            sample_width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
        if sample_width != 2 or not frames:
            return path
        samples = array.array("h")
        samples.frombytes(frames)
        channel = min(channels - 1, max(0, int(stats.get("channel", 0))))
        filtered = _high_pass_pcm16(samples[channel::channels], rate)
        peak = max((abs(sample) for sample in filtered), default=0.0)
        if peak < _MICROPHONE_MIN_ACCEPTABLE_PEAK:
            return path

        target_peak = 14000.0
        gain = min(8.0, target_peak / max(1.0, peak))
        if stats.get("clipping", 0.0) > 0.002:
            gain = min(gain, 1.0)
        output = array.array("h")
        for sample in filtered:
            value = int(round(sample * gain))
            output.append(max(-32768, min(32767, value)))
        with wave.open(boosted_path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(output.tobytes())
        return boosted_path
    except (OSError, EOFError, wave.Error):
        return path


def _pcm16_wav_peak(path: str) -> int:
    return int(round(_pcm16_wav_activity(path).get("peak", 0.0)))


def _mic_recording_activity(path: str) -> dict[str, float]:
    stats = _pcm16_wav_activity(path)
    if stats.get("peak", 0) or stats.get("rms", 0):
        return stats
    # Unit tests and older extension points may monkeypatch the simpler peak
    # helper. Preserve that compatibility while the live path uses full stats.
    peak = float(_pcm16_wav_peak(path))
    if peak <= 0:
        return stats
    return {
        "peak": peak,
        "rms": max(1.0, peak / 6.0),
        "envelope": 0.5,
        "score": peak,
    }


def _microphone_activity_present(stats: dict[str, float] | None) -> bool:
    """Return True when a capture contains any real PCM movement.

    Some old Dell Realtek/HDA paths accept ``arecord`` and produce a valid WAV
    full of zeros. Treat that as an unusable capture path so the QC flow keeps
    trying the next ALSA device/source instead of playing silence back.
    """
    if not stats:
        return False
    return (
        float(stats.get("peak", 0.0)) >= 1.0
        or float(stats.get("rms", 0.0)) >= 0.5
        or float(stats.get("score", 0.0)) >= 1.0
    )


def _microphone_baseline_too_noisy(stats: dict[str, float] | None) -> bool:
    """Detect a quiet sample that is already dominated by capture noise."""
    if not stats:
        return False
    peak = float(stats.get("peak", 0.0))
    rms = float(stats.get("rms", 0.0))
    clipping = float(stats.get("clipping", 0.0))
    zcr = float(stats.get("zcr", 0.0))
    if peak >= 12000.0 or rms >= 3000.0:
        return True
    if clipping > 0.002:
        return True
    return rms >= 1200.0 and zcr >= 0.32


def _play_spoken_channel_test(
    channels: int,
    loops: int = 1,
    device_args: list[str] | None = None,
) -> bool:
    """Use ALSA's spoken samples: Front Left, Front Right, Rear Left, Rear Right."""
    try:
        rc = subprocess.run(
            [
                "speaker-test", *(device_args or []), "-c", str(channels),
                "-t", "wav", "-l", str(loops),
            ],
            capture_output=True, text=True, timeout=max(12, channels * 4 * loops), check=False,
        )
        return rc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _play_audio_file(
    path: str,
    timeout: int = 8,
    device_args_list: list[list[str]] | None = None,
) -> bool:
    for device_args in (device_args_list or _playback_device_args())[:10]:
        try:
            rc = subprocess.run(
                ["aplay", "-q", *device_args, path],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if rc.returncode == 0:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return False


def play_test_tone(seconds: float = 2.5) -> bool:
    """Play loud spoken speaker labels after forcing output to maximum volume.

    Preferred output is ALSA's spoken channel samples. On four-channel devices
    this announces Front Left, Front Right, Rear Left, and Rear Right. If the
    device only supports stereo, the fallback announces Front Left/Right.
    """
    # Strong boost for the speaker-only QC test. Microphone playback uses its
    # own controlled normalization path.
    _alsa_prepare_audio(speaker_playback_percent(), 100)
    sample_path = "/tmp/vstl_speaker_test.wav"
    try:
        if _write_amplified_spoken_speaker_labels(sample_path):
            if _play_audio_file(sample_path, timeout=int(seconds + 8)):
                return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    finally:
        try:
            os.unlink(sample_path)
        except OSError:
            pass

    for device_args in _playback_device_args()[:8]:
        if _play_spoken_channel_test(channels=4, device_args=device_args):
            return True
        if _play_spoken_channel_test(channels=2, device_args=device_args):
            return True

    return False


_PREFERRED_CAPTURE_ARGS: list[str] | None = None
_PREFERRED_CAPTURE_PROFILE: tuple[int | None, str, str] | None = None
_PREFERRED_CAPTURE_FORMAT: tuple[int, int] | None = None
_MICROPHONE_AUDIBLE_PEAK = 800
_MICROPHONE_MIN_ACCEPTABLE_PEAK = 24
_MICROPHONE_ACTIVITY_BASELINE: dict[str, float] | None = None
_LAST_MIC_ACTIVITY_EVIDENCE = ""


def microphone_record_seconds() -> int:
    """Operator-facing microphone recording window, clamped to 3-5 seconds."""
    try:
        requested = int(os.environ.get("VSTL_MIC_RECORD_SECONDS", "4") or "4")
    except ValueError:
        requested = 5
    return min(5, max(3, requested))


def _microphone_capture_percent() -> int:
    """Default capture level: loud enough for Dell internal mics, not maxed."""
    try:
        requested = int(os.environ.get("VSTL_MIC_CAPTURE_LEVEL", "80") or "80")
    except ValueError:
        requested = 80
    return min(90, max(45, requested))


def _format_microphone_activity(label: str, stats: dict[str, float] | None) -> str:
    stats = stats or {}
    return (
        f"{label}:peak={int(stats.get('peak', 0))},"
        f"rms={stats.get('rms', 0):.1f},"
        f"env={stats.get('envelope', 0):.2f},"
        f"score={int(stats.get('score', 0))},"
        f"clip={stats.get('clipping', 0):.4f},"
        f"zcr={stats.get('zcr', 0):.3f},"
        f"channel={int(stats.get('channel', 0)) + 1}/"
        f"{max(1, int(stats.get('channels', 1)))}"
    )


def _microphone_voice_detected(
    recording: dict[str, float] | None,
    baseline: dict[str, float] | None,
) -> bool:
    if not recording or recording.get("peak", 0) < _MICROPHONE_MIN_ACCEPTABLE_PEAK:
        return False
    # A clipped waveform or near-white-noise crossing rate is not usable
    # voice, even when it is much louder than the quiet baseline.
    if recording.get("clipping", 0.0) > 0.02 or recording.get("zcr", 0.0) > 0.55:
        return False
    if not baseline:
        return recording.get("peak", 0) >= _MICROPHONE_MIN_ACCEPTABLE_PEAK

    base_peak = float(baseline.get("peak", 0))
    base_rms = float(baseline.get("rms", 0))
    base_env = float(baseline.get("envelope", 0))
    base_score = float(baseline.get("score", 0))
    rec_peak = float(recording.get("peak", 0))
    rec_rms = float(recording.get("rms", 0))
    rec_env = float(recording.get("envelope", 0))
    rec_score = float(recording.get("score", 0))

    rms_delta = rec_rms - base_rms
    peak_delta = rec_peak - base_peak
    score_delta = rec_score - base_score
    if rms_delta >= max(8.0, base_rms * 0.45) and peak_delta >= max(40.0, base_peak * 0.35):
        return True
    if score_delta >= max(120.0, base_score * 0.50) and rec_rms >= base_rms + 5.0:
        return True
    if rec_env >= base_env + 0.18 and rec_rms >= base_rms + 6.0:
        return True
    return False


def _capture_formats() -> list[tuple[int, int]]:
    """Formats ordered for legacy HDA mic arrays, then compatibility paths."""
    return [(2, 48000), (1, 48000), (2, 44100), (1, 44100)]


def _capture_command(
    device_args: list[str],
    channels: int,
    rate: int,
    seconds: int,
    path: str,
) -> list[str]:
    return [
        "arecord", "-q", *device_args, "-t", "wav", "-f", "S16_LE",
        "-r", str(rate), "-c", str(channels), "-d", str(seconds), path,
    ]


def _capture_device_label(device_args: list[str] | None) -> str:
    if not device_args:
        return "default"
    try:
        return device_args[device_args.index("-D") + 1]
    except (ValueError, IndexError):
        return " ".join(device_args)


def _capture_profile_label(profile: tuple[int | None, str, str] | None) -> str:
    if not profile:
        return "driver-default"
    card, control, source = profile
    return f"card={card if card is not None else 'default'},{control}={source}"


def _microphone_candidate_score(
    stats: dict[str, float],
    baseline: dict[str, float] | None,
) -> float:
    """Rank speech above loud static and clipped capture paths."""
    detected = _microphone_voice_detected(stats, baseline)
    quality = float(stats.get("quality", 0.0) or stats.get("score", 0.0))
    clipping_penalty = float(stats.get("clipping", 0.0)) * 2_000_000.0
    zcr = float(stats.get("zcr", 0.0))
    zcr_penalty = max(0.0, zcr - 0.32) * 150_000.0
    baseline_rms = float((baseline or {}).get("rms", 0.0))
    voice_rise = max(0.0, float(stats.get("rms", 0.0)) - baseline_rms)
    if detected:
        return 1_000_000.0 + (quality * 20.0) + (voice_rise * 50.0) - clipping_penalty - zcr_penalty
    return quality - clipping_penalty - zcr_penalty


def _select_capture_device_args() -> list[str]:
    """Select the first working, hardware-ranked internal capture path.

    Selection runs while the technician is silent. Choosing the loudest probe
    therefore selects hiss, not a better microphone, so signal level is not a
    selection criterion here.
    """
    global _PREFERRED_CAPTURE_ARGS, _PREFERRED_CAPTURE_PROFILE, _PREFERRED_CAPTURE_FORMAT
    candidates = _capture_device_args()[:8]
    source_profiles: list[tuple[int | None, str, str] | None] = []
    for profile in _capture_source_profiles()[:6] + [None]:
        if profile not in source_profiles:
            source_profiles.append(profile)
    probe_index = 0
    max_probe_attempts = int(os.environ.get("VSTL_MIC_PROBE_ATTEMPTS", "24") or "24")
    first_working: tuple[list[str], tuple[int | None, str, str] | None, tuple[int, int]] | None = None
    for channels, rate in _capture_formats():
        for profile in source_profiles:
            for device_args in candidates:
                if probe_index >= max_probe_attempts:
                    break
                sample_path = f"/tmp/vstl_mic_probe_{probe_index}.wav"
                probe_index += 1
                _alsa_apply_capture_source_profile(profile)
                try:
                    result = subprocess.run(
                        _capture_command(device_args, channels, rate, 1, sample_path),
                        capture_output=True, text=True, timeout=4, check=False,
                    )
                    if result.returncode == 0:
                        capture_format = (channels, rate)
                        if first_working is None:
                            first_working = (list(device_args), profile, capture_format)
                        stats = _mic_recording_activity(sample_path)
                        if _microphone_activity_present(stats) and not _microphone_baseline_too_noisy(stats):
                            _PREFERRED_CAPTURE_ARGS = list(device_args)
                            _PREFERRED_CAPTURE_PROFILE = profile
                            _PREFERRED_CAPTURE_FORMAT = capture_format
                            return list(device_args)
                except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                    pass
                finally:
                    try:
                        os.unlink(sample_path)
                    except OSError:
                        pass
            if probe_index >= max_probe_attempts:
                break
        if probe_index >= max_probe_attempts:
            break
    if first_working is not None:
        _PREFERRED_CAPTURE_ARGS, _PREFERRED_CAPTURE_PROFILE, _PREFERRED_CAPTURE_FORMAT = first_working
    else:
        _PREFERRED_CAPTURE_ARGS = list(candidates[0] if candidates else [])
        _PREFERRED_CAPTURE_PROFILE = None
        _PREFERRED_CAPTURE_FORMAT = _capture_formats()[0]
    return list(_PREFERRED_CAPTURE_ARGS)


def _prime_microphone_capture(sample_path: str = "/tmp/vstl_mic_prime.wav") -> None:
    """Open capture once before the visible timed recording window."""
    candidates = _capture_device_args()
    device_args = (
        list(_PREFERRED_CAPTURE_ARGS)
        if _PREFERRED_CAPTURE_ARGS is not None
        else (candidates[0] if candidates else [])
    )
    preferred_format = _PREFERRED_CAPTURE_FORMAT or _capture_formats()[0]
    warmup_formats = [preferred_format]
    if preferred_format != (1, 48000):
        warmup_formats.append((1, 48000))
    warmups = [
        (f"{channels}ch_{rate}", channels, rate)
        for channels, rate in warmup_formats
    ]
    attempted_paths = [sample_path]
    for suffix, channels, rate in warmups:
        path = sample_path.replace(".wav", f"_{suffix}.wav")
        attempted_paths.append(path)
        try:
            subprocess.run(
                _capture_command(device_args, channels, rate, 1, path),
                capture_output=True, text=True, timeout=3, check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    for path in attempted_paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def prepare_microphone_recording() -> None:
    """Prepare and warm the mic path before telling the technician to speak."""
    ensure_audio_ready(timeout_sec=4)
    _alsa_prepare_audio(100, _microphone_capture_percent())
    if _PREFERRED_CAPTURE_ARGS is None:
        _select_capture_device_args()
    _alsa_apply_capture_source_profile(_PREFERRED_CAPTURE_PROFILE)
    _prime_microphone_capture()


def calibrate_microphone_baseline(
    seconds: int = 1,
    sample_path: str = "/tmp/vstl_mic_quiet.wav",
    status_callback=None,
) -> dict[str, float]:
    """Record a short quiet baseline so hiss/static is not mistaken for voice."""
    global _MICROPHONE_ACTIVITY_BASELINE, _LAST_MIC_ACTIVITY_EVIDENCE
    _alsa_prepare_audio(100, _microphone_capture_percent())
    device_args = _select_capture_device_args()
    profile = _PREFERRED_CAPTURE_PROFILE
    channels, rate = _PREFERRED_CAPTURE_FORMAT or _capture_formats()[0]
    baseline: dict[str, float] | None = None
    baseline_mode = "normal"
    if callable(status_callback):
        status_callback("quiet baseline")
    _alsa_apply_capture_source_profile(profile)
    try:
        rc = subprocess.run(
            _capture_command(device_args, channels, rate, seconds, sample_path),
            capture_output=True,
            text=True,
            timeout=seconds + 5,
            check=False,
        )
        if rc.returncode == 0:
            baseline = _pcm16_wav_activity(sample_path)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        baseline = None
    try:
        os.unlink(sample_path)
    except OSError:
        pass

    if _microphone_baseline_too_noisy(baseline):
        baseline_mode = "low_gain"
        retry_path = sample_path.replace(".wav", "_low_gain.wav")
        if callable(status_callback):
            status_callback("quiet baseline low gain")
        _alsa_prepare_audio(100, 35)
        _alsa_apply_capture_source_profile(profile)
        try:
            rc = subprocess.run(
                _capture_command(device_args, channels, rate, seconds, retry_path),
                capture_output=True,
                text=True,
                timeout=seconds + 5,
                check=False,
            )
            if rc.returncode == 0:
                retry_baseline = _pcm16_wav_activity(retry_path)
                if retry_baseline:
                    baseline = retry_baseline
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        try:
            os.unlink(retry_path)
        except OSError:
            pass

    _MICROPHONE_ACTIVITY_BASELINE = baseline or {
        "peak": 0.0,
        "rms": 0.0,
        "envelope": 0.0,
        "score": 0.0,
    }
    _LAST_MIC_ACTIVITY_EVIDENCE = (
        f"capture_device={_capture_device_label(device_args)}; "
        f"capture_profile={_capture_profile_label(profile)}; "
        f"capture_format={channels}ch/{rate}Hz; voice_detected=pending; "
        f"baseline_gain={baseline_mode}; "
        + _format_microphone_activity("quiet", _MICROPHONE_ACTIVITY_BASELINE)
    )
    return dict(_MICROPHONE_ACTIVITY_BASELINE)


def microphone_activity_evidence() -> str:
    return _LAST_MIC_ACTIVITY_EVIDENCE


def record_then_playback(
    seconds: int = 5,
    sample_path: str = "/tmp/vstl_mic_test.wav",
    prepare: bool = True,
    status_callback=None,
) -> bool:
    """Capture speech, choose the cleanest path, then play normalized mono."""
    global _PREFERRED_CAPTURE_ARGS, _PREFERRED_CAPTURE_PROFILE
    global _PREFERRED_CAPTURE_FORMAT, _LAST_MIC_ACTIVITY_EVIDENCE
    if prepare:
        prepare_microphone_recording()
    boosted_path = sample_path.replace(".wav", "_boosted.wav")
    attempted_paths: list[str] = [sample_path, boosted_path]
    try:
        candidates: list[list[str]] = []
        seen = set()
        preferred = [_PREFERRED_CAPTURE_ARGS] if _PREFERRED_CAPTURE_ARGS is not None else []
        for args in preferred + _capture_device_args():
            if args is None:
                continue
            key = tuple(args)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(args)
        candidates = candidates[:8]
        source_profiles: list[tuple[int | None, str, str] | None] = []
        for profile in [_PREFERRED_CAPTURE_PROFILE] + _capture_source_profiles()[:6] + [None]:
            if profile in source_profiles:
                continue
            source_profiles.append(profile)

        best_path = ""
        best_rank = float("-inf")
        best_stats: dict[str, float] | None = None
        best_args: list[str] | None = None
        best_profile: tuple[int | None, str, str] | None = None
        best_format: tuple[int, int] | None = None
        record_index = 0
        max_record_attempts = int(os.environ.get("VSTL_MIC_RECORD_ATTEMPTS", "6") or "6")
        formats = []
        for capture_format in [_PREFERRED_CAPTURE_FORMAT] + _capture_formats():
            if capture_format and capture_format not in formats:
                formats.append(capture_format)

        combinations = []
        preferred_combo = (
            list(_PREFERRED_CAPTURE_ARGS),
            _PREFERRED_CAPTURE_PROFILE,
            _PREFERRED_CAPTURE_FORMAT,
        ) if _PREFERRED_CAPTURE_ARGS is not None and _PREFERRED_CAPTURE_FORMAT else None
        if preferred_combo:
            combinations.append(preferred_combo)
        for capture_format in formats:
            for profile in source_profiles:
                for device_args in candidates:
                    combo = (list(device_args), profile, capture_format)
                    if combo not in combinations:
                        combinations.append(combo)

        for device_args, profile, capture_format in combinations:
            if record_index >= max_record_attempts:
                break
            channels, rate = capture_format
            _alsa_apply_capture_source_profile(profile)
            path = sample_path if record_index == 0 else sample_path.replace(".wav", f"_{record_index}.wav")
            record_index += 1
            attempted_paths.append(path)
            if callable(status_callback):
                status_callback("recording")
            try:
                rec = subprocess.run(
                    _capture_command(device_args, channels, rate, seconds, path),
                    capture_output=True, text=True, timeout=seconds + 6, check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                continue
            if rec.returncode != 0:
                continue
            stats = _mic_recording_activity(path)
            if not _microphone_activity_present(stats):
                continue
            rank = _microphone_candidate_score(stats, _MICROPHONE_ACTIVITY_BASELINE)
            if rank > best_rank:
                best_path = path
                best_rank = rank
                best_stats = stats
                best_args = list(device_args)
                best_profile = profile
                best_format = capture_format
            if _microphone_voice_detected(stats, _MICROPHONE_ACTIVITY_BASELINE):
                if stats.get("clipping", 0.0) <= 0.002 and stats.get("zcr", 0.0) <= 0.35:
                    break

        if not best_path:
            _LAST_MIC_ACTIVITY_EVIDENCE = (
                "voice_detected=no; "
                + _format_microphone_activity("quiet", _MICROPHONE_ACTIVITY_BASELINE)
                + f"; recording=no active sample across {record_index} capture path(s); playback=not available"
            )
            if callable(status_callback):
                status_callback("playback failed")
            return False
        voice_detected = _microphone_voice_detected(best_stats, _MICROPHONE_ACTIVITY_BASELINE)
        best_channels, best_rate = best_format or _capture_formats()[0]
        _LAST_MIC_ACTIVITY_EVIDENCE = (
            f"capture_device={_capture_device_label(best_args)}; "
            f"capture_profile={_capture_profile_label(best_profile)}; "
            f"capture_format={best_channels}ch/{best_rate}Hz; "
            f"capture_attempts={record_index}; "
            f"voice_detected={'yes' if voice_detected else 'no'}; "
            + _format_microphone_activity("quiet", _MICROPHONE_ACTIVITY_BASELINE)
            + "; "
            + _format_microphone_activity("recording", best_stats)
        )
        if best_args is not None and best_stats and best_stats.get("peak", 0) >= _MICROPHONE_MIN_ACCEPTABLE_PEAK:
            _PREFERRED_CAPTURE_ARGS = best_args
            _PREFERRED_CAPTURE_PROFILE = best_profile
            _PREFERRED_CAPTURE_FORMAT = best_format

        if callable(status_callback):
            if not voice_detected:
                status_callback("no voice detected")
        if not voice_detected:
            _LAST_MIC_ACTIVITY_EVIDENCE += "; playback=skipped_no_voice"
            if callable(status_callback):
                status_callback("playback failed")
            return False
        if callable(status_callback):
            status_callback("playback")
        playback_path = _normalize_pcm16_wav(best_path, boosted_path)
        _alsa_prepare_audio(100, _microphone_capture_percent())
        playback_ok = _play_audio_file(playback_path, timeout=seconds + 6)
        _LAST_MIC_ACTIVITY_EVIDENCE += f"; playback={'ok' if playback_ok else 'failed'}"
        return playback_ok
    finally:
        for path in dict.fromkeys(attempted_paths):
            try:
                os.unlink(path)
            except OSError:
                pass


def storage_throughput_mb_per_sec(device: str = None) -> tuple[float, str]:
    """Quick read throughput smoke test via hdparm -t. Returns (mb_per_sec,
    evidence_str). Used by the Storage QC screen — operator sees the raw
    number and gates PASS/FAIL.
    """
    if device is None:
        disks = _candidate_storage_disks()
        if disks:
            device = disks[0].get("path") or f"/dev/{disks[0].get('name', '')}"
    if not device:
        return 0.0, "no block device found"
    try:
        rc = subprocess.run(
            ["hdparm", "-t", "--direct", device],
            capture_output=True, text=True, timeout=20, check=False,
        )
        text = (rc.stdout + rc.stderr)
        # Output line: " Timing O_DIRECT disk reads: 1234 MB in  3.00 seconds = 411.30 MB/sec"
        import re
        m = re.search(r"=\s*([\d\.]+)\s*MB/sec", text)
        if m:
            mb = float(m.group(1))
            return mb, f"{device} hdparm read: {mb:.0f} MB/s"
        return 0.0, f"{device} hdparm parse error: {text.splitlines()[-1][:80] if text else 'no output'}"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return 0.0, f"{device} hdparm error: {e}"


# ---------------------------------------------------------------------------
# Compose the full QC bundle (called by the TUI after every screen runs)
# ---------------------------------------------------------------------------
def summarize(results: list[dict]) -> dict:
    passed = [r for r in results if r["result"] == "PASS"]
    failed = [r for r in results if r["result"] == "FAIL"]
    skipped = [r for r in results if r["result"] == "SKIP"]
    na = [r for r in results if r["result"] == "NA"]

    return {
        "tests": {r["key"]: r for r in results},
        "passed": [r["key"] for r in passed],
        "failed": [r["key"] for r in failed],
        "skipped": [r["key"] for r in skipped],
        "na": [r["key"] for r in na],
        "all_passed": (len(failed) == 0 and len(passed) > 0),
        "summary": (
            f"{len(passed)} PASS, {len(failed)} FAIL"
            + (f", {len(skipped)} SKIP" if skipped else "")
            + (f", {len(na)} NA" if na else "")
        ),
    }
