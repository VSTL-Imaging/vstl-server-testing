#!/usr/bin/env python3
"""
vstl-imaging-tui.py — VSTL 360 Bench TUI (Phase 1)
====================================================
Curses-based terminal UI that runs on the bench laptop after PXE-booting
into the VSTL Live ISO (Clonezilla-derived). Replaces the earlier
fully-automatic shell client with an interactive, technician-driven flow.

Phase 1 scope (per spec sections 1 + 3.1-3.7):
  1. Technician selection (L1 / L2) — persists for the whole session.
  2. Main menu with 5-second auto-select → Option 1 (Restore Approved
     System Image). ENTER confirms whatever is highlighted.
  3. Hardware-detect screens (3-second auto-advance, ENTER skips):
       Model → SKU → CPU → GPU → RAM → Storage Health → Battery Health
  4. POST the payload to /api/imaging/ingest, then show a completion
     screen with serial-no echo. Erase / Image / QC interactive tests
     ship in Phases 2-4.

Failure / fallback policy:
  * Hardware detection wraps every shelling-out call so a missing tool
    never crashes the UI — it just shows "UNKNOWN".
  * If /opt/vstl/config.env is missing the API base / key, the run still
    completes the on-screen flow and writes a local audit file so the
    operator can recover later.

Written for Python 3.9+ (Clonezilla Live ships 3.11). No third-party
deps — `urllib`, `curses`, `subprocess`, `json` are all stdlib.
"""
from __future__ import annotations

import curses
import hashlib
import json
import math
import os
import re
import shutil
import ssl
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional


# ----------------------------------------------------------------------------
# 2026-05-11 Phase 2 v2 — Bench HTTP must NOT use the default urllib UA.
# Cloudflare's bot-fight mode blocks "Python-urllib/3.x" at the edge on
# emergent.host (HTTP 403, error code 1010). Every urllib.request.Request
# below sets this header.
# ----------------------------------------------------------------------------
BENCH_USER_AGENT = "VSTL-Bench/2.0 (Linux; PXE; +https://vstl360.local)"

_WIPE_METHOD_STANDARDS = {
    "NVMe_SANITIZE_BLOCK_ERASE": "NIST SP 800-88 Purge",
    "NVMe_SANITIZE_CRYPTO_ERASE": "NIST SP 800-88 Purge",
    "NVMe_SANITIZE_OVERWRITE": "NIST SP 800-88 Purge",
    "NVMe_FORMAT_CRYPTO": "NIST SP 800-88 Purge",
    "ATA_SANITIZE_BLOCK_ERASE": "NIST SP 800-88 Purge",
    "ATA_SANITIZE_CRYPTO_SCRAMBLE": "NIST SP 800-88 Purge",
    "ATA_SECURITY_ERASE_ENHANCED": "NIST SP 800-88 Purge",
    "ATA_SECURITY_ERASE": "NIST SP 800-88 Purge",
    "NWIPE_DOD_3PASS": "DoD 5220.22-M 3-pass",
}
_CLEAR_WIPE_METHOD_STANDARDS = {
    "NVMe_FORMAT_USER_DATA": "NIST SP 800-88 Clear",
    "NVMe_SECURE_DISCARD_CLEAR": "NIST SP 800-88 Clear",
    "NVMe_SOFTWARE_ZERO_CLEAR": "NIST SP 800-88 Clear",
    "BLKDISCARD": "NIST SP 800-88 Clear",
}

_UNSUPPORTED_WIPE_STANDARD = "Unsupported data sanitization method"
_UNSUPPORTED_WIPE_MESSAGE = (
    "Clear-class and unknown wipe methods are disabled. "
    "Only approved purge-class methods can issue a certificate or authorize capture, "
    "except temporary model-specific Clear-only exceptions."
)

TESTING_MODE_CTRL_T = 20
TESTING_MODE_HOTKEY_LABEL = "Ctrl+Shift+Alt+T"
TESTING_MODE_SENTINEL = "__VSTL_TESTING_MODE__"
TESTING_RESTORE_ONLY_CHOICE = 4
TESTING_QC_ONLY_CHOICE = 5
TESTING_SECURE_ERASE_CHOICE = 6
TESTING_RESTORE_ONLY_LABEL = "Restore Only OS"
TESTING_QC_ONLY_LABEL = "QC"
TESTING_SECURE_ERASE_LABEL = "Secure Erase"
TESTING_END_RESTART = "restart"
TESTING_END_MAIN_MENU = "main_menu"
TESTING_END_LOGIN = "login"


# ----------------------------------------------------------------------------
# Full-screen color fill via /dev/fb0 (used by the Display QC test).
# Curses can only colour text cells — to actually paint the panel for
# dead-pixel inspection we have to write raw pixels to the framebuffer.
# ----------------------------------------------------------------------------
def _fb_pixel(rgb: tuple[int, int, int], bpp: int) -> bytes:
    """Return one framebuffer pixel in the active console format."""
    r, g, b = rgb
    if bpp == 32:
        return bytes((b, g, r, 255))
    if bpp == 24:
        return bytes((b, g, r))
    if bpp == 16:
        value = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        return value.to_bytes(2, "little")
    return b""


def _fb0_geometry() -> Optional[tuple[int, int, int, int]]:
    """Return (width, height, bpp, stride) for /dev/fb0, or None."""
    try:
        with open("/sys/class/graphics/fb0/virtual_size") as f:
            w, h = (int(x) for x in f.read().strip().split(","))
        with open("/sys/class/graphics/fb0/bits_per_pixel") as f:
            bpp = int(f.read().strip())
        stride = 0
        for stride_path in (
            "/sys/class/graphics/fb0/stride",
            "/sys/class/graphics/fb0/line_length",
        ):
            try:
                with open(stride_path) as f:
                    stride = int(f.read().strip())
                if stride > 0:
                    break
            except (FileNotFoundError, ValueError, OSError):
                continue
        pixel = _fb_pixel((0, 0, 0), bpp)
        if not pixel:
            return None
        stride = max(stride, w * len(pixel))
        return w, h, bpp, stride
    except (FileNotFoundError, ValueError, OSError):
        return None


def _fb0_fill(color_name: str) -> bool:
    """Fill /dev/fb0 with a solid color. Returns True on success."""
    try:
        info = _fb0_geometry()
    except OSError:
        info = None
    if not info:
        return False
    w, h, bpp, stride = info
    palette = {
        "RED":   (255, 0, 0),
        "GREEN": (0, 255, 0),
        "BLUE":  (0, 0, 255),
        "WHITE": (255, 255, 255),
        "BLACK": (0, 0, 0),
    }
    pixel = _fb_pixel(palette.get(color_name.upper(), palette["BLACK"]), bpp)
    if not pixel:
        return False
    row = pixel * w
    if len(row) < stride:
        row += b"\x00" * (stride - len(row))
    elif len(row) > stride:
        row = row[:stride]
    try:
        with open("/dev/fb0", "wb") as f:
            for _ in range(h):
                f.write(row)
        return True
    except (PermissionError, OSError):
        return False


def _clamp8(value: int) -> int:
    return 0 if value < 0 else 255 if value > 255 else value


def _append_yuv_as_bgra(row: bytearray, y: int, u: int, v: int, repeat: int) -> None:
    c = max(0, y - 16)
    d = u - 128
    e = v - 128
    r = _clamp8((298 * c + 409 * e + 128) >> 8)
    g = _clamp8((298 * c - 100 * d - 208 * e + 128) >> 8)
    b = _clamp8((298 * c + 516 * d + 128) >> 8)
    row.extend(bytes((b, g, r, 255)) * repeat)


def _fb0_blit_yuyv(raw: bytes, src_w: int, src_h: int, mirror: bool = False) -> bool:
    """Render a YUYV frame centered on /dev/fb0. No external player needed."""
    info = _fb0_geometry()
    if not info:
        return False
    fb_w, fb_h, bpp, stride = info
    if bpp != 32 or len(raw) < src_w * src_h * 2:
        return False

    scale = max(1, min(fb_w // src_w, fb_h // src_h))
    dst_w = src_w * scale
    dst_h = src_h * scale
    x0 = max(0, (fb_w - dst_w) // 2)
    y0 = max(0, (fb_h - dst_h) // 2)
    black_px = b"\x00\x00\x00\xff"
    blank_row = black_px * fb_w

    try:
        with open("/dev/fb0", "r+b", buffering=0) as fb:
            fb.seek(0)
            for dst_y in range(fb_h):
                if dst_y < y0 or dst_y >= y0 + dst_h:
                    row = bytearray(blank_row)
                else:
                    src_y = (dst_y - y0) // scale
                    src_off = src_y * src_w * 2
                    row = bytearray(black_px * x0)
                    src_pairs = range(src_w - 2, -1, -2) if mirror else range(0, src_w, 2)
                    for src_x in src_pairs:
                        off = src_off + src_x * 2
                        y1, u, y2, v = raw[off], raw[off + 1], raw[off + 2], raw[off + 3]
                        if mirror:
                            _append_yuv_as_bgra(row, y2, u, v, scale)
                            _append_yuv_as_bgra(row, y1, u, v, scale)
                        else:
                            _append_yuv_as_bgra(row, y1, u, v, scale)
                            _append_yuv_as_bgra(row, y2, u, v, scale)
                    right = fb_w - x0 - dst_w
                    if right > 0:
                        row.extend(black_px * right)
                if len(row) < stride:
                    row.extend(b"\x00" * (stride - len(row)))
                fb.write(row[:stride])
        return True
    except (OSError, PermissionError):
        return False


def _fb0_blit_bgra(raw: bytes, width: int, height: int) -> bool:
    """Write an FFmpeg-produced full-screen BGRA frame to /dev/fb0."""
    info = _fb0_geometry()
    if not info:
        return False
    fb_w, fb_h, bpp, stride = info
    row_bytes = width * 4
    if (
        bpp != 32
        or width != fb_w
        or height != fb_h
        or stride < row_bytes
        or len(raw) < row_bytes * height
    ):
        return False
    try:
        with open("/dev/fb0", "r+b", buffering=0) as fb:
            if stride == row_bytes:
                fb.write(raw[: row_bytes * height])
            else:
                padding = b"\x00" * max(0, stride - row_bytes)
                for y in range(height):
                    start = y * row_bytes
                    fb.write(raw[start:start + row_bytes])
                    if padding:
                        fb.write(padding)
        return True
    except (OSError, PermissionError):
        return False


def _fb0_draw_touch_grid(filled: set[tuple[int, int]], cols: int, rows: int) -> bool:
    """Draw a full-screen blue/green touch coverage grid on /dev/fb0."""
    info = _fb0_geometry()
    if not info:
        return False
    fb_w, fb_h, bpp, stride = info
    blue = _fb_pixel((42, 99, 255), bpp)
    green = _fb_pixel((35, 205, 95), bpp)
    gap_px = _fb_pixel((235, 239, 245), bpp)
    if not blue or not green or not gap_px:
        return False
    pixel_width = len(blue)
    if stride < fb_w * pixel_width:
        return False

    cell_w = max(1, fb_w // max(1, cols))
    cell_h = max(1, fb_h // max(1, rows))
    # One or two pixels is enough to distinguish blocks. Wide separators look
    # like display defects on high-resolution panels.
    gap = max(1, min(2, min(cell_w, cell_h) // 24))
    x_edges = [i * fb_w // cols for i in range(cols + 1)]
    y_edges = [i * fb_h // rows for i in range(rows + 1)]

    try:
        with open("/dev/fb0", "r+b", buffering=0) as fb:
            fb.seek(0)
            for y in range(fb_h):
                r = min(rows - 1, y * rows // fb_h)
                on_y_gap = (
                    y - y_edges[r] < gap
                    or y_edges[r + 1] - y <= gap
                )
                row = bytearray()
                for c in range(cols):
                    width = x_edges[c + 1] - x_edges[c]
                    if width <= 0:
                        continue
                    color = green if (r, c) in filled else blue
                    if on_y_gap:
                        row.extend(gap_px * width)
                    else:
                        left_gap = min(gap, max(1, width // 4))
                        right_gap = min(gap, max(1, width - left_gap))
                        inner = max(0, width - left_gap - right_gap)
                        row.extend(gap_px * left_gap)
                        row.extend(color * inner)
                        row.extend(gap_px * right_gap)
                expected = fb_w * pixel_width
                if len(row) < expected:
                    row.extend(gap_px * ((expected - len(row)) // pixel_width))
                if len(row) < stride:
                    row.extend(b"\x00" * (stride - len(row)))
                fb.write(row[:stride])
        return True
    except (OSError, PermissionError):
        return False


def _curses_draw_touch_grid(stdscr, filled: set[tuple[int, int]], cols: int, rows: int) -> None:
    """Text touch grid, used only when VSTL_TOUCH_USE_CURSES=1 is set."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    title = "QC - Touch Display"
    progress = f"{len(filled)}/{rows * cols}"
    footer = "Drag over every block   ESC ESC fail"
    grid_top = 2
    grid_bottom = max(grid_top + 1, h - 2)
    grid_h = max(1, grid_bottom - grid_top)

    try:
        stdscr.addstr(0, 1, title, curses.A_BOLD | curses.color_pair(CYAN_PAIR))
        stdscr.addstr(0, max(0, w - len(progress) - 2), progress, curses.A_BOLD)
    except curses.error:
        pass

    for r in range(rows):
        y0 = grid_top + r * grid_h // rows
        y1 = grid_top + (r + 1) * grid_h // rows
        if y1 <= y0:
            y1 = min(grid_bottom, y0 + 1)
        pad_y = 0 if y1 - y0 <= 2 else 1
        for c in range(cols):
            x0 = c * w // cols
            x1 = (c + 1) * w // cols
            if x1 <= x0:
                continue
            pad_x = 0 if x1 - x0 <= 4 else 1
            draw_x = min(w - 1, x0 + pad_x)
            draw_w = max(1, x1 - x0 - (pad_x * 2))
            attr = curses.A_REVERSE | curses.color_pair(
                GREEN_PAIR if (r, c) in filled else CYAN_PAIR
            )
            for y in range(y0 + pad_y, max(y0 + pad_y + 1, y1)):
                if y >= grid_bottom:
                    break
                try:
                    stdscr.addstr(y, draw_x, " " * min(draw_w, max(1, w - draw_x - 1)), attr)
                except curses.error:
                    pass
    draw_footer(stdscr, footer)
    stdscr.refresh()

# Make the helper module importable when this file is launched directly
# from /opt/vstl/ (Clonezilla doesn't preserve sys.path through systemd).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vstl_hw_detect as hw  # noqa: E402  (after sys.path tweak)
import vstl_lock_audit as la  # noqa: E402  (Phase 2A — Lock & MDM/BIOS)
import vstl_qc_tests as qc  # noqa: E402  (Phase 2B — Interactive QC)
import vstl_burn_stress as bs  # noqa: E402  (Phase 2C — Burn / Stress)
import vstl_secure_erase as se  # noqa: E402  (Phase 3 — Certified Secure Erase)
import vstl_image_capture as ic  # noqa: E402  (Phase 3 — Capture Golden Copy)
import vstl_image_restore as ir  # noqa: E402  (Phase 3 — Restore Golden Copy)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATHS = ["/opt/vstl/config.env", os.environ.get("VSTL_CONFIG", "")]
LOCAL_AUDIT_FILE = "/var/log/vstl-imaging-phase1.json"
AUTH_SESSION_FILE = os.environ.get(
    "VSTL_AUTH_SESSION_FILE",
    "/var/lib/vstl/imaging-auth-session.json",
)
_BENCH_CLIENT_ID: str | None = None
PENDING_INGEST_DIR = os.environ.get(
    "VSTL_PENDING_INGEST_DIR",
    "/var/lib/vstl/pending-ingest",
)
DHCP_RELEASE_IFACE_FILE = os.environ.get(
    "VSTL_DHCP_RELEASE_IFACE_FILE",
    "/run/vstl-dhcp-release-iface",
)
DHCP_RELEASE_LOG_FILE = os.environ.get(
    "VSTL_DHCP_RELEASE_LOG_FILE",
    "/var/log/vstl-dhcp-release.log",
)


def load_config() -> dict:
    """Parse a KEY=VALUE config.env file (no shell interpolation)."""
    cfg: dict = {}
    for p in CONFIG_PATHS:
        if not p or not os.path.isfile(p):
            continue
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
        break
    # Allow env override
    for k in ("VSTL_API_BASE", "VSTL_API_KEY", "VSTL_REPORT_BASE",
              "VSTL_REPORT_TOKEN", "SERVER_IP", "VSTL_SERVER_IP", "BENCH_ID",
              "VSTL_AUTO_SHUTDOWN", "VSTL_BURN_DURATION_SEC",
              "VSTL_BURN_THROTTLE_C", "VSTL_RELEASE_DHCP_ON_AUDIT_SUBMITTED",
              "VSTL_DHCP_RELEASE_IFACE", "VSTL_DHCP_RELEASE_IFACE_FILE",
              "VSTL_DHCP_RELEASE_LOG_FILE", "VSTL_SHARE_OPERATOR_SESSION"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def _api_base(cfg: dict) -> str:
    base = cfg.get("VSTL_API_BASE", "").rstrip("/")
    if base and not base.endswith("/api"):
        base = f"{base}/api"
    return base


def _bench_id(cfg: dict) -> str:
    """Return one stable identity for every laptop processed by this bench."""
    return str(
        cfg.get("BENCH_ID")
        or cfg.get("SERVER_HOSTNAME")
        or "vstl-imaging"
    ).strip()


def _safe_state_key(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-")
    return (safe or "client")[:128]


def _truthy_config(cfg: dict | None, key: str, default: bool = False) -> bool:
    if cfg is None:
        return default
    value = cfg.get(key)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _shared_operator_session_enabled(cfg: dict | None) -> bool:
    """Opt-in only: never share the active operator login between PXE clients."""
    return _truthy_config(cfg, "VSTL_SHARE_OPERATOR_SESSION", default=False)


def _read_first_existing(paths: list[str]) -> str:
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                value = handle.read().strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def _bench_client_id() -> str:
    """Return a stable key for this booted PXE client, not the whole bench."""
    global _BENCH_CLIENT_ID
    if _BENCH_CLIENT_ID:
        return _BENCH_CLIENT_ID
    explicit = os.environ.get("VSTL_BENCH_CLIENT_ID", "").strip()
    if explicit:
        _BENCH_CLIENT_ID = _safe_state_key(explicit)
        return _BENCH_CLIENT_ID

    parts = [
        _read_first_existing(["/proc/sys/kernel/random/boot_id"]),
        _read_first_existing([
            "/sys/class/dmi/id/product_serial",
            "/sys/class/dmi/id/product_uuid",
        ]),
    ]
    try:
        parts.append(os.uname().nodename)
    except (AttributeError, OSError):
        pass
    seed = "|".join(part for part in parts if part) or str(time.time_ns())
    _BENCH_CLIENT_ID = hashlib.sha256(seed.encode("utf-8", "ignore")).hexdigest()[:24]
    return _BENCH_CLIENT_ID


def _auth_session_path() -> str:
    explicit = os.environ.get("VSTL_AUTH_SESSION_FILE", "").strip()
    if explicit:
        return explicit
    base, ext = os.path.splitext(AUTH_SESSION_FILE)
    if not ext:
        ext = ".json"
    return f"{base}-{_bench_client_id()}{ext}"



def _utc_epoch_from_http_date(value: str) -> int | None:
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.astimezone(timezone.utc).timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _certificate_verify_failed(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    text = f"{reason}".lower()
    return "certificate_verify_failed" in text or "certificate verify failed" in text


def _http_date_candidates(cfg: dict) -> list[tuple[str, bool]]:
    candidates: list[tuple[str, bool]] = []
    server = (
        cfg.get("VSTL_SERVER_IP")
        or cfg.get("SERVER_IP")
        or "10.255.0.75"
    ).strip()
    if server:
        candidates.extend([
            (f"http://{server}/vstl-pxe/boot.ipxe", False),
            (f"http://{server}/", False),
        ])
    base = _api_base(cfg)
    if base:
        candidates.append((base, True))
    seen: set[str] = set()
    result: list[tuple[str, bool]] = []
    for url, insecure_tls in candidates:
        if url and url not in seen:
            seen.add(url)
            result.append((url, insecure_tls))
    return result


def _read_server_http_date_epoch(cfg: dict, timeout: int = 5) -> int | None:
    for url, insecure_tls in _http_date_candidates(cfg):
        req = urllib.request.Request(
            url,
            headers={"User-Agent": BENCH_USER_AGENT},
            method="HEAD",
        )
        context = None
        if insecure_tls and url.lower().startswith("https://"):
            # Time bootstrap only: never send credentials through this context.
            context = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
                epoch = _utc_epoch_from_http_date(resp.headers.get("Date", ""))
                if epoch:
                    return epoch
        except urllib.error.HTTPError as e:
            epoch = _utc_epoch_from_http_date(e.headers.get("Date", ""))
            if epoch:
                return epoch
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
    return None


def _set_system_clock_utc(epoch: int) -> bool:
    if epoch <= 0:
        return False
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return False
    try:
        completed = subprocess.run(
            ["date", "-u", "-s", f"@{epoch}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _repair_clock_after_cert_error(cfg: dict) -> bool:
    epoch = _read_server_http_date_epoch(cfg)
    if not epoch:
        return False
    current = int(time.time())
    # Avoid unnecessary jumps for a normal live clock; this path is for units
    # whose RTC is far enough off that TLS certificate validity checks fail.
    if abs(current - epoch) < 120:
        return True
    return _set_system_clock_utc(epoch)


def _report_base(cfg: dict) -> str:
    base = cfg.get("VSTL_REPORT_BASE", "").rstrip("/")
    if base:
        return base
    server = (
        cfg.get("VSTL_SERVER_IP")
        or cfg.get("SERVER_IP")
        or "10.255.0.75"
    ).strip()
    return f"http://{server}/vstl-reports"


def _bench_state_request(
    cfg: dict,
    method: str,
    resource: str,
    body: dict | None = None,
    query: dict | None = None,
    timeout: int = 8,
) -> tuple[bool, dict, str]:
    """Read/write durable bench state on the on-prem imaging server."""
    token = cfg.get("VSTL_REPORT_TOKEN") or cfg.get("VSTL_API_KEY", "")
    bench_id = _bench_id(cfg)
    if not token or not bench_id:
        return False, {}, "VSTL_REPORT_TOKEN/VSTL_API_KEY / BENCH_ID missing"
    resource_key = str(resource or "").strip().lower()
    params = {"resource": resource, "bench_id": bench_id}
    if resource_key == "session":
        params["client_id"] = _bench_client_id()
    params.update(query or {})
    url = f"{_report_base(cfg)}/bench-state.php?{urllib.parse.urlencode(params)}"
    if body is not None and resource_key == "session":
        body = dict(body)
        body.setdefault("client_id", _bench_client_id())
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-VSTL-Report-Token": token,
            "User-Agent": BENCH_USER_AGENT,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8") or "{}")
            if isinstance(result, dict) and result.get("success"):
                return True, result, "ok"
            return False, result if isinstance(result, dict) else {}, "invalid state response"
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        return False, {}, f"HTTP {e.code}: {detail}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return False, {}, f"state service unavailable: {e}"


def _load_local_auth_session() -> dict:
    try:
        with open(_auth_session_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _load_auth_session(cfg: dict | None = None) -> dict:
    local = _load_local_auth_session()
    if local:
        return local
    if cfg and _shared_operator_session_enabled(cfg):
        ok, data, _msg = _bench_state_request(cfg, "GET", "session")
        session = data.get("session") if ok else None
        if isinstance(session, dict) and session.get("token"):
            _save_local_auth_session(session)
            return session
    return {}


def _save_local_auth_session(session: dict) -> None:
    path = _auth_session_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f)
        os.chmod(path, 0o600)
    except OSError:
        pass


def _save_auth_session(session: dict, cfg: dict | None = None) -> None:
    stored_session = dict(session or {})
    stored_session["bench_client_id"] = _bench_client_id()
    _save_local_auth_session(stored_session)
    if cfg and _shared_operator_session_enabled(cfg):
        _bench_state_request(
            cfg,
            "POST",
            "session",
            body={
                "bench_id": _bench_id(cfg),
                "client_id": _bench_client_id(),
                "session": stored_session,
            },
        )


def _clear_auth_session(cfg: dict | None = None) -> None:
    try:
        os.unlink(_auth_session_path())
    except OSError:
        pass
    if cfg and _shared_operator_session_enabled(cfg):
        _bench_state_request(cfg, "DELETE", "session")


def _operator_user(operator: dict | None) -> dict:
    if not isinstance(operator, dict):
        return {}
    user = operator.get("user")
    return user if isinstance(user, dict) else {}


def _operator_token(operator: dict | None) -> str:
    if not isinstance(operator, dict):
        return ""
    return str(operator.get("token") or "")


def _operator_session_snapshot(operator: dict | None) -> dict:
    """Capture the operator token/profile needed to safely retry queued uploads."""
    if not isinstance(operator, dict):
        return {}
    snapshot: dict[str, object] = {}
    for key in ("token", "token_type", "expires_at", "session_id", "selected_layer"):
        value = operator.get(key)
        if value not in (None, ""):
            snapshot[key] = value
    user = _operator_user(operator)
    if user:
        snapshot["user"] = user
    available_layers = operator.get("available_layers")
    if available_layers:
        snapshot["available_layers"] = available_layers
    return snapshot


def _operator_name(operator: dict | None) -> str:
    user = _operator_user(operator)
    return str(user.get("name") or user.get("email") or "UNKNOWN")


def _operator_roles(operator: dict | None) -> list[str]:
    roles = _operator_user(operator).get("roles") or []
    if isinstance(roles, str):
        return [roles]
    if isinstance(roles, list):
        return [str(role) for role in roles]
    return []


def _truthy_operator_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on", "allowed", "allow", "enabled", "enable"}


def _operator_is_admin(operator: dict | None) -> bool:
    user = _operator_user(operator)
    if bool(user.get("is_admin")):
        return True
    admin_roles = {"admin", "administrator", "super admin", "super_admin", "superadmin"}
    return any(role.strip().lower() in admin_roles for role in _operator_roles(operator))


def _operator_has_capture_access(operator: dict | None) -> bool:
    user = _operator_user(operator)
    return _truthy_operator_value(user.get("can_capture"))


def _testing_modifier_chord_active(require_trigger_key: bool) -> bool:
    try:
        from evdev import InputDevice, ecodes, list_devices  # type: ignore
    except Exception:
        return False

    ctrl_keys = {ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL}
    shift_keys = {ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT}
    alt_keys = {ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT}
    trigger_keys = {ecodes.KEY_T}
    required_keys = ctrl_keys | shift_keys | alt_keys | trigger_keys
    supported_keys: set[int] = set()
    active_keys: set[int] = set()

    for path in list_devices():
        try:
            dev = InputDevice(path)
            caps = dev.capabilities()
            key_codes = set(caps.get(ecodes.EV_KEY, []))
            if not required_keys.intersection(key_codes):
                continue
            supported_keys.update(key_codes)
            active_keys.update(dev.active_keys())
        except Exception:
            continue
    if not trigger_keys.issubset(supported_keys):
        return False
    if not (active_keys & ctrl_keys and active_keys & shift_keys and active_keys & alt_keys):
        return False
    if require_trigger_key and not (active_keys & trigger_keys):
        return False
    return True


def _is_testing_mode_hotkey(ch: int) -> bool:
    if ch == TESTING_MODE_CTRL_T:
        return _testing_modifier_chord_active(require_trigger_key=False)
    if ch == 27:
        return _testing_modifier_chord_active(require_trigger_key=True)
    return False


def _getch_with_testing_mode(stdscr, poll_interval: float = 0.05) -> int | str:
    """Wait for one key while polling the full testing-mode modifier chord."""
    stdscr.nodelay(True)
    try:
        while True:
            if _testing_modifier_chord_active(require_trigger_key=True):
                return TESTING_MODE_SENTINEL
            ch = stdscr.getch()
            if ch != -1:
                if _is_testing_mode_hotkey(ch):
                    return TESTING_MODE_SENTINEL
                return ch
            time.sleep(poll_interval)
    finally:
        stdscr.nodelay(False)


def _testing_mode_operator() -> dict:
    return {
        "token": "",
        "selected_layer": "Layer 2",
        "_bench_layer_confirmed": True,
        "_testing_mode": True,
        "user": {
            "id": "",
            "name": "TESTING MODE",
            "email": "",
            "roles": ["Testing"],
            "can_capture": False,
        },
    }


def _operator_testing_mode(operator: dict | None) -> bool:
    return isinstance(operator, dict) and bool(operator.get("_testing_mode"))


def _normalize_layer(value: object) -> str:
    text = str(value or "").strip().lower().replace("_", " ")
    compact = text.replace(" ", "")
    if compact in {"l1", "layer1"}:
        return "Layer 1"
    if compact in {"l2", "layer2"}:
        return "Layer 2"
    return ""


def _operator_layer(operator: dict | None) -> str:
    user = _operator_user(operator)
    selected = _normalize_layer(
        (operator or {}).get("selected_layer")
        or user.get("layer")
    )
    if selected:
        return selected
    eligible = {_normalize_layer(role) for role in _operator_roles(operator)}
    eligible.discard("")
    return next(iter(eligible)) if len(eligible) == 1 else ""


def _operator_technician_level(operator: dict | None) -> str:
    return {"Layer 1": "L1", "Layer 2": "L2"}.get(_operator_layer(operator), "")


def _operator_has_layer_access(operator: dict | None) -> bool:
    user = _operator_user(operator)
    if _normalize_layer((operator or {}).get("selected_layer") or user.get("layer")):
        return True
    candidates = list(_operator_roles(operator))
    available = (operator or {}).get("available_layers") or user.get("available_layers") or []
    if isinstance(available, str):
        candidates.append(available)
    elif isinstance(available, list):
        candidates.extend(str(layer) for layer in available)
    return any(_normalize_layer(value) for value in candidates)


def _operator_bench_mode(operator: dict | None) -> str:
    tech = _operator_technician_level(operator)
    if tech:
        return tech
    if _operator_has_capture_access(operator):
        return "IMAGING"
    return ""


def _operator_label(operator: dict | None) -> str:
    layer = _operator_layer(operator)
    roles = ", ".join(_operator_roles(operator))
    suffix = layer or roles or ("Admin" if _operator_is_admin(operator) else "")
    return f"{_operator_name(operator)}" + (f" ({suffix})" if suffix else "")


def _nonnegative_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _normalize_box_scope(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    lot_no = str(value.get("lot_no") or "").strip()
    box_no = str(value.get("box_no") or value.get("box_number") or "").strip()
    if not lot_no or not box_no:
        return {}
    total = _nonnegative_int(value.get("total"))
    remaining = _nonnegative_int(value.get("remaining"))
    imaged_default = max(0, total - remaining) if total else 0
    imaged = _nonnegative_int(value.get("imaged"), imaged_default)
    model_label = str(value.get("model_label") or "").strip()
    model_count = _nonnegative_int(value.get("model_count"))
    return {
        "lot_no": lot_no[:128],
        "box_no": box_no[:128],
        "total": total,
        "remaining": remaining,
        "imaged": imaged,
        "brand": str(value.get("brand") or "").strip()[:80],
        "model": str(value.get("model") or "").strip()[:120],
        "model_label": model_label[:160],
        "model_count": model_count,
    }


def _operator_box(operator: dict | None) -> dict:
    if not isinstance(operator, dict):
        return {}
    return _normalize_box_scope(operator.get("selected_box"))


def _box_scope_key(value: object) -> tuple[str, str]:
    box = _normalize_box_scope(value)
    return (
        str(box.get("lot_no") or "").casefold(),
        str(box.get("box_no") or "").casefold(),
    )


def _box_scope_label(value: object) -> str:
    box = _normalize_box_scope(value)
    if not box:
        return "No Lot / Box selected"
    total = box.get("total", 0)
    imaged = box.get("imaged", 0)
    progress = f" - {imaged}/{total} imaged" if total else ""
    return f"{box['lot_no']} / {box['box_no']}{progress}"


def _box_model_label(box: dict) -> str:
    label = str(box.get("model_label") or "").strip()
    if label:
        return label
    return " ".join(part for part in (box.get("brand", ""), box.get("model", "")) if part)


def _set_operator_box(operator: dict, value: object, cfg: dict | None = None) -> dict:
    box = _normalize_box_scope(value)
    if box:
        operator["selected_box"] = box
    else:
        operator.pop("selected_box", None)
    if cfg:
        _save_auth_session(operator, cfg)
    return box


def _attach_box_scope_to_payload(payload: dict, operator: dict | None) -> None:
    if _operator_technician_level(operator) != "L1":
        return
    box = _operator_box(operator)
    if not box:
        return
    payload["lot_no"] = box["lot_no"]
    payload["lot_number"] = box["lot_no"]
    payload["box_no"] = box["box_no"]
    payload["box_number"] = box["box_no"]
    payload["box_model_label"] = _box_model_label(box)
    payload["box_total"] = box.get("total", 0)
    payload["box_imaged"] = box.get("imaged", 0)
    payload["box_remaining"] = box.get("remaining", 0)
    payload["box_model_count"] = box.get("model_count", 0)
    payload.setdefault("raw_data", {})["box_scope"] = dict(box)


def _refresh_operator_boxes_after_ingest(cfg: dict, operator: dict | None) -> None:
    """Refresh assigned boxes after a successful cloud ingest.

    VSTL 360 owns slot counts and auto-closes completed allocations, so the
    bench must not infer completion by decrementing a local cache.
    """
    if not isinstance(operator, dict):
        return
    if _operator_technician_level(operator) != "L1":
        return
    ok, boxes, _error, _status = fetch_my_boxes(cfg, operator)
    if not ok:
        return
    current_key = _box_scope_key(_operator_box(operator))
    if current_key != ("", ""):
        for box in boxes:
            if _box_scope_key(box) == current_key:
                _set_operator_box(operator, box, cfg)
                return
        _set_operator_box(operator, {}, cfg)
        return
    if len(boxes) == 1:
        _set_operator_box(operator, boxes[0], cfg)


def _auth_request(
    cfg: dict,
    method: str,
    path: str,
    body: dict | None = None,
    token: str = "",
    include_key: bool = False,
    timeout: int = 15,
) -> tuple[bool, dict, str, int | None]:
    base = _api_base(cfg)
    if not base:
        return False, {}, "VSTL_API_BASE missing in /opt/vstl/config.env", None
    headers = {
        "Content-Type": "application/json",
        "User-Agent": BENCH_USER_AGENT,
    }
    if include_key:
        key = cfg.get("VSTL_API_KEY", "")
        if not key:
            return False, {}, "VSTL_API_KEY missing in /opt/vstl/config.env", None
        headers["X-API-Key"] = key
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{base}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
            return True, payload if isinstance(payload, dict) else {}, "ok", resp.status
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:300].decode("utf-8", "replace")
            parsed = json.loads(detail)
            error_data = parsed if isinstance(parsed, dict) else {}
        except Exception:
            detail = e.reason
            error_data = {}
        return False, error_data, f"HTTP {e.code}: {detail}", e.code
    except urllib.error.URLError as e:
        if _certificate_verify_failed(e) and _repair_clock_after_cert_error(cfg):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8") or "{}")
                    return True, payload if isinstance(payload, dict) else {}, "ok", resp.status
            except urllib.error.HTTPError as retry_http:
                try:
                    detail = retry_http.read()[:300].decode("utf-8", "replace")
                    parsed = json.loads(detail)
                    error_data = parsed if isinstance(parsed, dict) else {}
                except Exception:
                    detail = retry_http.reason
                    error_data = {}
                return False, error_data, f"HTTP {retry_http.code}: {detail}", retry_http.code
            except urllib.error.URLError as retry_error:
                return False, {}, f"network error after clock sync: {retry_error.reason}", None
            except (TimeoutError, OSError, ValueError) as retry_error:
                return False, {}, f"connection error after clock sync: {retry_error}", None
        return False, {}, f"network error: {e.reason}", None
    except (TimeoutError, OSError, ValueError) as e:
        return False, {}, f"connection error: {e}", None


def _auth_session_from_response(data: dict) -> dict:
    user = dict(data.get("user") or {})
    selected_layer = _normalize_layer(data.get("selected_layer") or user.get("layer"))
    if selected_layer:
        user["layer"] = selected_layer
    return {
        "token": data.get("token"),
        "expires_at": data.get("expires_at", ""),
        "session_id": data.get("session_id", ""),
        "selected_layer": selected_layer,
        "user": user,
    }


def auth_login_pin(cfg: dict, pin: str, layer: str = "") -> tuple[bool, dict, str]:
    body = {
        "pin": pin,
        "bench_id": _bench_id(cfg),
    }
    if layer:
        body["layer"] = layer
    ok, data, msg, status = _auth_request(
        cfg,
        "POST",
        "/imaging/auth/login-pin",
        body=body,
        include_key=True,
        timeout=18,
    )
    if ok and data.get("ok") and data.get("token"):
        session = _auth_session_from_response(data)
        _save_auth_session(session, cfg)
        return True, session, "login ok"
    if ok and data.get("need_layer"):
        return False, data, "working layer selection required"
    friendly = {
        400: "PIN or selected layer is invalid",
        401: "PIN was not recognized",
        403: "This VSTL 360 user is inactive",
        429: "Too many failed PIN attempts; ask an Admin to reset the PIN or wait 15 minutes",
    }.get(status)
    if friendly:
        return False, data, friendly
    return False, {}, msg if not ok else json.dumps(data)[:300]


def auth_me(cfg: dict, token: str) -> tuple[bool, dict, str]:
    ok, data, msg, status = _auth_request(
        cfg,
        "GET",
        "/imaging/auth/me",
        token=token,
        timeout=12,
    )
    if ok and data.get("ok"):
        session = {
            "token": token,
            "expires_at": ((data.get("session") or {}).get("expires_at") or ""),
            "session_id": ((data.get("session") or {}).get("id") or ""),
            "user": data.get("user") or {},
        }
        session["selected_layer"] = _normalize_layer(session["user"].get("layer"))
        _save_auth_session(session, cfg)
        return True, session, "session ok"
    if status == 401:
        _clear_auth_session(cfg)
    return False, {}, msg if not ok else json.dumps(data)[:300]


def fetch_my_boxes(
    cfg: dict,
    operator: dict | None,
) -> tuple[bool, list[dict], str, int | None]:
    """Fetch the authenticated L1 operator's active box allocations."""
    token = _operator_token(operator)
    if not token:
        return False, [], "operator session token missing", 401
    ok, data, msg, status = _auth_request(
        cfg,
        "GET",
        "/imaging/my-boxes",
        token=token,
        timeout=18,
    )
    if not ok:
        if status == 401:
            _clear_auth_session(cfg)
        return False, [], msg, status
    boxes = []
    for item in data.get("boxes") or []:
        box = _normalize_box_scope(item)
        if box:
            boxes.append(box)
    return True, boxes, "", status


def auth_logout(cfg: dict, token: str) -> None:
    if token:
        _auth_request(cfg, "POST", "/imaging/auth/logout", token=token, timeout=8)
    _clear_auth_session(cfg)


def _attach_operator_to_payload(payload: dict, operator: dict | None) -> None:
    user = _operator_user(operator)
    if not user:
        return
    name = _operator_name(operator)
    payload["user"] = name
    payload["technician_user_id"] = str(user.get("id") or "")
    payload["technician_user_name"] = name
    payload["technician_user_email"] = str(user.get("email") or "")
    payload["technician_roles"] = _operator_roles(operator)
    payload["technician_is_admin"] = _operator_is_admin(operator)
    payload["technician_can_capture"] = _operator_has_capture_access(operator)
    raw = payload.setdefault("raw_data", {})
    raw["operator"] = {
        "id": payload["technician_user_id"],
        "name": name,
        "email": payload["technician_user_email"],
        "roles": payload["technician_roles"],
        "is_admin": payload["technician_is_admin"],
        "can_capture": payload["technician_can_capture"],
        "layer": _operator_layer(operator),
    }


def _read_burn_duration(cfg: dict) -> int:
    """Pull the Phase-2C burn duration in seconds, defaulting to 5 minutes."""
    try:
        return max(30, int(cfg.get("VSTL_BURN_DURATION_SEC", "300")))
    except (ValueError, TypeError):
        return 300


# ---------------------------------------------------------------------------
# UI primitives
# ---------------------------------------------------------------------------
PURPLE_PAIR = 1
GREEN_PAIR = 2
YELLOW_PAIR = 3
RED_PAIR = 4
DIM_PAIR = 5
CYAN_PAIR = 6
WHITE_PAIR = 7
FOOTER_PAIR = 8


def _setup_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(PURPLE_PAIR, curses.COLOR_WHITE, curses.COLOR_MAGENTA)
    curses.init_pair(GREEN_PAIR, curses.COLOR_GREEN, -1)
    curses.init_pair(YELLOW_PAIR, curses.COLOR_YELLOW, -1)
    curses.init_pair(RED_PAIR, curses.COLOR_RED, -1)
    curses.init_pair(DIM_PAIR, curses.COLOR_CYAN, -1)
    curses.init_pair(CYAN_PAIR, curses.COLOR_CYAN, -1)
    curses.init_pair(WHITE_PAIR, curses.COLOR_WHITE, -1)
    curses.init_pair(FOOTER_PAIR, curses.COLOR_BLACK, curses.COLOR_WHITE)


_LAST_SCREEN_KEY: Optional[str] = None


def _write_tty_bytes(payload: bytes) -> None:
    """Write terminal control bytes to every plausible active console."""
    wrote = False
    try:
        if os.isatty(1):
            os.write(1, payload)
            wrote = True
    except OSError:
        pass

    # /dev/tty is the controlling terminal; /dev/tty1 is the Clonezilla live
    # console requested by the PXE kernel command line. Some KVM/framebuffer
    # sessions ignore one path but accept the other.
    for path in ("/dev/tty", "/dev/tty1", "/dev/console"):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NOCTTY)
            try:
                os.write(fd, payload)
                wrote = True
            finally:
                os.close(fd)
        except OSError:
            continue

    if not wrote:
        try:
            os.write(1, payload)
        except OSError:
            pass


def _set_console_cursor_visible(visible: bool) -> None:
    """Hide/show the Linux console cursor across KVM/framebuffer consoles."""
    _write_tty_bytes(b"\033[?25h" if visible else b"\033[?25l")
    try:
        subprocess.run(
            ["setterm", "-cursor", "on" if visible else "off"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        with open("/sys/class/graphics/fbcon/cursor_blink", "w") as f:
            f.write("1\n" if visible else "0\n")
    except OSError:
        pass


def _set_console_graphics_mode(enabled: bool) -> bool:
    """Stop fbcon from repainting text/cursor cells over the touch grid."""
    import fcntl

    kdsetmode = 0x4B3A
    mode = 0x01 if enabled else 0x00  # KD_GRAPHICS / KD_TEXT
    for path in ("/dev/tty1", "/dev/tty", "/dev/console"):
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
            try:
                fcntl.ioctl(fd, kdsetmode, mode)
                return True
            finally:
                os.close(fd)
        except OSError:
            continue
    return False


def _physical_blank_tty(stdscr) -> None:
    """Physically overwrite the active Linux console with blank cells."""
    try:
        h, w = stdscr.getmaxyx()
        width = max(1, w - 1)
        blank = b" " * width
        chunks = [b"\033[?25l\033[0m\033[H\033[2J\033[3J"]
        for y in range(1, h + 1):
            chunks.append(f"\033[{y};1H".encode("ascii"))
            chunks.append(blank)
        chunks.append(b"\033[H")
        _write_tty_bytes(b"".join(chunks))
    except (curses.error, OSError):
        pass


def _restore_curses_after_framebuffer(stdscr, screen_key: str) -> None:
    """Return from direct framebuffer graphics to a drawable curses screen."""
    try:
        _set_console_graphics_mode(False)
    except (curses.error, OSError):
        pass
    _set_console_cursor_visible(True)
    try:
        curses.reset_prog_mode()
    except curses.error:
        pass
    try:
        _write_tty_bytes(b"\033[?25l\033[0m\033[H\033[2J\033[3J")
        _physical_blank_tty(stdscr)
        stdscr.bkgd(" ", curses.A_NORMAL)
        stdscr.nodelay(False)
        stdscr.erase()
        stdscr.clear()
        stdscr.clearok(True)
        _blank_canvas(stdscr)
        stdscr.touchwin()
        stdscr.redrawwin()
        stdscr.refresh()
        try:
            curses.doupdate()
        except curses.error:
            pass
    except (curses.error, OSError):
        pass
    _prepare_screen_transition(stdscr, screen_key)


def _prepare_screen_transition(stdscr, screen_key: str) -> None:
    """Force a clean transition once per screen, without repainting every loop.

    PiKVM/framebuffer consoles sometimes keep stale character cells unless the
    terminal receives a real clear-screen sequence. Doing that on every draw
    makes menus blink, so this only runs when the page identity changes.
    """
    global _LAST_SCREEN_KEY
    if screen_key == _LAST_SCREEN_KEY:
        return
    _LAST_SCREEN_KEY = screen_key
    try:
        # Send a physical terminal clear for framebuffer/KVM consoles that
        # keep old cells even after curses.erase(). This runs once per screen.
        _write_tty_bytes(b"\033[?25l\033[0m\033[H\033[2J\033[3J")
        _physical_blank_tty(stdscr)
        stdscr.bkgd(" ", curses.A_NORMAL)
        stdscr.erase()
        stdscr.clear()
        stdscr.clearok(True)
        _blank_canvas(stdscr)
        stdscr.touchwin()
        stdscr.redrawwin()
        stdscr.refresh()
        try:
            curses.doupdate()
        except curses.error:
            pass
    except (curses.error, OSError):
        pass


def _blank_canvas(stdscr) -> None:
    """Overwrite every visible console row with blank normal cells."""
    try:
        h, w = stdscr.getmaxyx()
        width = max(1, w - 1)
        blank = " " * width
    except curses.error:
        return

    try:
        stdscr.attrset(curses.A_NORMAL)
        stdscr.bkgd(" ", curses.A_NORMAL)
    except curses.error:
        pass

    for y in range(h):
        try:
            stdscr.move(y, 0)
            stdscr.clrtoeol()
            stdscr.addstr(y, 0, blank, curses.A_NORMAL)
        except curses.error:
            continue


def _begin_screen_frame(stdscr, subtitle: str) -> None:
    """Start a fresh full-screen frame before drawing screen content."""
    try:
        stdscr.erase()
    except curses.error:
        pass
    draw_header(stdscr, subtitle)


def _drain_pending_input(stdscr, duration: float = 0.08) -> None:
    """Drop key repeats that arrive during screen transitions."""
    try:
        stdscr.nodelay(True)
        end = time.monotonic() + duration
        while time.monotonic() < end:
            if stdscr.getch() == -1:
                time.sleep(0.01)
    except curses.error:
        pass
    finally:
        try:
            stdscr.nodelay(False)
        except curses.error:
            pass


def draw_header(stdscr, subtitle: str = "") -> None:
    _prepare_screen_transition(stdscr, subtitle)
    h, w = stdscr.getmaxyx()
    _blank_canvas(stdscr)
    title = " VSTL 360 — Bench Imaging "
    stdscr.addstr(0, 0, " " * w, curses.color_pair(PURPLE_PAIR) | curses.A_BOLD)
    stdscr.addstr(
        0,
        max(0, (w - len(title)) // 2),
        title,
        curses.color_pair(PURPLE_PAIR) | curses.A_BOLD,
    )
    if subtitle:
        stdscr.addstr(1, 2, subtitle, curses.color_pair(DIM_PAIR))


def draw_footer(stdscr, hint: str) -> None:
    h, w = stdscr.getmaxyx()
    attr = curses.color_pair(FOOTER_PAIR)
    stdscr.addstr(h - 1, 0, " " * (w - 1), attr)
    stdscr.addstr(h - 1, 2, hint[: w - 4], attr)


def center_block(stdscr, lines: list[tuple[str, int]], top_offset: int = 4) -> None:
    """Render a centered list of (text, attr) lines starting at top_offset."""
    h, w = stdscr.getmaxyx()
    y = top_offset
    for text, attr in lines:
        parts = str(text).splitlines() or [""]
        for part in parts:
            if y >= h - 2:
                return
            x = max(2, (w - len(part)) // 2)
            try:
                stdscr.addstr(y, x, part[: w - x - 2], attr)
            except curses.error:
                pass
            y += 1


def _safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    """addstr that swallows boundary errors."""
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def _draw_selectable_row(
    stdscr,
    y: int,
    x: int,
    text: str,
    selected: bool,
    marker: str = ">",
) -> None:
    """Draw a selectable row as a fixed-width strip.

    Linux framebuffer consoles sometimes leave old reverse-video cells behind
    when only a shorter label is repainted. Repaint the full available row so
    old highlights and markers cannot survive when selection changes.
    """
    _, w = stdscr.getmaxyx()
    x = max(0, min(x, max(0, w - 2)))
    width = max(1, w - 1)
    text_width = max(1, w - x - 1)
    attr = curses.A_BOLD | curses.color_pair(GREEN_PAIR) if selected else curses.A_NORMAL
    prefix = marker if selected else " "
    row = f"{prefix} {text}"
    _safe_addstr(stdscr, y, 0, " " * width, curses.A_NORMAL)
    _safe_addstr(stdscr, y, x, row[:text_width], attr)


BENCH_WORKING_LAYERS = ("Layer 1", "Layer 2")


def _set_operator_working_layer(operator: dict, layer: str, cfg: dict | None = None) -> dict:
    selected = _normalize_layer(layer)
    if not selected:
        return operator
    operator["selected_layer"] = selected
    user = _operator_user(operator)
    if user:
        user["layer"] = selected
    if cfg:
        _save_auth_session(operator, cfg)
    return operator


def screen_working_layer(stdscr, operator_name: str, available_layers: list | None = None) -> str:
    """Choose the local bench working layer. L1/L2 users may choose either mode."""
    options = []
    for value in list(BENCH_WORKING_LAYERS) + list(available_layers or []):
        normalized = _normalize_layer(value)
        if normalized and normalized not in options:
            options.append(normalized)
    if not options:
        return ""
    selected = 0
    while True:
        _begin_screen_frame(stdscr, "Select working layer")
        center_block(stdscr, [
            (f"Operator: {operator_name or 'VSTL 360 user'}", curses.A_BOLD),
            ("", 0),
            ("Choose the bench mode for this unit.", curses.color_pair(DIM_PAIR)),
            ("L1 and L2 users may select either L1 or L2.", curses.color_pair(DIM_PAIR)),
        ], top_offset=3)
        _, width = stdscr.getmaxyx()
        for index, layer in enumerate(options):
            label = f"{index + 1}. {layer}"
            _draw_selectable_row(
                stdscr,
                8 + index * 2,
                max(4, (width - len(label) - 6) // 2),
                label,
                index == selected,
            )
        draw_footer(stdscr, "UP/DOWN select   ENTER confirm   Q cancel")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(options)
        elif ch in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(options)
        elif ord("1") <= ch <= ord(str(min(9, len(options)))):
            return options[ch - ord("1")]
        elif ch in (10, 13, curses.KEY_ENTER):
            return options[selected]
        elif ch in (ord("q"), ord("Q"), 27):
            return ""


def screen_box_picker(
    stdscr,
    cfg: dict,
    operator: dict,
    initial_boxes: Optional[list[dict]] = None,
    initial_error: str = "",
    initial_status: int | None = None,
) -> bool:
    """Select and persist the L1 operator's assigned Lot and Box."""
    boxes: Optional[list[dict]] = initial_boxes
    error = initial_error
    status = initial_status
    selected = 0

    while True:
        if boxes is None:
            _begin_screen_frame(stdscr, "Select assigned Lot / Box")
            center_block(
                stdscr,
                [("Loading your assigned boxes from VSTL 360...", curses.A_BOLD)],
                top_offset=5,
            )
            draw_footer(stdscr, "Please wait")
            stdscr.refresh()
            ok, boxes, error, status = fetch_my_boxes(cfg, operator)
            if ok:
                current_key = _box_scope_key(_operator_box(operator))
                selected = next(
                    (index for index, box in enumerate(boxes) if _box_scope_key(box) == current_key),
                    0,
                )

        if error and boxes == []:
            _begin_screen_frame(stdscr, "Assigned Lot / Box unavailable")
            center_block(stdscr, [
                ("Could not load your assigned boxes.",
                 curses.A_BOLD | curses.color_pair(RED_PAIR)),
                (error[:120], curses.color_pair(DIM_PAIR)),
            ], top_offset=5)
            draw_footer(stdscr, "R retry   S switch user   Q quit")
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord("r"), ord("R"), 10, 13, curses.KEY_ENTER):
                boxes, error, status = None, "", None
                continue
            if ch in (ord("s"), ord("S")):
                auth_logout(cfg, _operator_token(operator))
                return False
            if ch in (ord("q"), ord("Q")):
                sys.exit(0)
            continue

        if not boxes:
            _begin_screen_frame(stdscr, "No assigned Lot / Box")
            center_block(stdscr, [
                ("No boxes assigned to you. Ask your supervisor to allocate a box.",
                 curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
                (f"Operator: {_operator_name(operator)}",
                 curses.color_pair(DIM_PAIR)),
            ], top_offset=5)
            draw_footer(stdscr, "R refresh   S switch user   Q quit")
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord("r"), ord("R"), 10, 13, curses.KEY_ENTER):
                boxes = None
                continue
            if ch in (ord("s"), ord("S")):
                auth_logout(cfg, _operator_token(operator))
                return False
            if ch in (ord("q"), ord("Q")):
                sys.exit(0)
            continue

        selected %= len(boxes)
        _begin_screen_frame(stdscr, "Select assigned Lot / Box")
        h, w = stdscr.getmaxyx()
        _safe_addstr(stdscr, 3, 4, f"Operator: {_operator_name(operator)}", curses.A_BOLD)
        _safe_addstr(
            stdscr,
            4,
            4,
            "Choose the box being processed. The selection remains active until switched.",
            curses.color_pair(DIM_PAIR),
        )
        visible_count = max(1, min(len(boxes), (h - 10) // 2))
        offset = min(max(0, selected - visible_count + 1), max(0, len(boxes) - visible_count))
        for row, index in enumerate(range(offset, min(len(boxes), offset + visible_count))):
            box = boxes[index]
            progress = (
                f"{box['remaining']} left / {box['total']}"
                if box["total"]
                else f"{box['remaining']} left"
            )
            detail = _box_model_label(box)
            label = f"{box['lot_no']} / {box['box_no']}"
            if detail:
                label += f" - {detail}"
            label += f" - {progress}"
            _draw_selectable_row(stdscr, 7 + row * 2, 4, label[: max(20, w - 10)], index == selected)
        draw_footer(stdscr, "UP/DOWN select   ENTER confirm   R refresh   S switch user   Q quit")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(boxes)
        elif ch in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(boxes)
        elif ch in (10, 13, curses.KEY_ENTER):
            _set_operator_box(operator, boxes[selected], cfg)
            return True
        elif ch in (ord("r"), ord("R")):
            boxes, error, status = None, "", None
        elif ch in (ord("s"), ord("S")):
            auth_logout(cfg, _operator_token(operator))
            return False
        elif ch in (ord("q"), ord("Q")):
            sys.exit(0)


def ensure_operator_box(stdscr, cfg: dict, operator: dict) -> bool:
    """Refresh a cached L1 box or show the picker when selection is needed."""
    ok, boxes, error, status = fetch_my_boxes(cfg, operator)
    if ok:
        current_key = _box_scope_key(_operator_box(operator))
        for box in boxes:
            if current_key != ("", "") and _box_scope_key(box) == current_key:
                _set_operator_box(operator, box, cfg)
                return True
        return screen_box_picker(stdscr, cfg, operator, initial_boxes=boxes)
    return screen_box_picker(
        stdscr,
        cfg,
        operator,
        initial_boxes=[],
        initial_error=error,
        initial_status=status,
    )


def screen_sync_pending(stdscr, cfg: dict, operator: dict) -> None:
    """Retry this operator's server-backed cloud queue after login."""
    user_id = str(_operator_user(operator).get("id") or "")
    if not user_id:
        return
    pending = _list_pending_ingests(cfg, user_id)
    if not pending:
        return
    _begin_screen_frame(stdscr, "Sync queued audits")
    center_block(stdscr, [
        (f"Retrying {len(pending)} completed unit(s) for {_operator_name(operator)}...", curses.A_BOLD),
        ("Only audits owned by this operator will be sent.", curses.color_pair(DIM_PAIR)),
    ], top_offset=5)
    draw_footer(stdscr, "Please wait")
    stdscr.refresh()
    results = flush_pending_ingests(cfg, operator)
    sent = sum(1 for ok, _msg, _data, _status in results.values() if ok)
    remaining = max(0, len(pending) - sent)
    _begin_screen_frame(stdscr, "Sync queued audits")
    center_block(stdscr, [
        (f"Cloud sync complete: {sent} sent, {remaining} still queued.",
         curses.A_BOLD | curses.color_pair(GREEN_PAIR if remaining == 0 else YELLOW_PAIR)),
    ], top_offset=5)
    draw_footer(stdscr, "Continuing...")
    stdscr.refresh()
    time.sleep(1.0)


def screen_login(stdscr, cfg: dict) -> dict:
    """Require VSTL 360 login before any bench screens appear."""
    saved = _load_auth_session(cfg)
    saved_box = _operator_box(saved)
    token = _operator_token(saved)
    if token:
        ok, session, _msg = auth_me(cfg, token)
        if ok:
            if saved_box:
                session["selected_box"] = saved_box
                _save_auth_session(session, cfg)
            while True:
                _begin_screen_frame(stdscr, "VSTL 360 Login")
                login_lines = [
                    ("Existing operator session found.", curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
                    ("", 0),
                    (f"User: {_operator_label(session)}", curses.A_BOLD),
                ]
                if _operator_box(session):
                    login_lines.append((f"Box: {_box_scope_label(_operator_box(session))}", curses.color_pair(CYAN_PAIR)))
                login_lines.extend([
                    ("", 0),
                    ("ENTER continue with this user", curses.color_pair(DIM_PAIR)),
                    ("S switch user / logout", curses.color_pair(DIM_PAIR)),
                ])
                center_block(stdscr, login_lines, top_offset=4)
                draw_footer(stdscr, "ENTER continue   S switch user   Q quit")
                stdscr.refresh()
                ch = _getch_with_testing_mode(stdscr)
                if ch == TESTING_MODE_SENTINEL:
                    return _testing_mode_operator()
                if ch in (10, 13, curses.KEY_ENTER):
                    screen_sync_pending(stdscr, cfg, session)
                    return session
                if ch in (ord("s"), ord("S")):
                    auth_logout(cfg, token)
                    break
                if ch in (ord("q"), ord("Q")):
                    sys.exit(0)

    last_error = ""
    while True:
        _begin_screen_frame(stdscr, "VSTL 360 Login")
        _safe_addstr(stdscr, 3, 4, "Login is required before VSTL Bench access.", curses.A_BOLD)
        _safe_addstr(stdscr, 5, 6, "Enter your 4-digit Imaging PIN:", curses.A_BOLD)
        _safe_addstr(
            stdscr,
            7,
            6,
            "Login is kept on this laptop only; nearby benches must sign in separately.",
            curses.color_pair(DIM_PAIR),
        )
        if last_error:
            _safe_addstr(stdscr, 10, 6, f"PIN login failed: {last_error}"[:90], curses.color_pair(RED_PAIR))
        draw_footer(stdscr, "Type PIN   ENTER login   ESC clears field   Q quit")
        stdscr.move(5, 42)
        stdscr.refresh()
        pin = _read_input_line(
            stdscr,
            5,
            42,
            mask=True,
            max_len=4,
        ).strip()
        if pin == TESTING_MODE_SENTINEL:
            return _testing_mode_operator()
        if not pin:
            last_error = "PIN required"
            continue
        if pin.lower() == "q":
            sys.exit(0)
        if not pin.isdigit() or len(pin) != 4:
            last_error = "PIN must be exactly 4 digits"
            continue

        _begin_screen_frame(stdscr, "VSTL 360 Login")
        center_block(stdscr, [("Checking PIN with VSTL 360...", curses.A_BOLD)], top_offset=5)
        draw_footer(stdscr, "Please wait")
        stdscr.refresh()
        ok, session, msg = auth_login_pin(cfg, pin)
        if not ok and session.get("need_layer"):
            selected_layer = screen_working_layer(
                stdscr,
                str(session.get("operator_name") or ""),
                session.get("available_layers") or [],
            )
            if not selected_layer:
                last_error = "working layer selection cancelled"
                continue
            _begin_screen_frame(stdscr, "VSTL 360 Login")
            center_block(
                stdscr,
                [(f"Starting {selected_layer} shift session...", curses.A_BOLD)],
                top_offset=5,
            )
            draw_footer(stdscr, "Please wait")
            stdscr.refresh()
            ok, session, msg = auth_login_pin(cfg, pin, selected_layer)
            if ok:
                _set_operator_working_layer(session, selected_layer, None)
                session["_bench_layer_confirmed"] = True
        if ok:
            _begin_screen_frame(stdscr, "VSTL 360 Login")
            center_block(stdscr, [
                ("Login successful.", curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
                ("", 0),
                (f"User: {_operator_label(session)}", curses.A_BOLD),
            ], top_offset=5)
            draw_footer(stdscr, "Continuing...")
            stdscr.refresh()
            time.sleep(1.0)
            screen_sync_pending(stdscr, cfg, session)
            return session
        last_error = msg


# ---------------------------------------------------------------------------
# Screen 1 — Technician selection (no timeout per spec — must choose)
# ---------------------------------------------------------------------------
def screen_technician(stdscr, operator: dict | None = None) -> str:
    """Compatibility helper; the locally selected bench layer is authoritative."""
    return _operator_technician_level(operator)


# ---------------------------------------------------------------------------
# Screen 2 — Main menu with 5-second auto-select Option 1
# ---------------------------------------------------------------------------
MENU_OPTIONS = [
    "Restore Approved System Image  (with QC Test and Certified Secure Erase)",
    "QC Test Only  (Certified Secure Erase optional)",
    "Certified Secure Erase",
    "Capture Full System Image",
]
AUTO_SELECT_SECS = 5
BOX_REQUIRED_MENU_CHOICES = {0, 1, 2}


def _visible_menu_options(operator: dict | None) -> list[tuple[int, str]]:
    options = list(enumerate(MENU_OPTIONS))
    if not _operator_technician_level(operator):
        options = [(idx, label) for idx, label in options if idx in (2, 3)]
    if not _operator_has_capture_access(operator):
        options = [(idx, label) for idx, label in options if idx != 3]
    return options


def _menu_choice_requires_l1_box(choice: int | None) -> bool:
    return choice in BOX_REQUIRED_MENU_CHOICES


def screen_main_menu(stdscr, technician: str, operator: dict | None = None) -> int:
    """Returns 0..3 for the selected process.

    Per spec 1.3: countdown is regardless of technician level.
    Per spec 1.4: ENTER confirms highlighted; if none, defaults to Option 1.
    """
    selected = 0
    deadline = time.monotonic() + AUTO_SELECT_SECS
    stdscr.nodelay(True)

    try:
        while True:
            visible_options = _visible_menu_options(operator)
            _begin_screen_frame(stdscr, f"Technician: {technician}")
            h, w = stdscr.getmaxyx()
            _safe_addstr(stdscr, 3, 4, f"User: {_operator_label(operator)}", curses.A_BOLD)
            menu_start = 6
            _safe_addstr(stdscr, menu_start - 2, 4, "Choose an action:", curses.A_BOLD)

            for display_idx, (_choice_idx, label) in enumerate(visible_options):
                y = menu_start + display_idx * 2
                _draw_selectable_row(
                    stdscr,
                    y,
                    6,
                    f"{display_idx + 1}. {label}",
                    display_idx == selected,
                )
            if not _operator_has_capture_access(operator):
                _safe_addstr(
                    stdscr,
                    menu_start + len(visible_options) * 2,
                    6,
                    "Capture is hidden because this operator has no Capture permission (needs Admin or the Imaging role).",
                    curses.color_pair(DIM_PAIR),
                )

            # 2026-05-19 BUGFIX: once the operator touches any key we set
            # `deadline = float("inf")` to cancel auto-select. The next loop
            # iteration tried to render `int(deadline - now)` which raises
            # OverflowError on `int(inf)` → curses tears down → the bench
            # crashed to a blank framebuffer ("blue screen"). Only compute
            # the countdown when the deadline is still finite.
            if math.isfinite(deadline):
                remaining = max(0, int(deadline - time.monotonic()))
                cdown = (
                    f"Auto-selecting first visible option in {remaining}s — press any arrow key to choose, ENTER to confirm"
                    if remaining > 0
                    else "Confirming first visible option…"
                )
                _safe_addstr(stdscr, menu_start + len(visible_options) * 2 + 2, 4, cdown, curses.color_pair(YELLOW_PAIR))
            else:
                _safe_addstr(stdscr, menu_start + len(visible_options) * 2 + 2, 4,
                             "Auto-select cancelled — press ENTER to confirm your choice",
                             curses.color_pair(DIM_PAIR))
            draw_footer(stdscr, f"UP/DOWN navigate   1-{len(visible_options)} jump   ENTER confirm   Q quit")
            stdscr.refresh()

            ch = stdscr.getch()
            if ch == -1:
                if time.monotonic() >= deadline:
                    return visible_options[0][0]
                time.sleep(0.1)
                continue

            # User pressed a key — cancel the auto-select countdown
            deadline = float("inf")

            if ch in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(visible_options)
            elif ch in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(visible_options)
            elif ord("1") <= ch <= ord(str(len(visible_options))):
                selected = ch - ord("1")
            elif ch in (10, 13, curses.KEY_ENTER):
                return visible_options[selected][0]
            elif ch in (ord("q"), ord("Q")):
                sys.exit(0)
    finally:
        stdscr.nodelay(False)


def screen_testing_mode_menu(stdscr) -> int:
    """Hidden maintenance menu reached by the testing-mode hotkey."""
    options = [
        (TESTING_RESTORE_ONLY_CHOICE, "5", TESTING_RESTORE_ONLY_LABEL),
        (TESTING_QC_ONLY_CHOICE, "6", TESTING_QC_ONLY_LABEL),
        (TESTING_SECURE_ERASE_CHOICE, "7", TESTING_SECURE_ERASE_LABEL),
    ]
    selected = 0
    while True:
        _begin_screen_frame(stdscr, "Testing Mode")
        _safe_addstr(stdscr, 4, 4, "Choose an action:", curses.A_BOLD)
        for row, (_choice, key, label) in enumerate(options):
            _draw_selectable_row(
                stdscr,
                7 + row * 2,
                6,
                f"{key}. {label}",
                row == selected,
            )
        _safe_addstr(
            stdscr,
            14,
            6,
            "Reports and server submissions are disabled in testing mode.",
            curses.color_pair(DIM_PAIR),
        )
        draw_footer(stdscr, "UP/DOWN select   5/6/7 jump   ENTER confirm   Q quit")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(options)
        elif ch in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(options)
        elif ch in (10, 13, curses.KEY_ENTER):
            return options[selected][0]
        else:
            for index, (choice, key, _label) in enumerate(options):
                if ch == ord(key):
                    selected = index
                    return choice
        if ch in (ord("q"), ord("Q")):
            sys.exit(0)


# ---------------------------------------------------------------------------
# Phase 2A — Lock & MDM/BIOS Audit screens
# ---------------------------------------------------------------------------
PHASE2A_RUNNING_STEPS = [
    ("bios_password",  "Reading SMBIOS Hardware Security…"),
    ("ata_security",   "Querying drive password state…"),
    ("computrace",     "Scanning OEM strings for Computrace…"),
    ("intune",         "Probing for Microsoft Intune enrollment…"),
    ("azure_ad",       "Probing for Azure AD / Entra join…"),
    ("vendor_mdm",     "Probing for vendor MDM agents…"),
]


def _draw_lock_running(stdscr, completed_keys: list[str]) -> None:
    stdscr.erase()
    draw_header(stdscr, "Phase 2A — Lock & MDM/BIOS Audit")
    h, w = stdscr.getmaxyx()
    _safe_addstr(stdscr, 3, 4, "Running pre-flight lock checks…", curses.A_BOLD)
    for i, (key, msg) in enumerate(PHASE2A_RUNNING_STEPS):
        y = 5 + i
        if key in completed_keys:
            mark = "✓ "
            attr = curses.color_pair(GREEN_PAIR)
        else:
            mark = "… "
            attr = curses.color_pair(DIM_PAIR)
        label = la.LOCK_LABELS[key]
        _safe_addstr(stdscr, y, 6, f"{mark}{label:<32}  {msg}", attr)
    draw_footer(stdscr, "Please wait — this takes ~5 seconds")
    stdscr.refresh()


def screen_lock_audit_run(stdscr) -> dict:
    """Render the running screen and execute the full audit.

    The detection itself runs synchronously (it's fast — well under a
    second on most units), but we redraw the screen between probes so
    the operator sees progress instead of a frozen UI.
    """
    completed: list[str] = []
    _draw_lock_running(stdscr, completed)

    # We can't easily interleave la.run_full_audit's internals, so we run
    # each detector individually here — same logic, just with redraws.
    parts = la._find_windows_partitions() if os.geteuid() == 0 else []
    mount_root = la._mount_windows_ro(parts) if parts else None
    try:
        checks: dict[str, dict] = {}
        runners = [
            ("bios_password",  la.detect_bios_password),
            ("ata_security",   la.detect_ata_security),
            ("computrace",     la.detect_computrace),
            ("intune",         lambda: la.detect_intune(mount_root)),
            ("azure_ad",       lambda: la.detect_azure_ad(mount_root)),
            ("vendor_mdm",     lambda: la.detect_vendor_mdm(mount_root)),
        ]
        for key, fn in runners:
            checks[key] = fn()
            completed.append(key)
            _draw_lock_running(stdscr, completed)
            time.sleep(0.15)  # cosmetic — let operator see the tick
    finally:
        if mount_root:
            la._umount_quiet(mount_root)

    detected = [k for k in la.LOCK_KEYS_ORDER if checks[k]["present"]]
    return {
        "halted": bool(detected),
        "checks": checks,
        "detected_locks": detected,
        "summary": ", ".join(la.LOCK_LABELS[k] for k in detected) if detected else "All clear",
    }


def screen_lock_manual_confirm(stdscr, label: str, prompt: str) -> bool:
    """Per choice 2c — auto-detect for hardware locks may be wrong, so
    prompt the operator. Returns True if operator confirms a lock IS set
    (i.e. unit IS locked); False if they confirm it is NOT.
    """
    while True:
        stdscr.erase()
        draw_header(stdscr, f"Manual Confirm — {label}")
        h, w = stdscr.getmaxyx()
        center_block(stdscr, [
            (prompt, curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
            ("", 0),
            ("If you are not 100% sure, choose YES (safer).",
             curses.color_pair(DIM_PAIR)),
            ("", 0),
            ("[ Y ]  Yes — this lock IS set / I cannot bypass it",
             curses.color_pair(RED_PAIR) | curses.A_BOLD),
            ("[ N ]  No — confirmed unlocked / I have access",
             curses.color_pair(GREEN_PAIR) | curses.A_BOLD),
        ], top_offset=4)
        draw_footer(stdscr, "Press Y or N")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N")):
            return False


def screen_lock_audit_clear(stdscr, audit: dict) -> None:
    """All clear — show summary for 3 seconds before continuing."""
    stdscr.erase()
    draw_header(stdscr, "Phase 2A — Lock Audit Complete")
    lines = [("✓  No locks or MDM enrolment detected",
              curses.A_BOLD | curses.color_pair(GREEN_PAIR)), ("", 0)]
    for key in la.LOCK_KEYS_ORDER:
        c = audit["checks"][key]
        lines.append((f"{la.LOCK_LABELS[key]:<32}  {c['status']}",
                      curses.color_pair(DIM_PAIR)))
    center_block(stdscr, lines, top_offset=4)
    draw_footer(stdscr, "Continuing in 3 seconds…   ENTER skip")
    stdscr.refresh()
    _wait_with_skip(stdscr, 3)


def _read_input_line(
    stdscr,
    y: int,
    x: int,
    mask: bool,
    max_len: int = 64,
    special_keys: Optional[dict[int, str]] = None,
) -> str:
    """Tiny curses input — collects keystrokes until ENTER or ESC."""
    buf = ""
    curses.curs_set(1)
    try:
        while True:
            ch = _getch_with_testing_mode(stdscr)
            if ch == TESTING_MODE_SENTINEL:
                return TESTING_MODE_SENTINEL
            if special_keys and ch in special_keys:
                return special_keys[ch]
            if ch in (10, 13, curses.KEY_ENTER):
                return buf
            if ch in (27,):  # ESC
                return ""
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf = buf[:-1]
                    _safe_addstr(stdscr, y, x + len(buf), " ")
                    stdscr.move(y, x + len(buf))
                    stdscr.refresh()
                continue
            if 32 <= ch < 127 and len(buf) < max_len:
                buf += chr(ch)
                _safe_addstr(stdscr, y, x + len(buf) - 1, "*" if mask else chr(ch))
                stdscr.refresh()
    finally:
        curses.curs_set(0)


def screen_lock_admin_override(stdscr, cfg: dict, locks_summary: str) -> Optional[dict]:
    """Operator typed 'O' on the HALT screen. Capture admin email + password,
    POST to /imaging/lock-override-verify, return the {user_id, user_name,
    role} dict on success, None on cancel/failure.
    """
    while True:
        stdscr.erase()
        draw_header(stdscr, "Admin Override — Authorize lock bypass")
        h, w = stdscr.getmaxyx()
        center_block(stdscr, [
            ("This unit is reporting:", curses.color_pair(DIM_PAIR)),
            (locks_summary[:max(20, w - 12)],
             curses.A_BOLD | curses.color_pair(RED_PAIR)),
            ("", 0),
            ("Admin / Supervisor credentials are required to proceed.",
             curses.A_BOLD),
            ("This will be permanently logged as LOCKED-OVERRIDE.",
             curses.color_pair(YELLOW_PAIR)),
        ], top_offset=3)

        prompt_y = 11
        _safe_addstr(stdscr, prompt_y,     6, "Email    : ", curses.A_BOLD)
        _safe_addstr(stdscr, prompt_y + 2, 6, "Password : ", curses.A_BOLD)
        draw_footer(stdscr, "ENTER submit each field   ESC cancel override")
        stdscr.refresh()

        stdscr.move(prompt_y, 17)
        email = _read_input_line(stdscr, prompt_y, 17, mask=False)
        if not email:
            return None
        stdscr.move(prompt_y + 2, 17)
        pwd = _read_input_line(stdscr, prompt_y + 2, 17, mask=True)
        if not pwd:
            return None

        # Hit the verify endpoint
        base = cfg.get("VSTL_API_BASE", "").rstrip("/")
        key = cfg.get("VSTL_API_KEY", "")
        if not base or not key:
            _show_message(stdscr, "Cannot reach API — VSTL_API_BASE/KEY missing.",
                          color=RED_PAIR, secs=3)
            return None
        try:
            req = urllib.request.Request(
                f"{base}/imaging/lock-override-verify",
                data=json.dumps({"email": email, "password": pwd}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": key,
                    "User-Agent": BENCH_USER_AGENT,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
                if data.get("ok"):
                    data["timestamp"] = datetime.now(timezone.utc).isoformat()
                    return data
        except urllib.error.HTTPError as e:
            try:
                err_msg = json.loads(e.read().decode("utf-8")).get("detail", e.reason)
            except (ValueError, OSError):
                err_msg = f"HTTP {e.code} {e.reason}"
            _show_message(stdscr, f"Override failed: {err_msg}",
                          color=RED_PAIR, secs=3)
        except (urllib.error.URLError, OSError) as e:
            _show_message(stdscr, f"Network error: {e}", color=RED_PAIR, secs=3)
        # On failure, loop and let them retry
        if not _confirm_yn(stdscr, "Try again?"):
            return None


def _show_message(stdscr, msg: str, color: int = DIM_PAIR, secs: int = 2) -> None:
    stdscr.erase()
    draw_header(stdscr, "Message")
    center_block(stdscr, [(msg, curses.A_BOLD | curses.color_pair(color))])
    draw_footer(stdscr, f"Continuing in {secs}s…")
    stdscr.refresh()
    time.sleep(secs)


def _confirm_yn(stdscr, prompt: str) -> bool:
    stdscr.erase()
    draw_header(stdscr, "Confirm")
    center_block(stdscr, [(prompt, curses.A_BOLD),
                          ("", 0),
                          ("Y = yes   N = no", curses.color_pair(DIM_PAIR))])
    draw_footer(stdscr, "Press Y or N")
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N")):
            return False


MDM_LOCK_KEYS = {"intune", "azure_ad", "vendor_mdm"}
L1_SKIPPABLE_LOCK_KEYS = MDM_LOCK_KEYS | {"bios_password", "ata_security"}


def _detected_lock_keys(audit: dict) -> set[str]:
    return set(audit.get("detected_locks") or [])


def _l1_can_continue_lock_audit(audit: dict) -> bool:
    """L1 can skip only MDM/join, BIOS, and drive-password findings.

    Computrace and any other active lock classes still use the strict halt
    path. L2 never receives this skip action.
    """
    detected = _detected_lock_keys(audit)
    return bool(detected) and detected.issubset(L1_SKIPPABLE_LOCK_KEYS)


def screen_lock_halt(stdscr, audit: dict, technician: str = "L2") -> str:
    """RED halt screen. Operator picks one of:
        K - Skip warning (L1 only, MDM/join, BIOS, and/or drive-password findings)
        O — Admin Override (proceeds with LOCKED-OVERRIDE tag)
        S — Submit halt + power off (no override; unit is set aside)
        Q — Drop to shell (technical bypass)
    Returns one of {"continue_l1", "override", "submit_halt", "shell"}.
    """
    tech = str(technician).upper()
    detected = _detected_lock_keys(audit)
    has_mdm_signal = bool(detected & MDM_LOCK_KEYS)
    can_l1_continue = tech == "L1" and _l1_can_continue_lock_audit(audit)
    mdm_strict_halt = has_mdm_signal and tech != "L1"
    show_override_actions = not can_l1_continue and not mdm_strict_halt
    while True:
        stdscr.erase()
        draw_header(stdscr, "Phase 2A — UNIT IS LOCKED")
        h, w = stdscr.getmaxyx()
        lines = [
            ("⚠  HALT — Lock or MDM enrollment detected",
             curses.A_BOLD | curses.color_pair(RED_PAIR)),
            ("", 0),
        ]
        for key in la.LOCK_KEYS_ORDER:
            c = audit["checks"][key]
            if c["present"]:
                lines.append((
                    f"✗  {la.LOCK_LABELS[key]:<32}  {c['status']}",
                    curses.A_BOLD | curses.color_pair(RED_PAIR),
                ))
                if c.get("evidence"):
                    lines.append((
                        f"   {c['evidence'][:max(30, w - 12)]}",
                        curses.color_pair(DIM_PAIR),
                    ))
        lines.append(("", 0))
        if can_l1_continue:
            lines.append((
                "[ K ]  L1 skip MDM/BIOS/drive-lock warning and continue",
                curses.A_BOLD | curses.color_pair(YELLOW_PAIR),
            ))
            lines.append((
                "[ S ]  Submit halt to server and power off (set unit aside)",
                curses.color_pair(DIM_PAIR),
            ))
            lines.append(("", 0))
        elif mdm_strict_halt:
            lines.append((
                "MDM/Join enrollment requires halt for L2. Continue and override are disabled.",
                curses.A_BOLD | curses.color_pair(RED_PAIR),
            ))
            lines.append((
                "[ S ]  Submit halt to server and power off (set unit aside)",
                curses.color_pair(DIM_PAIR),
            ))
        else:
            lines.append((
                "[ O ]  Admin override - proceed with LOCKED-OVERRIDE audit log",
                curses.A_BOLD | curses.color_pair(YELLOW_PAIR),
            ))
            lines.append((
                "[ S ]  Submit halt to server and power off (set unit aside)",
                curses.color_pair(DIM_PAIR),
            ))
            lines.append((
                "[ Q ]  Drop to shell  (technical bypass - no audit log)",
                curses.color_pair(DIM_PAIR),
            ))
        center_block(stdscr, lines, top_offset=2)
        if can_l1_continue:
            footer = "Press K / S"
        elif mdm_strict_halt:
            footer = "Press S"
        else:
            footer = "Press O / S / Q"
        draw_footer(stdscr, footer)
        stdscr.refresh()

        ch = stdscr.getch()
        if can_l1_continue and ch in (ord("k"), ord("K")):
            return "continue_l1"
        if ch in (ord("s"), ord("S")):
            return "submit_halt"
        if show_override_actions and ch in (ord("o"), ord("O")):
            return "override"
        if show_override_actions and ch in (ord("q"), ord("Q")):
            return "shell"


# ---------------------------------------------------------------------------
# Phase 2B — Interactive QC test screens (Display, Keyboard, Camera,
# Fingerprint, Speaker, Microphone, Touchscreen, Ports). Per design 1b the
# layer-gating happens at the controller level — these helpers don't care.
# ---------------------------------------------------------------------------
def _qc_intro(stdscr, layer: str, test_count: int) -> None:
    stdscr.erase()
    draw_header(stdscr, "Phase 2B — Interactive QC Tests")
    center_block(stdscr, [
        (f"Technician layer: {layer}", curses.color_pair(DIM_PAIR)),
        ("", 0),
        (f"You are about to run {test_count} interactive QC tests.",
         curses.A_BOLD),
        ("", 0),
        ("L1 = flexible (failures recorded with remarks, unit moves on).",
         curses.color_pair(DIM_PAIR)),
        ("L2 = strict (any failure routes the unit back for rework).",
         curses.color_pair(YELLOW_PAIR)),
        ("", 0),
        ("ENTER  start the QC test", curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
    ], top_offset=4)
    draw_footer(stdscr, "ENTER continue   Q quit")
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return
        if ch in (ord("q"), ord("Q")):
            sys.exit(0)


def _qc_remarks_dialog(stdscr, prompt: str) -> str:
    """Single-line text capture for fail remarks."""
    stdscr.erase()
    draw_header(stdscr, "Add Remarks")
    h, w = stdscr.getmaxyx()
    center_block(stdscr, [
        (prompt, curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
        ("", 0),
        ("Type a brief note for the audit log, then ENTER.",
         curses.color_pair(DIM_PAIR)),
        ("(ESC = leave blank)", curses.color_pair(DIM_PAIR)),
    ], top_offset=4)
    _safe_addstr(stdscr, 11, 6, "Remarks: ", curses.A_BOLD)
    draw_footer(stdscr, "ENTER submit   ESC blank")
    stdscr.refresh()
    return _read_input_line(stdscr, 11, 15, mask=False, max_len=120)


COSMETIC_GRADES = ("A+", "A", "B", "C", "D")
TRIPLE_ENTER_WINDOW_SEC = 1.8
POST_QC_BACK_TO_QC = "__BACK_TO_QC__"


def _post_qc_csv_from_lines(lines: list[str]) -> str:
    """Normalize multiline post-QC entries into a stable comma list."""
    values: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        value = " ".join(str(raw).strip().split())
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return ", ".join(values)


def _post_qc_grade_screen(stdscr, allow_back_to_qc: bool = False) -> str:
    selected = 0
    while True:
        stdscr.erase()
        draw_header(stdscr, "Post-QC - Cosmetic Grading")
        lines: list[tuple[str, int]] = [
            ("Select exactly one cosmetic grade for this unit.", curses.A_BOLD),
            ("No custom grades and no multiple selection.", curses.color_pair(DIM_PAIR)),
            ("", 0),
        ]
        for idx, grade in enumerate(COSMETIC_GRADES):
            marker = ">" if idx == selected else " "
            attr = curses.A_BOLD | curses.color_pair(GREEN_PAIR) if idx == selected else curses.A_NORMAL
            lines.append((f"{marker} {idx + 1}. {grade}", attr))
        if allow_back_to_qc:
            lines.extend([
                ("", 0),
                ("[ B ]  Go back to previous QC test", curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
            ])
        center_block(stdscr, lines, top_offset=4)
        footer = "UP/DOWN select   1-5 quick select   ENTER confirm"
        if allow_back_to_qc:
            footer += "   B back"
        draw_footer(stdscr, footer)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k"), ord("K")):
            selected = (selected - 1) % len(COSMETIC_GRADES)
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            selected = (selected + 1) % len(COSMETIC_GRADES)
        elif allow_back_to_qc and ch in (ord("b"), ord("B")):
            return POST_QC_BACK_TO_QC
        elif ch in (10, 13, curses.KEY_ENTER):
            return COSMETIC_GRADES[selected]
        elif ord("1") <= ch <= ord("5"):
            return COSMETIC_GRADES[ch - ord("1")]


def _post_qc_multiline_screen(stdscr, title: str, prompt: str) -> str:
    lines: list[str] = []
    empty_count = 0
    first_empty_at = 0.0
    current = ""
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    while True:
        stdscr.erase()
        draw_header(stdscr, title)
        h, w = stdscr.getmaxyx()
        help_lines = [
            (prompt, curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
            ("Type one line, press ENTER, then type the next line.", curses.color_pair(DIM_PAIR)),
            ("Press ENTER three times rapidly on a blank line to finish.", curses.color_pair(DIM_PAIR)),
            ("Duplicates and blank lines will be removed automatically.", curses.color_pair(DIM_PAIR)),
            ("", 0),
        ]
        preview = _post_qc_csv_from_lines(lines)
        if preview:
            help_lines.append((f"Saved so far: {preview[: max(10, w - 20)]}", curses.color_pair(CYAN_PAIR)))
            help_lines.append(("", 0))
        center_block(stdscr, help_lines, top_offset=3)
        y = min(h - 4, 11 + min(len(lines), max(0, h - 18)))
        _safe_addstr(stdscr, y, 4, "> " + current[: max(1, w - 8)], curses.A_BOLD)
        draw_footer(stdscr, "ENTER add line   blank ENTER x3 rapidly = done")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            value = current.strip()
            now = time.monotonic()
            if value:
                lines.append(value)
                current = ""
                empty_count = 0
                first_empty_at = 0.0
            else:
                if empty_count == 0 or now - first_empty_at > TRIPLE_ENTER_WINDOW_SEC:
                    empty_count = 1
                    first_empty_at = now
                else:
                    empty_count += 1
                if empty_count >= 3:
                    try:
                        curses.curs_set(0)
                    except curses.error:
                        pass
                    return _post_qc_csv_from_lines(lines)
            continue
        if ch in (27,):
            current = ""
            empty_count = 0
            first_empty_at = 0.0
            continue
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            current = current[:-1]
            empty_count = 0
            first_empty_at = 0.0
            continue
        if 32 <= ch <= 126 and len(current) < 160:
            current += chr(ch)
            empty_count = 0
            first_empty_at = 0.0


def screen_post_qc_step_complete(
    stdscr,
    label: str,
    value: str,
    *,
    has_previous_post_qc: bool,
    allow_back_to_qc: bool,
) -> str:
    """Post-QC keyboard navigation gate.

    Returns "next", "retest", "back", or "back_to_qc".
    """
    preview = value if value else "(blank)"
    if len(preview) > 90:
        preview = preview[:87] + "..."
    while True:
        stdscr.erase()
        draw_header(stdscr, f"Post-QC - {label} Saved")
        lines: list[tuple[str, int]] = [
            (f"{label}: {preview}", curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
            ("", 0),
            ("[ ENTER ]  Continue to next step", curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
            ("[ R ]      Re-enter this step", curses.A_BOLD | curses.color_pair(CYAN_PAIR)),
        ]
        if has_previous_post_qc:
            lines.append((
                "[ B ]      Go back to previous post-QC step",
                curses.A_BOLD | curses.color_pair(YELLOW_PAIR),
            ))
        elif allow_back_to_qc:
            lines.append((
                "[ B ]      Go back to previous QC test",
                curses.A_BOLD | curses.color_pair(YELLOW_PAIR),
            ))
        else:
            lines.append((
                "[ B ]      Back not available on first post-QC step",
                curses.color_pair(DIM_PAIR),
            ))
        center_block(stdscr, lines, top_offset=5)
        draw_footer(stdscr, "ENTER continue   R re-enter   B back")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return "next"
        if ch in (ord("r"), ord("R")):
            return "retest"
        if ch in (ord("b"), ord("B")):
            if has_previous_post_qc:
                return "back"
            if allow_back_to_qc:
                return "back_to_qc"


def screen_post_qc_details(stdscr, allow_back_to_qc: bool = False) -> dict:
    """Collect mandatory post-QC grading, parts, and remarks details."""
    steps = [
        (
            "cosmetic_grade",
            "Cosmetic Grade",
            lambda: _post_qc_grade_screen(stdscr, allow_back_to_qc=allow_back_to_qc),
        ),
        (
            "parts_required",
            "Parts Required",
            lambda: _post_qc_multiline_screen(
                stdscr,
                "Post-QC - Parts Required",
                "Enter required parts for this laptop/system.",
            ),
        ),
        (
            "additional_remarks",
            "Additional Remarks",
            lambda: _post_qc_multiline_screen(
                stdscr,
                "Post-QC - Additional Remarks",
                "Enter additional remarks for this laptop/system.",
            ),
        ),
    ]
    details: dict[str, str] = {}
    index = 0
    while index < len(steps):
        key, label, screen_fn = steps[index]
        value = screen_fn()
        if value == POST_QC_BACK_TO_QC:
            return {"_nav": "back_to_qc"}
        details[key] = value
        action = screen_post_qc_step_complete(
            stdscr,
            label,
            value,
            has_previous_post_qc=index > 0,
            allow_back_to_qc=allow_back_to_qc and index == 0,
        )
        if action == "back_to_qc":
            return {"_nav": "back_to_qc"}
        if action == "back":
            index = max(0, index - 1)
            continue
        if action == "retest":
            continue
        index += 1
    return details


def _qc_pass_fail_choice(stdscr, label: str, prompt_lines: list[str],
                          allow_skip: bool) -> tuple[str, str]:
    """Ask operator PASS / FAIL / RETEST / (optional SKIP).
    
    Returns (verdict, remarks). verdict is "PASS" | "FAIL" | "RETEST" | "SKIP".
    The caller is responsible for re-running the actual test logic when
    verdict == "RETEST" (added 2026-05-11 per ops request: every QC test
    must support a retest cycle without losing the operator's place).
    """
    while True:
        stdscr.erase()
        draw_header(stdscr, f"QC — {label}")
        lines: list[tuple[str, int]] = []
        for ln in prompt_lines:
            lines.append((ln, curses.A_BOLD))
        lines += [
            ("", 0),
            ("[ P ]  PASS — works as expected",
             curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
            ("[ F ]  FAIL — defective / not working",
             curses.A_BOLD | curses.color_pair(RED_PAIR)),
            ("[ R ]  RETEST — run this test again",
             curses.A_BOLD | curses.color_pair(CYAN_PAIR)),
        ]
        if allow_skip:
            lines.append((
                "[ S ]  SKIP — note as remark, do not test (L1 only)",
                curses.color_pair(YELLOW_PAIR),
            ))
        center_block(stdscr, lines, top_offset=3)
        draw_footer(stdscr, "Press P / F / R" + (" / S" if allow_skip else ""))
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("p"), ord("P")):
            return "PASS", ""
        if ch in (ord("f"), ord("F")):
            remarks = _qc_remarks_dialog(stdscr, f"Why did {label} fail?")
            return "FAIL", remarks or "no remarks"
        if ch in (ord("r"), ord("R")):
            return "RETEST", ""
        if allow_skip and ch in (ord("s"), ord("S")):
            remarks = _qc_remarks_dialog(stdscr, f"Why are you skipping {label}?")
            return "SKIP", remarks or "no remarks"


def _qc_run_with_retest(stdscr, test_name: str, allow_skip: bool,
                         run_test_fn, prompt_lines: list[str]) -> dict:
    """Standard 'run + verdict' loop with [R]etest support.
    
    `run_test_fn(stdscr)` runs the actual test (color cycle, key capture,
    play tone, camera preview, etc.) and returns an evidence string. The
    operator is then prompted PASS/FAIL/RETEST/SKIP. On RETEST we re-run
    run_test_fn and re-prompt. Retest count is captured in the evidence.
    """
    retries = 0
    last_evidence = ""
    while True:
        evidence = run_test_fn(stdscr)
        if evidence:
            last_evidence = evidence
        if test_name == "display":
            _restore_curses_after_framebuffer(stdscr, "qc_display_verdict")
        verdict, remarks = _qc_pass_fail_choice(
            stdscr, qc.TEST_LABELS.get(test_name, test_name.replace("_", " ").title()),
            prompt_lines, allow_skip,
        )
        if verdict != "RETEST":
            ev = last_evidence
            if retries > 0:
                ev = f"{ev}  (retested {retries}x)"
            return qc.make_result(
                test_name, applicable=True, evidence=ev,
                ran=True, result=verdict, remarks=remarks,
            )
        retries += 1


def screen_qc_step_complete(stdscr, result: dict, has_previous: bool) -> str:
    """Post-test navigation gate for keyboard-only bench correction.

    Operators can correct the current test with R, or go back one completed
    QC test with B. The caller owns the test index so results are replaced
    instead of duplicated.
    """
    label = str(result.get("label") or result.get("key") or "QC Test")
    verdict = str(result.get("result") or "UNKNOWN").upper()
    remarks = str(result.get("remarks") or "").strip()
    if verdict == "PASS":
        verdict_attr = curses.A_BOLD | curses.color_pair(GREEN_PAIR)
    elif verdict == "FAIL":
        verdict_attr = curses.A_BOLD | curses.color_pair(RED_PAIR)
    elif verdict == "SKIP":
        verdict_attr = curses.A_BOLD | curses.color_pair(YELLOW_PAIR)
    else:
        verdict_attr = curses.A_BOLD | curses.color_pair(DIM_PAIR)

    while True:
        stdscr.erase()
        draw_header(stdscr, f"QC - {label} Complete")
        lines: list[tuple[str, int]] = [
            (f"{label}: {verdict}", verdict_attr),
        ]
        if remarks:
            lines.extend([
                ("", 0),
                (f"Remarks: {remarks[:80]}", curses.color_pair(DIM_PAIR)),
            ])
        lines.extend([
            ("", 0),
            ("[ ENTER ]  Continue to next test", curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
            ("[ R ]      Re-perform this test", curses.A_BOLD | curses.color_pair(CYAN_PAIR)),
        ])
        if has_previous:
            lines.append((
                "[ B ]      Go Back to Previous Test",
                curses.A_BOLD | curses.color_pair(YELLOW_PAIR),
            ))
        else:
            lines.append((
                "[ B ]      Go Back to Previous Test (not available on first test)",
                curses.color_pair(DIM_PAIR),
            ))
        center_block(stdscr, lines, top_offset=4)
        footer = "ENTER next   R redo current"
        if has_previous:
            footer += "   B previous test"
        draw_footer(stdscr, footer)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return "next"
        if ch in (ord("r"), ord("R")):
            return "retest"
        if has_previous and ch in (ord("b"), ord("B")):
            return "back"


def _qc_color_cycle(stdscr, secs: int = 6) -> str:
    """Display test — actually fill the screen with R/G/B/W via /dev/fb0.
    
    2026-05-11 fix: the previous curses-A_REVERSE approach only coloured
    text cells (operator reported "only shows text, no coloured screen").
    Direct framebuffer writes give the operator a true full-screen panel
    for dead-pixel / backlight inspection. Curses is endwin'd while
    writing, then restored for the verdict screen.
    """
    h, w = stdscr.getmaxyx()
    # 2026-05-14 Bug #6: founder asked to ADD a full black screen step to
    # the LCD test (helps spot stuck-on / always-lit pixels). Black sits
    # at the END of the cycle so the operator finishes on a dark screen
    # before the verdict prompt — also matches the existing black reset.
    colors = ["RED", "GREEN", "BLUE", "WHITE", "BLACK"]

    # Try true full-screen via /dev/fb0 first
    fb_ok = _fb0_fill("BLACK")
    if fb_ok:
        previous_cursor = None
        graphics_mode = False
        previous_nodelay = False
        try:
            try:
                previous_cursor = curses.curs_set(0)
            except curses.error:
                previous_cursor = None
            stdscr.nodelay(True)
            _set_console_cursor_visible(False)
            _physical_blank_tty(stdscr)
            graphics_mode = _set_console_graphics_mode(True)
            for name in colors:
                _fb0_fill(name)
                while True:
                    ch = stdscr.getch()
                    if ch in (10, 13, curses.KEY_ENTER):
                        break
                    time.sleep(0.04)
            _fb0_fill("BLACK")
        finally:
            if graphics_mode:
                _set_console_graphics_mode(False)
            _set_console_cursor_visible(True)
            stdscr.nodelay(previous_nodelay)
            _restore_curses_after_framebuffer(stdscr, "qc_display_color_done")
            if previous_cursor is not None:
                try:
                    curses.curs_set(previous_cursor)
                except curses.error:
                    pass
        return "fb0 color cycle: R/G/B/W/B full-screen; touch input suppressed"
        try:
            curses.endwin()
            for name in colors:
                _fb0_fill(name)
                # Print short label centered in framebuffer-text terminal
                sys.stdout.write(
                    f"\x1b[H\x1b[1;97m  {name} — look for dead pixels — ENTER  \x1b[0m\n"
                )
                sys.stdout.flush()
                try:
                    sys.stdin.readline()  # wait for ENTER
                except (KeyboardInterrupt, OSError):
                    break
            _fb0_fill("BLACK")
        finally:
            # Re-enter curses cleanly
            stdscr.refresh()
            curses.curs_set(0)
        return "fb0 color cycle: R/G/B/W full-screen"

    # Fallback to curses-coloured cells when /dev/fb0 is unavailable
    color_pairs = [
        (curses.COLOR_RED,    "RED"),
        (curses.COLOR_GREEN,  "GREEN"),
        (curses.COLOR_BLUE,   "BLUE"),
        (curses.COLOR_WHITE,  "WHITE"),
        (curses.COLOR_BLACK,  "BLACK"),
    ]
    for i, (color, name) in enumerate(color_pairs):
        pair_idx = 50 + i
        try:
            curses.init_pair(pair_idx, curses.COLOR_BLACK, color)
        except curses.error:
            continue
        stdscr.erase()
        attr = curses.color_pair(pair_idx) | curses.A_REVERSE
        for y in range(h):
            try:
                stdscr.addstr(y, 0, " " * (w - 1), attr)
            except curses.error:
                pass
        msg = f"  {name}  —  look for dead pixels, ENTER to advance  "
        try:
            stdscr.addstr(h // 2, max(0, (w - len(msg)) // 2), msg,
                          curses.A_BOLD | curses.A_REVERSE)
        except curses.error:
            pass
        stdscr.refresh()
        _wait_with_skip(stdscr, max(2, secs // len(color_pairs)))
    return "curses fallback color cycle"


def screen_qc_display(stdscr) -> dict:
    return _qc_run_with_retest(
        stdscr, "display", allow_skip=False,
        run_test_fn=lambda s: _qc_color_cycle(s, secs=6),
        prompt_lines=[
            "Did all 5 colours (R / G / B / WHITE / BLACK) fill the screen",
            "with NO dead pixels, stuck-on pixels, vertical lines, or backlight bleed?",
        ],
    )


def _keyboard_select_menu(
    stdscr,
    subtitle: str,
    question: str,
    options: list[tuple[str, object]],
):
    """Reusable full-screen selector for the keyboard-profile wizard."""
    selected = 0
    offset = 0
    while True:
        _begin_screen_frame(stdscr, subtitle)
        h, _w = stdscr.getmaxyx()
        _safe_addstr(stdscr, 3, 4, question, curses.A_BOLD)
        visible_count = max(4, h - 9)
        if selected < offset:
            offset = selected
        elif selected >= offset + visible_count:
            offset = selected - visible_count + 1

        visible = options[offset:offset + visible_count]
        for row, (label, _value) in enumerate(visible):
            idx = offset + row
            _draw_selectable_row(
                stdscr,
                5 + row,
                6,
                f"{idx + 1}. {label}",
                idx == selected,
            )
        if offset:
            _safe_addstr(stdscr, 4, 6, "^ more options above", curses.color_pair(DIM_PAIR))
        if offset + visible_count < len(options):
            _safe_addstr(
                stdscr,
                h - 3,
                6,
                "v more options below",
                curses.color_pair(DIM_PAIR),
            )
        draw_footer(stdscr, "UP/DOWN select   1-9 jump   ENTER confirm")
        stdscr.refresh()

        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(options)
        elif ch in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(options)
        elif ord("1") <= ch <= ord("9"):
            idx = ch - ord("1")
            if idx < len(options):
                selected = idx
        elif ch in (10, 13, curses.KEY_ENTER):
            _drain_pending_input(stdscr)
            return options[selected][1]


def _keyboard_custom_format_entry(stdscr) -> str:
    """Collect a printed keyboard format not present in the standard list."""
    while True:
        _begin_screen_frame(stdscr, "Keyboard profile 4/6 - Other printed format")
        center_block(stdscr, [
            ("Enter the printed keyboard language or format.", curses.A_BOLD),
            ("Example: DANISH, ITALIAN, CANADIAN FRENCH", curses.color_pair(DIM_PAIR)),
        ], top_offset=4)
        _safe_addstr(stdscr, 9, 6, "Format: ", curses.A_BOLD)
        draw_footer(stdscr, "Type format name   ENTER confirm")
        stdscr.move(9, 14)
        stdscr.refresh()
        value = qc.normalize_keyboard_custom_format(
            _read_input_line(stdscr, 9, 14, mask=False, max_len=40)
        )
        if value != "OTHER":
            _drain_pending_input(stdscr)
            return value


def screen_keyboard_profile_wizard(stdscr) -> dict:
    """Ask the technician which physical keyboard should be tested."""
    while True:
        has_numpad = bool(_keyboard_select_menu(
            stdscr,
            "Keyboard profile 1/6 - Numeric keypad",
            "Choose the keyboard numeric type:",
            [
                ("With numeric keypad", True),
                ("Without numeric keypad", False),
            ],
        ))
        physical_layout = str(_keyboard_select_menu(
            stdscr,
            "Keyboard profile 2/6 - Physical layout",
            "Choose the physical keyboard layout:",
            [
                ("US ANSI layout", "US ANSI"),
                ("UK / European ISO layout", "UK ISO"),
            ],
        ))
        arrangement = str(_keyboard_select_menu(
            stdscr,
            "Keyboard profile 3/6 - Key arrangement",
            "Choose the printed letter arrangement:",
            [
                ("QWERTY", "QWERTY"),
                ("AZERTY", "AZERTY"),
                ("QWERTZ", "QWERTZ"),
            ],
        ))
        print_format = str(_keyboard_select_menu(
            stdscr,
            "Keyboard profile 4/6 - Printed format",
            "Choose the keyboard printed language / format:",
            [
                ("US", "US"),
                ("UK", "UK"),
                ("German", "GERMAN"),
                ("Belgian", "BELGIAN"),
                ("British", "BRITISH"),
                ("Spanish", "SPANISH"),
                ("Polish", "POLISH"),
                ("Swedish", "SWEDISH"),
                ("Swiss", "SWISS"),
                ("French", "FRENCH"),
                ("US with Arabic print", "US WITH ARABIC PRINT"),
                ("Other - type manually", "OTHER"),
            ],
        ))
        custom_format = ""
        if print_format == "OTHER":
            custom_format = _keyboard_custom_format_entry(stdscr)

        has_pointing_stick = bool(_keyboard_select_menu(
            stdscr,
            "Keyboard profile 5/6 - Pointing stick",
            "Choose pointing stick / joystick availability:",
            [
                ("With pointing stick / joystick", True),
                ("Without pointing stick / joystick", False),
            ],
        ))
        has_backlight = bool(_keyboard_select_menu(
            stdscr,
            "Keyboard profile 6/6 - Backlight",
            "Choose keyboard backlight availability:",
            [
                ("With back light", True),
                ("Without back light", False),
            ],
        ))

        profile = qc.build_keyboard_profile(
            has_numpad=has_numpad,
            physical_layout=physical_layout,
            arrangement=arrangement,
            print_format=print_format,
            custom_format=custom_format,
            has_pointing_stick=has_pointing_stick,
            has_backlight=has_backlight,
        )

        while True:
            _begin_screen_frame(stdscr, "Confirm keyboard profile")
            center_block(stdscr, [
                (profile["name"], curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
                ("", 0),
                (f"Physical layout : {profile['physical_layout']}", curses.A_BOLD),
                (f"Numeric keypad  : {profile['numeric']}", curses.A_BOLD),
                (f"Pointing stick  : {profile['pointing_stick']}", curses.A_BOLD),
                ("", 0),
                ("[ ENTER ] Start keyboard test", curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
                ("[ R ] Redo keyboard selections", curses.color_pair(YELLOW_PAIR)),
            ], top_offset=4)
            draw_footer(stdscr, "ENTER start keyboard test   R redo selections")
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (10, 13, curses.KEY_ENTER):
                _drain_pending_input(stdscr)
                return profile
            if ch in (ord("r"), ord("R")):
                _drain_pending_input(stdscr)
                break


def _keyboard_alpha_rows(ecodes, arrangement: str) -> tuple[list[tuple[str, list[int]]], dict[int, str]]:
    """Return physical alpha-key rows and labels for QWERTY/AZERTY/QWERTZ."""
    qwerty_rows = [
        ("Alpha 1", [
            ecodes.KEY_Q, ecodes.KEY_W, ecodes.KEY_E, ecodes.KEY_R, ecodes.KEY_T,
            ecodes.KEY_Y, ecodes.KEY_U, ecodes.KEY_I, ecodes.KEY_O, ecodes.KEY_P,
        ]),
        ("Alpha 2", [
            ecodes.KEY_A, ecodes.KEY_S, ecodes.KEY_D, ecodes.KEY_F, ecodes.KEY_G,
            ecodes.KEY_H, ecodes.KEY_J, ecodes.KEY_K, ecodes.KEY_L,
        ]),
        ("Alpha 3", [
            ecodes.KEY_Z, ecodes.KEY_X, ecodes.KEY_C, ecodes.KEY_V,
            ecodes.KEY_B, ecodes.KEY_N, ecodes.KEY_M,
        ]),
    ]
    labels = {
        ecodes.KEY_Q: "Q", ecodes.KEY_W: "W", ecodes.KEY_E: "E",
        ecodes.KEY_R: "R", ecodes.KEY_T: "T", ecodes.KEY_Y: "Y",
        ecodes.KEY_U: "U", ecodes.KEY_I: "I", ecodes.KEY_O: "O",
        ecodes.KEY_P: "P", ecodes.KEY_A: "A", ecodes.KEY_S: "S",
        ecodes.KEY_D: "D", ecodes.KEY_F: "F", ecodes.KEY_G: "G",
        ecodes.KEY_H: "H", ecodes.KEY_J: "J", ecodes.KEY_K: "K",
        ecodes.KEY_L: "L", ecodes.KEY_Z: "Z", ecodes.KEY_X: "X",
        ecodes.KEY_C: "C", ecodes.KEY_V: "V", ecodes.KEY_B: "B",
        ecodes.KEY_N: "N", ecodes.KEY_M: "M",
    }
    arrangement = str(arrangement or "QWERTY").upper()
    if arrangement == "QWERTZ":
        labels[ecodes.KEY_Y] = "Z"
        labels[ecodes.KEY_Z] = "Y"
    elif arrangement == "AZERTY":
        qwerty_rows = [
            ("Alpha 1", [
                ecodes.KEY_Q, ecodes.KEY_W, ecodes.KEY_E, ecodes.KEY_R, ecodes.KEY_T,
                ecodes.KEY_Y, ecodes.KEY_U, ecodes.KEY_I, ecodes.KEY_O, ecodes.KEY_P,
            ]),
            ("Alpha 2", [
                ecodes.KEY_A, ecodes.KEY_S, ecodes.KEY_D, ecodes.KEY_F, ecodes.KEY_G,
                ecodes.KEY_H, ecodes.KEY_J, ecodes.KEY_K, ecodes.KEY_L,
                ecodes.KEY_SEMICOLON,
            ]),
            ("Alpha 3", [
                ecodes.KEY_Z, ecodes.KEY_X, ecodes.KEY_C,
                ecodes.KEY_V, ecodes.KEY_B, ecodes.KEY_N,
            ]),
        ]
        labels[ecodes.KEY_Q] = "A"
        labels[ecodes.KEY_W] = "Z"
        labels[ecodes.KEY_A] = "Q"
        labels[ecodes.KEY_Z] = "W"
        labels[ecodes.KEY_SEMICOLON] = "M"
    return qwerty_rows, labels


_KEYBOARD_RESELECT_SENTINEL = "__KEYBOARD_RESELECT__"


def _kbd_evdev_capture(stdscr, profile: dict) -> str:
    """Full-keyboard evdev capture covers the standard laptop keyboard,
    optional numpad, modifiers, arrows/navigation, F1-F12, and ESC.
    Operator presses each key on a virtual map; pressed keys light up.
    Falls back to stdin-typing if /dev/input/event* isn't readable.

    2026-05-14 fixes:
      • Bug #7: ESC used to be the exit key, so the ESC key itself
        could never be tested. Exit is now **Ctrl+Q** (left-or-right
        Ctrl + Q held together), which frees ESC for testing like any
        other key.
      • Bug "fonts too small": before entering the test we attempt to
        switch the Linux console font to `sun12x22` (or any large font
        available in /usr/share/consolefonts), and restore the original
        font on exit. This makes the whole evdev grid much more
        readable on a bench laptop's framebuffer console.
    """
    try:
        from evdev import InputDevice, list_devices, ecodes  # type: ignore
    except ImportError:
        return ""

    # Switch to a larger console font for readability (best-effort)
    prior_font_restore = None
    for big_font in ("sun12x22", "Lat15-Terminus20x10", "LatGrkCyr-12x22",
                     "Terminus32x16", "Uni3-Terminus28x14"):
        try:
            rc = subprocess.run(
                ["setfont", big_font],
                capture_output=True, timeout=2, check=False,
            )
            if rc.returncode == 0:
                # remember to restore the system default on exit
                prior_font_restore = ["setfont"]  # no args = restore default
                break
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue

    devices = []
    for path in list_devices():
        try:
            d = InputDevice(path)
            caps = d.capabilities()
            if ecodes.EV_KEY in caps:
                # heuristic: a keyboard advertises >50 EV_KEY codes
                if len(caps[ecodes.EV_KEY]) > 50:
                    devices.append(d)
        except (OSError, PermissionError):
            continue
    if not devices:
        if prior_font_restore:
            try:
                subprocess.run(prior_font_restore, capture_output=True,
                               timeout=2, check=False)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass
        return ""

    pressed: set[int] = set()
    held_keys: set[int] = set()
    show_numpad = bool(profile.get("has_numpad"))
    numpad_note = "numpad=shown" if show_numpad else "numpad=hidden"
    profile_evidence = qc.keyboard_profile_evidence(profile)
    numpad_row = (
        "Numpad",
        [ecodes.KEY_KP0, ecodes.KEY_KP1, ecodes.KEY_KP2, ecodes.KEY_KP3,
         ecodes.KEY_KP4, ecodes.KEY_KP5, ecodes.KEY_KP6, ecodes.KEY_KP7,
         ecodes.KEY_KP8, ecodes.KEY_KP9, ecodes.KEY_KPENTER,
         ecodes.KEY_KPPLUS, ecodes.KEY_KPMINUS],
    )
    alpha_rows, alpha_labels = _keyboard_alpha_rows(
        ecodes, str(profile.get("arrangement", "QWERTY"))
    )
    digits_symbols_row = (
        "Digits/Symbols",
        [
            ecodes.KEY_GRAVE,
            ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3, ecodes.KEY_4,
            ecodes.KEY_5, ecodes.KEY_6, ecodes.KEY_7, ecodes.KEY_8,
            ecodes.KEY_9, ecodes.KEY_0, ecodes.KEY_MINUS, ecodes.KEY_EQUAL,
        ],
    )
    bracket_row = (
        "Brackets",
        [ecodes.KEY_LEFTBRACE, ecodes.KEY_RIGHTBRACE, ecodes.KEY_BACKSLASH],
    )
    punctuation_row = (
        "Punctuation",
        [
            ecodes.KEY_SEMICOLON,
            ecodes.KEY_APOSTROPHE,
            ecodes.KEY_COMMA,
            ecodes.KEY_DOT,
            ecodes.KEY_SLASH,
        ],
    )
    # Group keys for the on-screen display
    rows = [
        ("F-keys",      [ecodes.KEY_F1, ecodes.KEY_F2, ecodes.KEY_F3, ecodes.KEY_F4,
                       ecodes.KEY_F5, ecodes.KEY_F6, ecodes.KEY_F7, ecodes.KEY_F8,
                       ecodes.KEY_F9, ecodes.KEY_F10, ecodes.KEY_F11, ecodes.KEY_F12]),
        digits_symbols_row,
        *alpha_rows,
        bracket_row,
        punctuation_row,
        ("Modifiers",[ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT, ecodes.KEY_LEFTCTRL,
                       ecodes.KEY_RIGHTCTRL, ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT,
                       ecodes.KEY_CAPSLOCK, ecodes.KEY_TAB, ecodes.KEY_ENTER,
                       ecodes.KEY_BACKSPACE, ecodes.KEY_SPACE, ecodes.KEY_ESC]),
        ("Arrows/Nav",[ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_LEFT, ecodes.KEY_RIGHT,
                       ecodes.KEY_HOME, ecodes.KEY_END, ecodes.KEY_PAGEUP,
                       ecodes.KEY_PAGEDOWN, ecodes.KEY_INSERT, ecodes.KEY_DELETE]),
    ]
    if show_numpad:
        rows.insert(5, numpad_row)
    if str(profile.get("physical_layout", "")).upper() == "UK ISO":
        rows.insert(-2, ("ISO key", [ecodes.KEY_102ND]))
    all_keys = {k for _, ks in rows for k in ks}

    # Set evdev devices to non-blocking
    import selectors
    sel = selectors.DefaultSelector()
    for d in devices:
        try:
            d.grab()  # exclusive — keys won't leak to console
        except OSError:
            pass
        sel.register(d, selectors.EVENT_READ)

    deadline = time.time() + 120  # max 2 minutes
    # 2026-05-17 Bug #7 final: exit = press ESC TWICE within 1.5 seconds.
    # First press counts ESC as covered; second press inside the window
    # exits the screen. Replaces the Ctrl+Q combo which operators kept
    # forgetting / treating as a keyboard "exit" hotkey on real units.
    ESC_DOUBLE_WINDOW = 1.5  # seconds
    last_esc_ts: float | None = None

    label_map = {
        ecodes.KEY_F1:"F1", ecodes.KEY_F2:"F2", ecodes.KEY_F3:"F3", ecodes.KEY_F4:"F4",
        ecodes.KEY_F5:"F5", ecodes.KEY_F6:"F6", ecodes.KEY_F7:"F7", ecodes.KEY_F8:"F8",
        ecodes.KEY_F9:"F9", ecodes.KEY_F10:"F10", ecodes.KEY_F11:"F11", ecodes.KEY_F12:"F12",
        ecodes.KEY_Q:"Q", ecodes.KEY_W:"W", ecodes.KEY_E:"E", ecodes.KEY_R:"R",
        ecodes.KEY_T:"T", ecodes.KEY_Y:"Y", ecodes.KEY_U:"U", ecodes.KEY_I:"I",
        ecodes.KEY_O:"O", ecodes.KEY_P:"P",
        ecodes.KEY_A:"A", ecodes.KEY_S:"S", ecodes.KEY_D:"D", ecodes.KEY_F:"F",
        ecodes.KEY_G:"G", ecodes.KEY_H:"H", ecodes.KEY_J:"J", ecodes.KEY_K:"K",
        ecodes.KEY_L:"L",
        ecodes.KEY_Z:"Z", ecodes.KEY_X:"X", ecodes.KEY_C:"C", ecodes.KEY_V:"V",
        ecodes.KEY_B:"B", ecodes.KEY_N:"N", ecodes.KEY_M:"M",
        ecodes.KEY_GRAVE:"`",
        ecodes.KEY_1:"1", ecodes.KEY_2:"2", ecodes.KEY_3:"3", ecodes.KEY_4:"4",
        ecodes.KEY_5:"5", ecodes.KEY_6:"6", ecodes.KEY_7:"7", ecodes.KEY_8:"8",
        ecodes.KEY_9:"9", ecodes.KEY_0:"0", ecodes.KEY_MINUS:"-", ecodes.KEY_EQUAL:"=",
        ecodes.KEY_LEFTBRACE:"[", ecodes.KEY_RIGHTBRACE:"]", ecodes.KEY_BACKSLASH:"\\",
        ecodes.KEY_SEMICOLON:";", ecodes.KEY_APOSTROPHE:"'",
        ecodes.KEY_COMMA:",", ecodes.KEY_DOT:".", ecodes.KEY_SLASH:"/",
        ecodes.KEY_KP0:"K0", ecodes.KEY_KP1:"K1", ecodes.KEY_KP2:"K2", ecodes.KEY_KP3:"K3",
        ecodes.KEY_KP4:"K4", ecodes.KEY_KP5:"K5", ecodes.KEY_KP6:"K6", ecodes.KEY_KP7:"K7",
        ecodes.KEY_KP8:"K8", ecodes.KEY_KP9:"K9", ecodes.KEY_KPENTER:"K↵",
        ecodes.KEY_KPPLUS:"K+", ecodes.KEY_KPMINUS:"K-",
        ecodes.KEY_LEFTSHIFT:"LSh", ecodes.KEY_RIGHTSHIFT:"RSh", ecodes.KEY_LEFTCTRL:"LCt",
        ecodes.KEY_RIGHTCTRL:"RCt", ecodes.KEY_LEFTALT:"LAl", ecodes.KEY_RIGHTALT:"RAl",
        ecodes.KEY_CAPSLOCK:"Cap", ecodes.KEY_TAB:"Tab", ecodes.KEY_ENTER:"Ent",
        ecodes.KEY_BACKSPACE:"Bk", ecodes.KEY_SPACE:"Spc", ecodes.KEY_ESC:"Esc",
        ecodes.KEY_UP:"↑", ecodes.KEY_DOWN:"↓", ecodes.KEY_LEFT:"←", ecodes.KEY_RIGHT:"→",
        ecodes.KEY_HOME:"Hm", ecodes.KEY_END:"En", ecodes.KEY_PAGEUP:"PU",
        ecodes.KEY_PAGEDOWN:"PD", ecodes.KEY_INSERT:"Ins", ecodes.KEY_DELETE:"Del",
        ecodes.KEY_102ND:"ISO",
    }
    label_map.update(alpha_labels)

    try:
        while time.time() < deadline:
            # Redraw map
            stdscr.erase()
            draw_header(stdscr, "QC - Keyboard (evdev raw capture)")
            _safe_addstr(
                stdscr,
                3,
                2,
                str(profile.get("name", "KEYBOARD PROFILE")),
                curses.A_BOLD | curses.color_pair(GREEN_PAIR),
            )
            _safe_addstr(
                stdscr,
                4,
                2,
                (
                    f"{profile.get('physical_layout', 'UNKNOWN')} | "
                    f"{profile.get('numeric', 'UNKNOWN')} | "
                    f"{profile.get('pointing_stick', 'UNKNOWN')}"
                ),
                curses.color_pair(DIM_PAIR),
            )
            row_y = 6
            for row_label, keys in rows:
                # Wider label column + spacing between keys for readability
                line = f"  {row_label:14s}: "
                _safe_addstr(stdscr, row_y, 0, line, curses.A_BOLD)
                col_x = len(line)
                source = pressed
                for k in keys:
                    # Wider [ XXX ] cell (4 chars inside) + double space gap
                    label = label_map.get(k, '?')
                    txt = f"[ {label:4s} ]"
                    attr = (curses.color_pair(GREEN_PAIR) | curses.A_BOLD
                            if k in source else curses.color_pair(DIM_PAIR))
                    _safe_addstr(stdscr, row_y, col_x, txt, attr)
                    col_x += len(txt) + 1
                # Extra vertical spacing between rows for the bigger console font
                row_y += 2
            row_y += 1
            covered = len(pressed & all_keys)
            total = len(all_keys)
            _safe_addstr(stdscr, row_y, 2,
                         f"Coverage: {covered}/{total}  —  press ESC twice within 1.5s to exit",
                         curses.A_BOLD | (curses.color_pair(GREEN_PAIR) if covered == total else 0))
            draw_footer(
                stdscr,
                (
                    "Press every displayed key.  ESC ESC = finish  "
                    "Ctrl+Backspace = reselect options"
                ),
            )
            stdscr.refresh()

            # Poll evdev for 0.2 sec
            events = sel.select(timeout=0.2)
            for k, _ in events:
                dev = k.fileobj
                try:
                    for ev in dev.read():
                        if ev.type != ecodes.EV_KEY:
                            continue
                        if ev.value == 0:  # key up
                            held_keys.discard(ev.code)
                            continue
                        if ev.value != 1:  # ignore key repeat
                            continue

                        held_keys.add(ev.code)
                        pressed.add(ev.code)

                        if (
                            ev.code == ecodes.KEY_BACKSPACE
                            and (
                                ecodes.KEY_LEFTCTRL in held_keys
                                or ecodes.KEY_RIGHTCTRL in held_keys
                            )
                        ):
                            return _KEYBOARD_RESELECT_SENTINEL

                        # Exit gesture: double-tap ESC within ESC_DOUBLE_WINDOW
                        if ev.code == ecodes.KEY_ESC:
                            now = time.time()
                            if (
                                last_esc_ts is not None
                                and (now - last_esc_ts) <= ESC_DOUBLE_WINDOW
                            ):
                                return (
                                    f"{profile_evidence}; evdev capture: {numpad_note}; "
                                    f"{len(pressed)} keys pressed; "
                                    f"covered {len(pressed & all_keys)}/{total} target keys"
                                )
                            last_esc_ts = now
                        if all_keys.issubset(pressed):
                            stdscr.refresh()
                            time.sleep(0.4)
                            return (
                                f"{profile_evidence}; keyboard_auto_pass=true; "
                                f"evdev capture: {numpad_note}; "
                                f"covered {total}/{total} target keys"
                            )
                except (OSError, BlockingIOError):
                    pass
    finally:
        for d in devices:
            try:
                d.ungrab()
            except OSError:
                pass
        sel.close()
        # Restore the system console font on exit
        if prior_font_restore:
            try:
                subprocess.run(prior_font_restore, capture_output=True,
                               timeout=2, check=False)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass
    return (
        f"{profile_evidence}; evdev capture timed out: {numpad_note}; "
        f"{len(pressed & all_keys)}/{total} target keys"
    )


def screen_qc_keyboard(stdscr) -> dict:
    """Two-stage keyboard test:
       1) evdev raw key capture (standard keys, F-keys, optional numpad, modifiers)
       2) Operator verdict with retest option.
    Falls back to stdin-typing if evdev unavailable.
    """
    profile = screen_keyboard_profile_wizard(stdscr)

    def run_kbd(s, keyboard_profile: dict):
        evidence = _kbd_evdev_capture(s, keyboard_profile)
        if evidence:
            return evidence
        # Fallback to stdin typing
        s.erase()
        draw_header(s, "QC - Keyboard (typed fallback)")
        center_block(s, [
            (keyboard_profile["name"], curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
            ("", 0),
            ("Type the entire string below into the input field, then ENTER:",
             curses.A_BOLD),
            ("", 0),
            ("ABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789",
             curses.color_pair(GREEN_PAIR) | curses.A_BOLD),
            ("", 0),
            ("If a key is broken, type what you can and mark the test FAIL.",
             curses.color_pair(DIM_PAIR)),
        ], top_offset=3)
        _safe_addstr(s, 13, 6, "Input: ", curses.A_BOLD)
        draw_footer(s, "ENTER submit   ESC abort")
        s.refresh()
        typed = _read_input_line(s, 13, 13, mask=False, max_len=80).upper()
        needed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        missing = sorted({ch for ch in needed if ch not in typed})
        return (
            f"{qc.keyboard_profile_evidence(keyboard_profile)}; "
            f"typed: {typed[:60]}; missing: {''.join(missing) or 'none'}"
        )

    retries = 0
    while True:
        prompts = [
            f"Profile: {profile['name']}",
            "All displayed keys (including ESC, F-keys and modifiers) registered?",
        ]
        if profile["has_backlight"]:
            prompts.append("Did the keyboard back light illuminate correctly?")
        if profile["has_pointing_stick"]:
            prompts.append("Did the pointing stick / joystick operate correctly?")

        evidence = run_kbd(stdscr, profile)
        if evidence == _KEYBOARD_RESELECT_SENTINEL:
            retries += 1
            profile = screen_keyboard_profile_wizard(stdscr)
            continue
        if "keyboard_auto_pass=true" in evidence:
            result = qc.make_result(
                "keyboard", applicable=True, evidence=evidence,
                ran=True, result="PASS", remarks="",
            )
        else:
            verdict, remarks = _qc_pass_fail_choice(
                stdscr, "Keyboard", prompts, allow_skip=False,
            )
            if verdict == "RETEST":
                retries += 1
                continue
            if retries:
                evidence = f"{evidence}  (retested {retries}x)"
            result = qc.make_result(
                "keyboard", applicable=True, evidence=evidence,
                ran=True, result=verdict, remarks=remarks,
            )
        action = _keyboard_result_screen(stdscr, result)
        if action == "retest":
            retries += 1
            continue
        if action == "reselect":
            retries += 1
            profile = screen_keyboard_profile_wizard(stdscr)
            continue
        break
    result["keyboard_profile"] = profile
    result["profile_name"] = profile["name"]
    return result


def _keyboard_result_screen(stdscr, result: dict, hold_seconds: float = 3.0) -> str:
    """Show the keyboard verdict for three seconds; allow retest or reselect."""
    verdict = str(result.get("result") or "FAIL").upper()
    color = GREEN_PAIR if verdict == "PASS" else RED_PAIR
    deadline = time.monotonic() + max(3.0, hold_seconds)
    stdscr.nodelay(True)
    try:
        while True:
            remaining = max(0, int(deadline - time.monotonic() + 0.999))
            stdscr.erase()
            draw_header(stdscr, "QC - Keyboard Result")
            center_block(stdscr, [
                (f"Result: {verdict}",
                 curses.A_BOLD | curses.color_pair(color)),
                ("", 0),
                ("[ R ]  Retest keyboard",
                 curses.A_BOLD | curses.color_pair(CYAN_PAIR)),
                ("[ E ]  Edit / reselect keyboard options",
                 curses.color_pair(YELLOW_PAIR)),
                (f"Continuing in {remaining} second"
                 f"{'s' if remaining != 1 else ''}...",
                 curses.color_pair(DIM_PAIR)),
            ], top_offset=4)
            draw_footer(
                stdscr,
                "R retest   E reselect options   Result remains visible for at least 3 seconds",
            )
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord("r"), ord("R")):
                return "retest"
            if ch in (ord("e"), ord("E")):
                return "reselect"
            if time.monotonic() >= deadline:
                return "continue"
            time.sleep(0.1)
    finally:
        stdscr.nodelay(False)


def _camera_live_preview_legacy_unused(secs: int = 7) -> str:
    """2026-05-13 Phase-2 fix: previously this grabbed a SINGLE still via
    `fswebcam` and displayed that one frame for 5 s via `fbi` — operators
    correctly reported "the camera shows an image but no video". Now we
    try, in order:
      1. `mpv` rendering live /dev/video0 to the framebuffer (`--vo=drm`)
      2. `ffplay` with the framebuffer SDL driver
      3. `v4l2-ctl --stream-mmap` (LED only, no preview)
    The first available player that successfully exits non-zero / hits
    the deadline wins. Returns evidence string describing the path used.
    """
    deadline = max(3, int(secs))
    # 1) mpv → DRM/KMS framebuffer (best path on Clonezilla Live)
    try:
        rc = subprocess.run(
            ["mpv", "--no-config", "--vo=drm", "--really-quiet", "--vf=hflip",
             "--no-audio", "--profile=low-latency",
             f"--length={deadline}",
             "av://v4l2:/dev/video0"],
            capture_output=True, timeout=deadline + 5, check=False,
        )
        if rc.returncode in (0, None):
            return f"camera live video {deadline}s (mpv+drm)"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # 2) ffplay → fbdev SDL output
    try:
        env = dict(os.environ)
        env.setdefault("SDL_VIDEODRIVER", "fbcon")
        env.setdefault("SDL_FBDEV", "/dev/fb0")
        rc = subprocess.run(
            ["ffplay", "-hide_banner", "-loglevel", "error",
             "-f", "v4l2", "-i", "/dev/video0", "-vf", "hflip",
             "-t", str(deadline), "-autoexit", "-an"],
            capture_output=True, timeout=deadline + 5, check=False, env=env,
        )
        if rc.returncode in (0, None):
            return f"camera live video {deadline}s (ffplay+fbcon)"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    # 3) Stream-only fallback (LED on, no preview rendered)
    try:
        subprocess.run(
            ["v4l2-ctl", "--device=/dev/video0",
             "--stream-mmap", f"--stream-count={deadline * 15}"],
            capture_output=True, timeout=deadline + 3, check=False,
        )
        return f"camera LED activated {deadline}s (v4l2-ctl stream — no preview tool found)"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "camera activation attempted (no preview / streaming tools available)"


def _camera_devices() -> list[str]:
    try:
        return [f"/dev/{d}" for d in sorted(os.listdir("/dev")) if d.startswith("video")]
    except OSError:
        return []


def _camera_v4l2_stream_cmd(device: str, width: int, height: int,
                            frames: int, stream_to: str) -> list[str]:
    return [
        "v4l2-ctl", f"--device={device}",
        f"--set-fmt-video=width={width},height={height},pixelformat=YUYV",
        "--stream-mmap=4", f"--stream-count={frames}", f"--stream-to={stream_to}",
    ]


def _camera_read_available_frame(fd: int, frame_size: int, deadline: float) -> bytes:
    frame = bytearray()
    while len(frame) < frame_size and time.time() < deadline:
        try:
            chunk = os.read(fd, frame_size - len(frame))
            if chunk:
                frame.extend(chunk)
                continue
        except BlockingIOError:
            pass
        except OSError:
            break
        time.sleep(0.002)
    return bytes(frame)


def _camera_fb_v4l2_stream_preview(device: str, secs: int) -> str:
    """Stream YUYV frames continuously with v4l2-ctl and render to /dev/fb0."""
    if not shutil.which("v4l2-ctl") or not os.path.exists("/dev/fb0"):
        return ""
    for width, height in ((640, 480), (320, 240)):
        deadline = time.time() + max(3, secs)
        frame_size = width * height * 2
        target_frames = max(30, int(max(3, secs) * 20))
        frames = 0
        proc = None
        fd = None
        pipe_path = f"/tmp/vstl_camera_preview_{os.getpid()}_{width}x{height}.yuyv"
        try:
            try:
                os.unlink(pipe_path)
            except OSError:
                pass
            os.mkfifo(pipe_path, 0o600)
            proc = subprocess.Popen(
                _camera_v4l2_stream_cmd(device, width, height, target_frames, pipe_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            fd = os.open(pipe_path, os.O_RDONLY | os.O_NONBLOCK)
            while time.time() < deadline:
                raw = _camera_read_available_frame(fd, frame_size, deadline)
                if len(raw) < frame_size:
                    break
                if not _fb0_blit_yuyv(raw, width, height, mirror=True):
                    return ""
                frames += 1
                if proc.poll() is not None:
                    break
        except OSError:
            frames = 0
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
            try:
                os.unlink(pipe_path)
            except OSError:
                pass
        if frames >= 10:
            return f"camera live video {frames} frames ({device}, continuous v4l2 YUYV framebuffer)"
    return ""


def _camera_ffmpeg_fb_cmd(
    device: str,
    fb_w: int,
    fb_h: int,
    input_format: str,
    src_w: int,
    src_h: int,
    fps: int,
) -> list[str]:
    video_filter = (
        f"fps={fps},hflip,"
        f"scale={fb_w}:{fb_h}:force_original_aspect_ratio=decrease,"
        f"pad={fb_w}:{fb_h}:(ow-iw)/2:(oh-ih)/2:black"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-thread_queue_size", "64",
        "-f", "v4l2", "-framerate", str(fps),
        "-video_size", f"{src_w}x{src_h}",
    ]
    if input_format:
        cmd.extend(["-input_format", input_format])
    cmd.extend([
        "-i", device, "-an", "-vf", video_filter,
        "-pix_fmt", "bgra", "-f", "rawvideo", "pipe:1",
    ])
    return cmd


def _camera_ffmpeg_fbdev_preview(device: str, secs: int) -> str:
    """Stream V4L2 video directly into the Linux framebuffer with FFmpeg."""
    info = _fb0_geometry()
    if not shutil.which("ffmpeg") or not info or not os.path.exists("/dev/fb0"):
        return ""
    fb_w, fb_h, bpp, stride = info
    if bpp != 32 or stride < fb_w * 4:
        return ""

    duration = max(3, int(secs))
    modes = (
        ("mjpeg", 1280, 720, 25),
        ("mjpeg", 640, 480, 25),
        ("yuyv422", 640, 480, 20),
        ("", 640, 480, 15),
    )
    for input_format, src_w, src_h, fps in modes:
        cmd = _camera_ffmpeg_fb_cmd(
            device, fb_w, fb_h, input_format, src_w, src_h, fps,
        )
        cmd[-5:] = [
            "-pix_fmt", "bgra", "-t", str(duration),
            "-f", "fbdev", "/dev/fb0",
        ]
        started = time.monotonic()
        try:
            rc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=duration + 4,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        elapsed = time.monotonic() - started
        if rc.returncode == 0 and elapsed >= min(2.0, duration * 0.6):
            mode = input_format or "auto"
            return (
                f"camera live video {elapsed:.1f}s "
                f"({device}, ffmpeg {mode} native fbdev)"
            )
    return ""


def _camera_ffmpeg_fb_preview(device: str, secs: int) -> str:
    """Fallback renderer for FFmpeg builds whose fbdev output cannot open."""
    info = _fb0_geometry()
    if not shutil.which("ffmpeg") or not info:
        return ""
    fb_w, fb_h, bpp, _stride = info
    if bpp != 32:
        return ""

    frame_size = fb_w * fb_h * 4
    modes = (
        ("mjpeg", 1280, 720, 25),
        ("mjpeg", 640, 480, 25),
        ("yuyv422", 640, 480, 20),
        ("", 640, 480, 15),
    )
    for input_format, src_w, src_h, fps in modes:
        proc = None
        frames = 0
        deadline = time.time() + max(3, secs)
        try:
            proc = subprocess.Popen(
                _camera_ffmpeg_fb_cmd(
                    device, fb_w, fb_h, input_format, src_w, src_h, fps,
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            if proc.stdout is None:
                continue
            fd = proc.stdout.fileno()
            os.set_blocking(fd, False)
            while time.time() < deadline:
                frame_deadline = min(deadline, time.time() + 2.0)
                raw = _camera_read_available_frame(fd, frame_size, frame_deadline)
                if len(raw) < frame_size:
                    break
                if not _fb0_blit_bgra(raw, fb_w, fb_h):
                    return ""
                frames += 1
                if proc.poll() is not None:
                    break
        except (OSError, ValueError):
            frames = 0
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if frames >= max(12, int(max(3, secs) * 5)):
            mode = input_format or "auto"
            return (
                f"camera live video {frames} frames "
                f"({device}, ffmpeg {mode} direct framebuffer)"
            )
    return ""


def _camera_fb_v4l2_snapshot_preview(device: str, secs: int) -> str:
    """Last-resort fallback: repeated single frames rendered to /dev/fb0."""
    if not shutil.which("v4l2-ctl") or not os.path.exists("/dev/fb0"):
        return ""
    raw_path = "/tmp/vstl_camera_preview.yuyv"
    deadline = time.time() + max(3, secs)
    frames = 0
    width, height = 320, 240
    while time.time() < deadline:
        try:
            try:
                os.unlink(raw_path)
            except OSError:
                pass
            rc = subprocess.run(
                [
                    "v4l2-ctl", f"--device={device}",
                    f"--set-fmt-video=width={width},height={height},pixelformat=YUYV",
                    "--stream-mmap=3", "--stream-count=1", f"--stream-to={raw_path}",
                ],
                capture_output=True, timeout=3, check=False,
            )
            if rc.returncode != 0 or not os.path.exists(raw_path):
                break
            with open(raw_path, "rb") as f:
                raw = f.read(width * height * 2)
            if len(raw) < width * height * 2:
                break
            if not _fb0_blit_yuyv(raw, width, height, mirror=True):
                return ""
            frames += 1
        except (OSError, subprocess.TimeoutExpired):
            break
    if frames >= 2:
        return f"camera preview degraded to {frames} still frames ({device}, v4l2 fallback)"
    return ""


def _camera_fswebcam_fbi_preview(device: str, secs: int) -> str:
    """Fallback live-ish preview: repeated fswebcam frames displayed by fbi."""
    if not (shutil.which("fswebcam") and shutil.which("fbi") and shutil.which("timeout")):
        return ""
    img_path = "/tmp/vstl_camera_preview.jpg"
    deadline = time.time() + max(3, secs)
    frames = 0
    while time.time() < deadline:
        try:
            rc = subprocess.run(
                [
                    "fswebcam", "--quiet", "--no-banner", "--flip", "h", "--frames", "1",
                    "--skip", "1", "--resolution", "640x480",
                    "--device", device, img_path,
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3, check=False,
            )
            if rc.returncode != 0 or not os.path.exists(img_path):
                break
            subprocess.run(
                [
                    "timeout", "0.7s", "fbi", "-T", "1", "-d", "/dev/fb0",
                    "-noverbose", "-a", img_path,
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=1.5, check=False,
            )
            frames += 1
        except (OSError, subprocess.TimeoutExpired):
            break
    if frames >= 2:
        return f"camera live video {frames} frames ({device}, fswebcam/fbi slideshow)"
    return ""


def _camera_player_preview(device: str, secs: int) -> str:
    """Prefer true media-player video renderers when present in the live image."""
    deadline = max(3, int(secs))
    if shutil.which("mpv"):
        try:
            rc = subprocess.run(
                ["mpv", "--no-config", "--vo=drm", "--really-quiet", "--vf=hflip",
                 "--no-audio", "--profile=low-latency", f"--length={deadline}",
                 f"av://v4l2:{device}"],
                capture_output=True, timeout=deadline + 5, check=False,
            )
            if rc.returncode in (0, None):
                return f"camera live video {deadline}s ({device}, mpv+drm)"
        except (subprocess.TimeoutExpired, OSError):
            pass

    if shutil.which("ffplay"):
        try:
            env = dict(os.environ)
            env.setdefault("SDL_VIDEODRIVER", "fbcon")
            env.setdefault("SDL_FBDEV", "/dev/fb0")
            rc = subprocess.run(
                ["ffplay", "-hide_banner", "-loglevel", "error",
                 "-f", "v4l2", "-i", device, "-vf", "hflip", "-t", str(deadline),
                 "-autoexit", "-an"],
                capture_output=True, timeout=deadline + 5, check=False, env=env,
            )
            if rc.returncode in (0, None):
                return f"camera live video {deadline}s ({device}, ffplay+fbcon)"
        except (subprocess.TimeoutExpired, OSError):
            pass
    return ""


def _camera_live_preview(secs: int = 7) -> str:
    """Display true live webcam video on the framebuffer."""
    deadline = max(3, int(secs))
    devices = _camera_devices() or ["/dev/video0"]
    for device in devices:
        evidence = _camera_ffmpeg_fbdev_preview(device, deadline)
        if evidence:
            return evidence
        evidence = _camera_ffmpeg_fb_preview(device, deadline)
        if evidence:
            return evidence
        evidence = _camera_player_preview(device, deadline)
        if evidence:
            return evidence
        evidence = _camera_fb_v4l2_stream_preview(device, deadline)
        if evidence:
            return evidence
        evidence = _camera_fswebcam_fbi_preview(device, deadline)
        if evidence:
            return evidence
        evidence = _camera_fb_v4l2_snapshot_preview(device, deadline)
        if evidence:
            return evidence

    try:
        subprocess.run(
            ["v4l2-ctl", f"--device={devices[0]}", "--stream-mmap",
             f"--stream-count={deadline * 15}"],
            capture_output=True, timeout=deadline + 3, check=False,
        )
        return f"camera stream opened but preview renderer failed ({devices[0]})"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "camera preview failed: no usable preview or stream tool"


def screen_qc_camera(stdscr) -> dict:
    probe = qc.PROBES["camera"]()
    if not probe["applicable"]:
        return qc.na_result("camera", probe["evidence"])

    def run_cam(s):
        s.erase()
        draw_header(s, "QC — Camera (live video)")
        center_block(s, [
            ("Streaming live video from the webcam — watch the screen for ~7 s.",
             curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
            ("", 0),
            ("You should see motion, not a single still image.",
             curses.color_pair(DIM_PAIR)),
        ])
        s.refresh()
        return _camera_live_preview(secs=7)

    return _qc_run_with_retest(
        stdscr, "camera", allow_skip=False,
        run_test_fn=run_cam,
        prompt_lines=[
            "Did the laptop camera show a live moving video feed?",
            "If only the LED came on with no picture, mark this test FAIL.",
        ],
    )


def screen_qc_fingerprint(stdscr) -> dict:
    probe = qc.PROBES["fingerprint"]()
    if not probe["applicable"]:
        return qc.na_result("fingerprint", probe["evidence"])
    if not probe.get("touch_capable"):
        return qc.make_result(
            "fingerprint",
            applicable=True,
            ran=False,
            result="NA",
            evidence=probe["evidence"],
            remarks=(
                "reader present on USB/I2C/SPI/ACPI, but the live Linux "
                "environment has no usable libfprint/event driver"
            ),
        )
    return _run_fingerprint_auto_test(stdscr, probe["evidence"])

    def run_fp(s):
        s.erase()
        draw_header(s, "QC — Fingerprint")
        center_block(s, [
            ("Place your finger on the fingerprint reader.",
             curses.A_BOLD),
            ("", 0),
            ("(LED/blink/click confirms the reader is alive.)",
             curses.color_pair(DIM_PAIR)),
        ])
        s.refresh()
        return probe["evidence"]

    return _qc_run_with_retest(
        stdscr, "fingerprint", allow_skip=False,
        run_test_fn=run_fp,
        prompt_lines=["Did the reader respond (LED/blink/click)?"],
    )


def _start_fprintd_enroll_probe():
    binary = shutil.which("fprintd-enroll")
    if not binary:
        return None, "fprintd-enroll unavailable"
    user = os.environ.get("USER") or "user"
    for cmd in (
        ["dbus-daemon", "--system", "--fork"],
        ["service", "dbus", "start"],
        ["systemctl", "start", "dbus"],
        ["systemctl", "start", "fprintd"],
    ):
        try:
            subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    commands = (
        [binary, "-f", "right-index-finger", user],
        [binary, user],
        [binary],
    )
    last_error = ""
    for command in commands:
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            return proc, "fprintd-enroll started: " + " ".join(command[1:])
        except OSError as exc:
            last_error = str(exc)
            continue
    return None, f"fprintd-enroll start failed: {last_error or 'unknown error'}"


def _fingerprint_fprintd_touch_line(line: str) -> bool:
    normalized = (line or "").lower()
    touch_terms = (
        "enroll-stage-passed",
        "enroll-completed",
        "enroll-complete",
        "verify-match",
        "finger detected",
        "fingerprint captured",
        "scan completed",
        "scan complete",
        "enroll-retry-scan",
        "enroll-swipe-too-short",
        "enroll-finger-not-centered",
        "enroll-remove-and-retry",
        "enroll-duplicate",
        "swipe too short",
        "not centered",
        "remove and retry",
        "scan again",
        "try again",
    )
    return any(term in normalized for term in touch_terms)


def _open_fingerprint_event_devices():
    try:
        from evdev import InputDevice, ecodes  # type: ignore
    except ImportError:
        return [], None, "python-evdev unavailable"

    opened = []
    labels = []
    for item in getattr(qc, "fingerprint_event_devices", lambda: [])():
        path = item.split()[0]
        if not path.startswith("/dev/input/event"):
            continue
        try:
            dev = InputDevice(path)
            try:
                dev.read()
            except (OSError, BlockingIOError):
                pass
            opened.append(dev)
            labels.append(item)
        except (OSError, PermissionError):
            continue
    evidence = "fingerprint evdev=" + " | ".join(labels) if labels else "fingerprint evdev unavailable"
    return opened, ecodes, evidence


def _open_fingerprint_hidraw_devices():
    opened = []
    labels = []
    for item in getattr(qc, "fingerprint_hidraw_devices", lambda: [])():
        path = item.split()[0]
        if not path.startswith("/dev/hidraw"):
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                os.read(fd, 4096)
            except (OSError, BlockingIOError):
                pass
            opened.append((fd, item))
            labels.append(item)
        except (OSError, PermissionError):
            continue
    evidence = "fingerprint hidraw=" + " | ".join(labels) if labels else "fingerprint hidraw unavailable"
    return opened, evidence


def _run_fingerprint_auto_test(stdscr, probe_evidence: str) -> dict:
    import select
    import selectors

    proc, fprintd_evidence = _start_fprintd_enroll_probe()
    devices, ecodes, evdev_evidence = _open_fingerprint_event_devices()
    hidraw_devices, hidraw_evidence = _open_fingerprint_hidraw_devices()
    sel = selectors.DefaultSelector()
    for dev in devices:
        try:
            sel.register(dev, selectors.EVENT_READ, ("evdev", getattr(dev, "path", "fingerprint event")))
        except (OSError, ValueError):
            pass
    for fd, label in hidraw_devices:
        try:
            sel.register(fd, selectors.EVENT_READ, ("hidraw", label))
        except (OSError, ValueError):
            pass

    evidence_parts = [probe_evidence, fprintd_evidence, evdev_evidence, hidraw_evidence]
    fprintd_lines: list[str] = []
    started_at = time.monotonic()
    previous_nodelay = False
    stdscr.nodelay(True)

    def evidence(extra: str = "") -> str:
        parts = list(evidence_parts)
        parts.extend(line for line in fprintd_lines[-5:] if line)
        if extra:
            parts.append(extra)
        return "; ".join(dict.fromkeys(part for part in parts if part))[:500]

    def stop_probe() -> None:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass

    try:
        while True:
            elapsed = int(time.monotonic() - started_at)
            stdscr.erase()
            draw_header(stdscr, "QC - Fingerprint")
            center_block(stdscr, [
                ("Touch the fingerprint reader to auto PASS.", curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
                ("Do not depend on LED blink; many good sensors do not blink.",
                 curses.color_pair(YELLOW_PAIR)),
                ("", 0),
                (f"Waiting for fingerprint touch... {elapsed}s", curses.A_BOLD),
                ("[ K ]  mark FAIL and enter remarks if the sensor does not detect.",
                 curses.A_BOLD | curses.color_pair(RED_PAIR)),
            ], top_offset=4)
            draw_footer(stdscr, "Touch sensor = auto PASS   K = FAIL with remarks")
            stdscr.refresh()

            ch = stdscr.getch()
            if ch in (ord("k"), ord("K")):
                stop_probe()
                remarks = _qc_remarks_dialog(stdscr, "Why did Fingerprint fail?")
                return qc.make_result(
                    "fingerprint",
                    applicable=True,
                    ran=True,
                    result="FAIL",
                    evidence=evidence("operator pressed K: no fingerprint detection"),
                    remarks=remarks or "fingerprint sensor did not detect touch",
                )

            if proc and proc.stdout:
                try:
                    readable, _, _ = select.select([proc.stdout], [], [], 0)
                except (OSError, ValueError):
                    readable = []
                for stream in readable:
                    line = stream.readline().strip()
                    if not line:
                        continue
                    fprintd_lines.append(line)
                    if _fingerprint_fprintd_touch_line(line):
                        stop_probe()
                        return qc.make_result(
                            "fingerprint",
                            applicable=True,
                            ran=True,
                            result="PASS",
                            evidence=evidence(f"auto-pass via fprintd: {line}"),
                            remarks="auto-pass: fingerprint touch detected",
                        )
                if proc.poll() is not None and not any("fprintd exited" in part for part in evidence_parts):
                    evidence_parts.append(f"fprintd exited rc={proc.returncode}")

            if devices or hidraw_devices:
                for key, _mask in sel.select(timeout=0.03):
                    kind, label = key.data if key.data else ("evdev", "fingerprint event")
                    if kind == "hidraw":
                        try:
                            payload = os.read(key.fileobj, 4096)
                        except (OSError, BlockingIOError):
                            payload = b""
                        if payload and time.monotonic() - started_at > 0.4:
                            stop_probe()
                            return qc.make_result(
                                "fingerprint",
                                applicable=True,
                                ran=True,
                                result="PASS",
                                evidence=evidence(f"auto-pass via hidraw: {label} bytes={len(payload)}"),
                                remarks="auto-pass: fingerprint touch detected",
                            )
                        continue
                    if ecodes is None:
                        continue
                    dev = key.fileobj
                    try:
                        events = [
                            ev for ev in dev.read()
                            if ev.type != ecodes.EV_SYN and getattr(ev, "value", 0) != 0
                        ]
                    except (OSError, BlockingIOError):
                        events = []
                    if events and time.monotonic() - started_at > 0.4:
                        stop_probe()
                        return qc.make_result(
                            "fingerprint",
                            applicable=True,
                            ran=True,
                            result="PASS",
                            evidence=evidence(f"auto-pass via evdev: {label}"),
                            remarks="auto-pass: fingerprint touch detected",
                        )

            time.sleep(0.08)
    finally:
        stop_probe()
        stdscr.nodelay(previous_nodelay)
        sel.close()
        for dev in devices:
            try:
                dev.close()
            except OSError:
                pass
        for fd, _label in hidraw_devices:
            try:
                os.close(fd)
            except OSError:
                pass


def screen_qc_speaker(stdscr) -> dict:
    probe = qc.probe_speaker()
    if not probe["applicable"]:
        return qc.na_result("speaker", probe["evidence"])

    def run_spk(s):
        s.erase()
        draw_header(s, "QC — Speaker")
        center_block(s, [
            ("Playing test tone (volume forced to 100%) — listen now…",
             curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
            ("", 0),
            ("(if you hear nothing, speakers may be broken or unmuted needed)",
             curses.color_pair(DIM_PAIR)),
        ])
        s.refresh()
        qc.play_test_tone(seconds=2.5)
        return probe["evidence"]

    return _qc_run_with_retest(
        stdscr, "speaker", allow_skip=False,
        run_test_fn=run_spk,
        prompt_lines=["Did you hear the test tone CLEARLY from BOTH speakers?"],
    )


def screen_qc_microphone(stdscr) -> dict:
    probe = qc.probe_microphone()
    if not probe["applicable"]:
        return qc.na_result("microphone", probe["evidence"])

    def run_mic(s):
        s.erase()
        draw_header(s, "QC — Microphone")
        center_block(s, [
            ("Speak into the laptop microphone for 3 seconds.", curses.A_BOLD),
            ("Recording… you'll hear it played back.",
             curses.color_pair(YELLOW_PAIR)),
        ])
        s.refresh()
        qc.record_then_playback(seconds=3)
        return probe["evidence"]

    return _qc_run_with_retest(
        stdscr, "microphone", allow_skip=False,
        run_test_fn=run_mic,
        prompt_lines=["Did you hear your own voice CLEARLY during playback?"],
    )


def screen_qc_speaker(stdscr) -> dict:
    readiness = qc.ensure_audio_ready()
    probe = qc.probe_speaker()

    def run_spk(s):
        s.erase()
        draw_header(s, "QC - Speaker")
        volume = qc.speaker_playback_percent()
        lines = [
            (f"Playing amplified spoken speaker labels at {volume}% volume.",
             curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
            ("Listen for spoken Front Left and Front Right labels only.",
             curses.color_pair(CYAN_PAIR)),
            ("", 0),
            ("No sine tone or generated noise should play in this test.",
             curses.color_pair(DIM_PAIR)),
        ]
        if not probe["applicable"]:
            lines.extend([
                ("", 0),
                ("Linux did not enumerate an audio device. The test will still run.",
                 curses.A_BOLD | curses.color_pair(RED_PAIR)),
                ("Mark FAIL if no spoken audio is heard.", curses.color_pair(YELLOW_PAIR)),
            ])
        center_block(s, lines)
        s.refresh()
        played = qc.play_test_tone(seconds=4.0)
        return (
            f"{readiness}; {probe['evidence']}; "
            f"playback_command={'ok' if played else 'failed'}"
        )

    return _qc_run_with_retest(
        stdscr, "speaker", allow_skip=False,
        run_test_fn=run_spk,
        prompt_lines=["Did you hear the spoken speaker labels clearly?"],
    )


def screen_qc_microphone(stdscr) -> dict:
    readiness = qc.ensure_audio_ready()
    probe = qc.probe_microphone()
    record_seconds = qc.microphone_record_seconds()

    def run_mic(s):
        def show_status(status: str) -> None:
            label = status.upper()
            s.erase()
            draw_header(s, f"QC - Microphone - {label}")
            if status.startswith("quiet"):
                lines = [
                    ("QUIET BASELINE", curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
                    ("Please stay silent for 1 second.", curses.A_BOLD),
                    ("This lets the test reject old-Dell hiss/static as a false voice.",
                     curses.color_pair(DIM_PAIR)),
                ]
            elif status.startswith("recording"):
                lines = [
                    ("RECORDING", curses.A_BOLD | curses.color_pair(RED_PAIR)),
                    (f"Speak near the laptop microphone for {record_seconds} seconds.", curses.A_BOLD),
                    ("The sample will play back automatically if real voice is captured.",
                     curses.color_pair(YELLOW_PAIR)),
                ]
            elif status.startswith("no voice"):
                lines = [
                    ("NO VOICE DETECTED", curses.A_BOLD | curses.color_pair(RED_PAIR)),
                    ("Recording did not rise above the quiet baseline.", curses.A_BOLD),
                    ("Playback is skipped so silence/static cannot be marked as PASS.",
                     curses.color_pair(YELLOW_PAIR)),
                ]
            elif status.startswith("playback failed"):
                lines = [
                    ("PLAYBACK NOT AVAILABLE", curses.A_BOLD | curses.color_pair(RED_PAIR)),
                    ("The recording command did not leave a playable sample.", curses.A_BOLD),
                    ("Retest, or mark FAIL if the laptop microphone still does not hear speech.",
                     curses.color_pair(YELLOW_PAIR)),
                ]
            else:
                lines = [
                    ("PLAYBACK", curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
                    ("Listen for the voice recorded from the laptop microphone.", curses.A_BOLD),
                ]
            center_block(s, lines, top_offset=4)
            s.refresh()

        s.erase()
        draw_header(s, "QC - Microphone")
        center_block(s, [
            ("Preparing microphone capture path...", curses.A_BOLD),
            ("Do not speak yet.", curses.color_pair(YELLOW_PAIR)),
            (
                "Linux did not enumerate a microphone; the test will still run."
                if not probe["applicable"] else "",
                curses.A_BOLD | curses.color_pair(RED_PAIR),
            ),
        ])
        s.refresh()
        qc.prepare_microphone_recording()
        qc.calibrate_microphone_baseline(seconds=1, status_callback=show_status)
        s.erase()
        draw_header(s, "QC - Microphone")
        center_block(s, [
            ("FINAL MIC WARM-UP", curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
            ("Do not speak yet; recording starts next.", curses.A_BOLD),
            ("This opens the selected Dell/HP mic path before the real sample.",
             curses.color_pair(DIM_PAIR)),
        ])
        s.refresh()
        qc.prepare_microphone_recording()

        s.erase()
        draw_header(s, "QC - Microphone")
        center_block(s, [
            ("GET READY", curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
            (f"Recording starts in 2 seconds, then lasts {record_seconds} seconds.",
             curses.A_BOLD),
            ("Speak clearly when RECORDING appears.", curses.color_pair(GREEN_PAIR)),
        ])
        s.refresh()
        time.sleep(1.5)

        s.erase()
        draw_header(s, "QC - Microphone")
        center_block(s, [
            (f"Recording NOW for {record_seconds} seconds. Speak into the laptop microphone.", curses.A_BOLD),
            ("If real voice is detected, it will play back automatically.",
             curses.color_pair(YELLOW_PAIR)),
        ])
        s.refresh()
        recorded = qc.record_then_playback(
            seconds=record_seconds,
            prepare=False,
            status_callback=show_status,
        )
        return (
            f"{readiness}; {probe['evidence']}; "
            f"record_playback={'ok' if recorded else 'failed'}; "
            f"{qc.microphone_activity_evidence()}"
        )

    retries = 0
    last_evidence = ""
    while True:
        evidence = run_mic(stdscr)
        if evidence:
            last_evidence = evidence
        voice_played = (
            "voice_detected=yes" in last_evidence
            and "record_playback=ok" in last_evidence
            and "playback=ok" in last_evidence
        )
        if voice_played:
            verdict, remarks = _qc_pass_fail_choice(
                stdscr,
                "Microphone",
                ["Did you hear your own voice clearly during playback?"],
                False,
            )
            if verdict == "RETEST":
                retries += 1
                continue
            ev = last_evidence
            if retries > 0:
                ev = f"{ev}  (retested {retries}x)"
            return qc.make_result(
                "microphone", applicable=True, evidence=ev,
                ran=True, result=verdict, remarks=remarks,
            )

        while True:
            stdscr.erase()
            draw_header(stdscr, "QC - Microphone - AUTO FAIL")
            center_block(stdscr, [
                ("NO USABLE MICROPHONE VOICE WAS CAPTURED",
                 curses.A_BOLD | curses.color_pair(RED_PAIR)),
                ("The test detected silence/static instead of spoken voice.", curses.A_BOLD),
                ("Press R to retest after checking the mic opening/BIOS/audio hardware.",
                 curses.color_pair(YELLOW_PAIR)),
                ("Press ENTER to record FAIL. PASS is locked until voice playback succeeds.",
                 curses.A_BOLD),
            ], top_offset=4)
            draw_footer(stdscr, "R retest   ENTER record FAIL   F fail with remarks")
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord("r"), ord("R")):
                retries += 1
                break
            if ch in (ord("f"), ord("F")):
                remarks = _qc_remarks_dialog(stdscr, "Why did Microphone fail?")
                ev = last_evidence
                if retries > 0:
                    ev = f"{ev}  (retested {retries}x)"
                return qc.make_result(
                    "microphone", applicable=True, evidence=ev,
                    ran=True, result="FAIL", remarks=remarks or "no voice captured",
                )
            if ch in (10, 13, curses.KEY_ENTER):
                ev = last_evidence
                if retries > 0:
                    ev = f"{ev}  (retested {retries}x)"
                return qc.make_result(
                    "microphone", applicable=True, evidence=ev,
                    ran=True, result="FAIL",
                    remarks="microphone did not capture usable voice",
                )


def screen_qc_audio_jack(stdscr) -> dict:
    probe = qc.probe_audio_jack()
    if not probe["applicable"]:
        return qc.na_result("audio_jack", probe["evidence"])

    def run_headphones(s):
        while True:
            plugged = qc.current_audio_jack_active()
            state = "DETECTED" if plugged else (
                "NOT DETECTED" if probe.get("plugged") is False else "UNKNOWN"
            )
            color = GREEN_PAIR if plugged else YELLOW_PAIR
            s.erase()
            draw_header(s, "QC - Audio Jack")
            center_block(s, [
                ("Connect and wear headphones in the 3.5 mm audio jack.", curses.A_BOLD),
                (f"Jack state: {state}", curses.A_BOLD | curses.color_pair(color)),
                ("", 0),
                ("Press ENTER to play Front Left / Front Right labels.",
                 curses.color_pair(CYAN_PAIR)),
                ("Confirm sound in the headphones, not the laptop speakers.",
                 curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
                ("After playback, the PASS / FAIL prompt will appear automatically.",
                 curses.color_pair(DIM_PAIR)),
            ], top_offset=4)
            draw_footer(s, "ENTER play headphone labels   Q skip playback and go to verdict")
            s.refresh()
            ch = s.getch()
            if ch in (10, 13, curses.KEY_ENTER):
                break
            if ch in (ord("q"), ord("Q")):
                return (
                    f"{probe['evidence']}; jack_active={str(plugged).lower()}; "
                    "operator skipped headphone playback before verdict"
                )
        route = qc.prepare_audio_jack_output()
        played = qc.play_audio_jack_sample()
        return (
            f"{probe['evidence']}; jack_active={str(plugged).lower()}; "
            f"{route}; front_left_right_playback={'ok' if played else 'not_ok'}"
        )

    return _qc_run_with_retest(
        stdscr,
        "audio_jack",
        allow_skip=False,
        run_test_fn=run_headphones,
        prompt_lines=[
            "Did you hear Front Left / Front Right through the headphones?",
            "PASS only if sound came from the headphones, not laptop speakers.",
        ],
    )


def _obsolete_screen_qc_touchscreen(stdscr) -> dict:
    """2026-05-13 Phase-2 fix: per ops feedback the LCD touch test should
    auto-detect touch capability and only run the interactive test when
    the panel IS touch-capable. Non-touch units get a brief informational
    screen and the test is marked N/A (silent skip, not a failure).

    2026-05-14 Bug #3 fix: founder asked for an operator OVERRIDE on the
    auto-detection — some hybrid panels mis-report and a flip key lets
    the operator correct the classification before the test runs.
    Press T within 4 seconds to flip the verdict, ENTER to accept.
    """
    probe = qc.PROBES["touchscreen"]()
    auto_is_touch = bool(probe["applicable"])

    # Operator override prompt
    final_is_touch = auto_is_touch
    stdscr.erase()
    draw_header(stdscr, "QC — LCD Type (auto-detected)")
    center_block(stdscr, [
        (f"Auto-detected: {'TOUCH' if auto_is_touch else 'NON-TOUCH'} LCD",
         curses.A_BOLD | curses.color_pair(GREEN_PAIR if auto_is_touch else YELLOW_PAIR)),
        ("", 0),
        ("Press T to flip the classification, or ENTER / wait 4s to accept.",
         curses.color_pair(DIM_PAIR)),
    ])
    draw_footer(stdscr, "T = flip   ENTER = accept   (auto-accept in 4s)")
    stdscr.refresh()
    stdscr.nodelay(True)
    deadline = time.time() + 4
    try:
        while time.time() < deadline:
            ch = stdscr.getch()
            if ch in (ord("t"), ord("T")):
                final_is_touch = not final_is_touch
                stdscr.erase()
                draw_header(stdscr, "QC — LCD Type (operator override)")
                center_block(stdscr, [
                    (f"Operator override: {'TOUCH' if final_is_touch else 'NON-TOUCH'} LCD",
                     curses.A_BOLD | curses.color_pair(GREEN_PAIR if final_is_touch else YELLOW_PAIR)),
                    ("", 0),
                    ("Press T again to flip back, ENTER to accept.",
                     curses.color_pair(DIM_PAIR)),
                ])
                draw_footer(stdscr, "T = flip   ENTER = accept")
                stdscr.refresh()
                deadline = time.time() + 4
            elif ch in (10, 13):  # ENTER
                break
            time.sleep(0.05)
    finally:
        stdscr.nodelay(False)

    override_note = "" if final_is_touch == auto_is_touch else " (operator override)"

    if not final_is_touch:
        # Non-touch — brief banner then skip
        stdscr.erase()
        draw_header(stdscr, "QC — LCD Type")
        center_block(stdscr, [
            (f"Non-touch LCD{override_note} — touch test skipped.",
             curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
            ("", 0),
            ("Moving to next QC step…",
             curses.color_pair(DIM_PAIR)),
        ])
        stdscr.refresh()
        time.sleep(1.6)
        return qc.na_result(
            "touchscreen",
            f"non-touch LCD — auto-skipped{override_note}",
        )

    def run_ts(s):
        s.erase()
        draw_header(s, f"QC — Touchscreen (touch panel{override_note})")
        center_block(s, [
            (f"Touch panel{override_note} — running touch test.",
             curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
            ("", 0),
            ("Tap each corner of the screen, then drag diagonally across.",
             curses.A_BOLD),
        ])
        s.refresh()
        time.sleep(3)
        return (probe["evidence"] or "operator-overridden touch") + override_note

    return _qc_run_with_retest(
        stdscr, "touchscreen", allow_skip=False,
        run_test_fn=run_ts,
        prompt_lines=["Did the touchscreen register every tap and the drag?"],
    )


def _interactive_ports_probe(stdscr) -> str:
    """2026-05-17 Bug #1 (option d): per-port checklist UI.

    Replaces the previous "count any USB events" screen with an explicit
    6-slot checklist so the operator can confirm EACH physical port:
      1. USB Type-A #1
      2. USB Type-A #2
      3. USB Type-C #1
      4. USB Type-C #2
      5. HDMI
      6. SD-card reader

    Background: ``udevadm monitor`` listens for hot-plug events so the
    UI can flash a "plug detected" banner when a USB device is inserted.
    The operator then presses the digit (1-4) for the slot they tested
    so the checkbox flips. HDMI is auto-detected via /sys/class/drm AND
    digit-5 toggle. SD has no reliable automated detection — digit-6
    only. ESC ESC within 1.5 s exits.
    """
    # Slot model: ordered list of (key_digit, label, auto_detect_kind)
    SLOTS = [
        ("1", "USB Type-A #1", "usb"),
        ("2", "USB Type-A #2", "usb"),
        ("3", "USB Type-C #1", "usb"),
        ("4", "USB Type-C #2", "usb"),
        ("5", "HDMI",          "hdmi"),
        ("6", "SD-card reader", "manual"),
    ]
    checked = {s[0]: False for s in SLOTS}
    last_event_msg = ""

    # USB live detection
    proc = None
    try:
        proc = subprocess.Popen(
            ["udevadm", "monitor", "--udev", "--subsystem-match=usb"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        try:
            import fcntl
            fl = fcntl.fcntl(proc.stdout, fcntl.F_GETFL)
            fcntl.fcntl(proc.stdout, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        except (ImportError, OSError):
            pass
    except (FileNotFoundError, OSError):
        proc = None

    # HDMI baseline
    hdmi_baseline = set()
    try:
        for path in os.listdir("/sys/class/drm"):
            status = f"/sys/class/drm/{path}/status"
            if os.path.exists(status):
                with open(status) as f:
                    if f.read().strip() == "connected":
                        hdmi_baseline.add(path)
    except OSError:
        pass

    def _redraw():
        stdscr.erase()
        draw_header(stdscr, "QC — Ports (per-slot checklist)")
        y = 3
        _safe_addstr(stdscr, y, 2,
                     "Plug a USB stick / cable / SD card into EACH port one by one.",
                     curses.A_BOLD)
        y += 1
        _safe_addstr(stdscr, y, 2,
                     "After plugging into a slot, press the slot's number to mark it tested.",
                     curses.color_pair(DIM_PAIR))
        y += 2
        for digit, label, _kind in SLOTS:
            mark = "[X]" if checked[digit] else "[ ]"
            attr = (curses.color_pair(GREEN_PAIR) | curses.A_BOLD
                    if checked[digit] else curses.color_pair(DIM_PAIR))
            _safe_addstr(stdscr, y, 4, f" {digit}  {mark}  {label}", attr)
            y += 1
        y += 1
        done = sum(1 for v in checked.values() if v)
        total = len(SLOTS)
        attr = (curses.color_pair(GREEN_PAIR) | curses.A_BOLD
                if done == total else curses.A_BOLD)
        _safe_addstr(stdscr, y, 2,
                     f"[ports tested: {done} / {total}]", attr)
        y += 2
        if last_event_msg:
            _safe_addstr(stdscr, y, 2, last_event_msg,
                         curses.color_pair(YELLOW_PAIR))
            y += 1
        draw_footer(stdscr, "Press 1-6 to toggle a slot.  ESC ESC (double-tap) to exit.")
        stdscr.refresh()

    _redraw()
    stdscr.nodelay(True)
    ESC_DOUBLE = 1.5
    last_esc_ts: float | None = None
    deadline = time.time() + 180  # 3-minute safety net
    try:
        while time.time() < deadline:
            # 1. Drain udev events (non-blocking)
            if proc:
                try:
                    ln = proc.stdout.readline()
                except (BlockingIOError, OSError):
                    ln = ""
                if ln and "add" in ln.lower() and "/usb" in ln:
                    last_event_msg = "USB plug detected — press 1/2/3/4 for the slot you just used"
                    _redraw()

            # 2. HDMI poll (cheap, once per loop)
            try:
                connected = set()
                for path in os.listdir("/sys/class/drm"):
                    status = f"/sys/class/drm/{path}/status"
                    if os.path.exists(status):
                        with open(status) as f:
                            if f.read().strip() == "connected":
                                connected.add(path)
                new_hdmi = connected - hdmi_baseline
                if new_hdmi and not checked["5"]:
                    checked["5"] = True
                    last_event_msg = f"HDMI auto-detected: {sorted(new_hdmi)}"
                    _redraw()
            except OSError:
                pass

            # 3. Keyboard
            ch = stdscr.getch()
            if ch == 27:  # ESC
                now = time.time()
                if last_esc_ts is not None and (now - last_esc_ts) <= ESC_DOUBLE:
                    break
                last_esc_ts = now
                last_event_msg = "Press ESC again within 1.5 s to exit."
                _redraw()
            elif ch != -1 and 32 <= ch < 127:
                key = chr(ch)
                if key in checked:
                    checked[key] = not checked[key]
                    last_event_msg = (
                        f"Slot {key} {'marked tested' if checked[key] else 'unmarked'}"
                    )
                    _redraw()
            time.sleep(0.05)
    finally:
        stdscr.nodelay(False)
        if proc:
            try:
                proc.terminate()
            except OSError:
                pass

    # Build summary
    done = sum(1 for v in checked.values() if v)
    detail = ", ".join(
        f"{lbl}={'OK' if checked[d] else 'NO'}"
        for d, lbl, _k in SLOTS
    )
    return f"ports {done}/{len(SLOTS)}: {detail}"



def screen_qc_ports(stdscr) -> dict:
    return _qc_run_with_retest(
        stdscr, "ports", allow_skip=False,
        run_test_fn=_interactive_ports_probe,
        prompt_lines=[
            "Did all USB ports show ADD events above + HDMI register?",
            "(Operator verdict based on the live detection log.)",
        ],
    )


def screen_qc_storage(stdscr) -> dict:
    """2026-05-11 fix: new Storage QC screen — runs hdparm read-throughput
    on the primary disk. Operator gates PASS/FAIL based on the measured
    MB/s.
    """
    probe = qc.PROBES["storage"]()
    if not probe["applicable"]:
        return qc.na_result("storage", probe["evidence"])

    def run_storage(s):
        s.erase()
        draw_header(s, "QC — Storage")
        center_block(s, [
            ("Measuring read throughput on primary disk…", curses.A_BOLD),
            ("(takes ~5 seconds — `hdparm -t --direct`)",
             curses.color_pair(DIM_PAIR)),
        ])
        s.refresh()
        mb, evidence = qc.storage_throughput_mb_per_sec()
        # Show result for 2 sec
        s.erase()
        draw_header(s, "QC — Storage")
        threshold_pass = mb >= 100  # SATA HDD-tier; NVMe should be >>500
        col = curses.color_pair(GREEN_PAIR) if threshold_pass else curses.color_pair(RED_PAIR)
        center_block(s, [
            (evidence, curses.A_BOLD | col),
            ("", 0),
            ("Threshold for PASS hint: ≥ 100 MB/s",
             curses.color_pair(DIM_PAIR)),
        ])
        s.refresh()
        time.sleep(2)
        return evidence

    return _qc_run_with_retest(
        stdscr, "storage", allow_skip=False,
        run_test_fn=run_storage,
        prompt_lines=[
            "Based on the throughput measured above, is the storage HEALTHY?",
            "(Operator verdict: SSDs should easily exceed 100 MB/s read.)",
        ],
    )


def _read_ac_online(node: str) -> str:
    """Return '1' / '0' / '' for an AC power_supply node's online flag."""
    try:
        with open(f"/sys/class/power_supply/{node}/online") as f:
            return f.read().strip()
    except OSError:
        return ""


def _interactive_power_adapter_probe(stdscr) -> str:
    """2026-05-13 Phase-2 fix: new Power Adapter QC test (was missing).

    Reads `/sys/class/power_supply/{AC,ADP1,ACAD,…}/online` and watches it
    flip 0→1 (plug detected) and 1→0 (unplug detected) within a 30 s
    window. The operator is asked to unplug, then re-plug, the charger.
    """
    base = "/sys/class/power_supply"
    ac_nodes = []
    if os.path.isdir(base):
        for entry in os.listdir(base):
            type_path = f"{base}/{entry}/type"
            try:
                with open(type_path) as f:
                    if f.read().strip().lower() == "mains":
                        ac_nodes.append(entry)
            except OSError:
                up = entry.upper()
                if up.startswith(("AC", "ADP", "ACAD", "ACPI", "USBC")):
                    ac_nodes.append(entry)
    if not ac_nodes:
        return "no AC power_supply node — cannot detect charger"

    state_log = []
    initial_states = {n: _read_ac_online(n) for n in ac_nodes}
    saw_unplug = False
    saw_replug = False
    last_states = dict(initial_states)

    stdscr.erase()
    draw_header(stdscr, "QC — Power Adapter (interactive)")
    center_block(stdscr, [
        ("Step 1 — UNPLUG the charger now.",
         curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
        ("", 0),
        (f"Watching: {', '.join(ac_nodes)}",
         curses.color_pair(DIM_PAIR)),
        ("", 0),
        ("(Press ESC to abort.)", curses.color_pair(DIM_PAIR)),
    ], top_offset=2)
    stdscr.refresh()
    stdscr.nodelay(True)

    deadline = time.time() + 30
    while time.time() < deadline:
        for n in ac_nodes:
            cur = _read_ac_online(n)
            if cur != last_states[n]:
                state_log.append(f"{n}:{last_states[n]}->{cur}")
                last_states[n] = cur
                if not saw_unplug and cur == "0":
                    saw_unplug = True
                    stdscr.erase()
                    draw_header(stdscr, "QC — Power Adapter")
                    center_block(stdscr, [
                        ("✓ Unplug detected.",
                         curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
                        ("", 0),
                        ("Step 2 — RE-PLUG the charger now.",
                         curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
                    ], top_offset=2)
                    stdscr.refresh()
                elif saw_unplug and not saw_replug and cur == "1":
                    saw_replug = True
                    stdscr.erase()
                    draw_header(stdscr, "QC — Power Adapter")
                    center_block(stdscr, [
                        ("✓ Unplug detected.",
                         curses.color_pair(GREEN_PAIR)),
                        ("✓ Re-plug detected.",
                         curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
                    ], top_offset=2)
                    stdscr.refresh()
        if saw_unplug and saw_replug:
            time.sleep(0.5)
            break
        ch = stdscr.getch()
        if ch == 27:  # ESC
            break
        time.sleep(0.15)
    stdscr.nodelay(False)

    status = (
        "unplug+replug OK" if (saw_unplug and saw_replug)
        else "unplug only" if saw_unplug
        else "replug only" if saw_replug
        else "no state change observed"
    )
    return f"{status} [{ '; '.join(state_log) or 'no transitions' }]"


def screen_qc_power_adapter(stdscr) -> dict:
    """Power Adapter / DC-charger port check."""
    probe = qc.PROBES["power_adapter"]()
    return _qc_run_with_retest(
        stdscr, "power_adapter", allow_skip=False,
        run_test_fn=_interactive_power_adapter_probe,
        prompt_lines=[
            "Was the unplug + re-plug cycle observed cleanly above?",
            f"(Probe evidence: {probe.get('evidence', '')[:60]})",
        ],
    )


def _mark_next_port(slots: list[dict], status: dict[str, bool],
                    kind: str, messages: list[str], reason: str) -> bool:
    for slot in slots:
        if slot["kind"] == kind and not status.get(slot["id"], False):
            status[slot["id"]] = True
            messages.append(f"{slot['label']} detected ({reason})")
            return True
    return False


def _next_port_row_number(slots: list[dict], kind: str) -> int:
    return sum(1 for slot in slots if slot.get("kind") == kind) + 1


def _append_live_port(slots: list[dict], status: dict[str, bool],
                      kind: str, messages: list[str], evidence: str) -> dict:
    row_number = _next_port_row_number(slots, kind)
    base_label = qc.PORT_KIND_LABELS.get(kind, kind.replace("_", " ").title())
    label = f"{base_label} #{row_number}" if row_number > 1 else base_label
    slot = {
        "id": f"{kind}_live_{row_number}_{int(time.monotonic() * 1000)}",
        "kind": kind,
        "label": label,
        "evidence": f"live-detected: {evidence}",
        "live_discovered": True,
    }
    slots.append(slot)
    status[slot["id"]] = False
    messages.append(f"Added {label} from live event ({evidence})")
    return slot


def _has_remaining_port(slots: list[dict], status: dict[str, bool], kind: str) -> bool:
    return any(slot["kind"] == kind and not status.get(slot["id"], False) for slot in slots)


def _mark_display_ports(slots: list[dict], status: dict[str, bool],
                        messages: list[str], connected_kinds: set[str],
                        *, allow_live_add: bool = False) -> bool:
    changed = False
    for kind in sorted(connected_kinds):
        if allow_live_add and not _has_remaining_port(slots, status, kind):
            _append_live_port(slots, status, kind, messages, "display hotplug")
        changed |= _mark_next_port(slots, status, kind, messages, "external display connected")
    return changed


def _ports_evidence(slots: list[dict], status: dict[str, bool]) -> str:
    done = sum(1 for slot in slots if status.get(slot["id"], False))
    detail = ", ".join(
        f"{slot['label']}={'OK' if status.get(slot['id'], False) else 'NO'}"
        for slot in slots
    )
    return f"ports {done}/{len(slots)}: {detail}"


def _confirm_port_inventory(stdscr, detected_slots: list[dict]) -> list[dict]:
    """Let the technician correct firmware/kernel connector inventory once."""
    order = list(qc.PORT_KIND_LABELS)
    counts = {kind: 0 for kind in order}
    for slot in detected_slots:
        kind = slot.get("kind")
        if kind in counts:
            counts[kind] += 1

    selected = 0
    while True:
        stdscr.erase()
        draw_header(stdscr, "QC - Confirm Physical Ports")
        h, w = stdscr.getmaxyx()
        _safe_addstr(
            stdscr, 3, 2,
            "Verify the automatic list against the laptop chassis before testing.",
            curses.A_BOLD,
        )
        _safe_addstr(
            stdscr, 4, 2,
            "UP/DOWN select, LEFT/RIGHT change quantity, ENTER confirm.",
            curses.color_pair(DIM_PAIR),
        )
        y = 6
        for index, kind in enumerate(order):
            if y >= h - 3:
                break
            label = qc.PORT_KIND_LABELS[kind]
            marker = ">" if index == selected else " "
            attr = (
                curses.A_BOLD | curses.color_pair(GREEN_PAIR)
                if index == selected else curses.A_NORMAL
            )
            _safe_addstr(
                stdscr, y, 4,
                f"{marker} {label:<24}  {counts[kind]}",
                attr,
            )
            y += 2
        draw_footer(stdscr, "UP/DOWN select   LEFT/RIGHT quantity   ENTER confirm")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k"), ord("K")):
            selected = (selected - 1) % len(order)
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            selected = (selected + 1) % len(order)
        elif ch in (curses.KEY_LEFT, ord("-")):
            kind = order[selected]
            counts[kind] = max(0, counts[kind] - 1)
        elif ch in (curses.KEY_RIGHT, ord("+"), ord("=")):
            kind = order[selected]
            max_count = 8 if kind in {"usb_a", "usb_c"} else 4
            counts[kind] = min(max_count, counts[kind] + 1)
        elif ch in (10, 13, curses.KEY_ENTER):
            return qc.port_rows_from_counts(counts)


def _run_ports_matrix(stdscr, slots: list[dict]) -> dict:
    status = {slot["id"]: False for slot in slots}
    messages: list[str] = []
    last_usb_devices = qc.current_usb_device_keys()
    last_typec = qc.current_typec_partners()
    tested_usb_ports: set[str] = set()
    tested_usb_fingerprints: set[str] = set()
    suppressed_usb_fingerprints: dict[str, float] = {}
    typec_guard_until = 0.0
    tested_typec_ports: set[str] = set()
    pending_usb_ports: dict[str, float] = {}
    last_power_online = qc.current_dc_power_online()
    last_wired_link_up = qc.current_wired_link_up()
    last_audio_active = qc.current_audio_jack_active()
    last_display_connectors = qc.current_connected_display_connectors()
    last_display_kinds = set(last_display_connectors.values())
    power_armed = not last_power_online
    wired_armed = not last_wired_link_up
    audio_armed = not last_audio_active
    display_armed = {
        slot["kind"] for slot in slots
        if slot["kind"] in {"hdmi", "vga", "displayport", "dvi"}
        and slot["kind"] not in last_display_kinds
    }
    last_mark_times: dict[str, float] = {}
    suppress_power_until = 0.0
    suppress_usb_until = 0.0
    PHYSICAL_EVENT_COOLDOWN = 1.4
    USB_CLASSIFY_DELAY = 1.2
    TYPEC_USB_SUPPRESS_SECONDS = 10.0
    manual_port_key_defs = [
        ("H", "hdmi", "HDMI"),
        ("V", "vga", "VGA"),
        ("D", "displayport", "DisplayPort"),
        ("I", "dvi", "DVI"),
        ("J", "audio", "3.5mm Audio Jack"),
    ]
    selected_manual_kinds = {
        slot["kind"] for slot in slots
        if slot["kind"] in {kind for _, kind, _ in manual_port_key_defs}
    }
    manual_port_keys_all: dict[int, tuple[str, str]] = {}
    manual_port_keys: dict[int, tuple[str, str]] = {}
    manual_port_letters: dict[str, tuple[str, str]] = {}
    for letter, kind, label in manual_port_key_defs:
        manual_port_letters[kind] = (letter, label)
        for key in (ord(letter.lower()), ord(letter.upper())):
            manual_port_keys_all[key] = (kind, label)
            if kind in selected_manual_kinds:
                manual_port_keys[key] = (kind, label)

    def manual_mapping_text() -> str:
        parts = [
            f"{letter} - {label}"
            for letter, kind, label in manual_port_key_defs
            if kind in selected_manual_kinds
        ]
        return "Manual OK keys: " + (" | ".join(parts) if parts else "none selected")

    def manual_footer_text() -> str:
        short_names = {
            "hdmi": "HDMI",
            "vga": "VGA",
            "displayport": "DP",
            "dvi": "DVI",
            "audio": "3.5mm",
        }
        parts = [
            f"{letter} {short_names[kind]}"
            for letter, kind, _ in manual_port_key_defs
            if kind in selected_manual_kinds
        ]
        prefix = " | ".join(parts) if parts else "No manual ports selected"
        return f"{prefix}   R reselect   S fail"

    proc = None
    audio_proc = None
    udev_event_buffer: list[str] = []
    try:
        proc = subprocess.Popen(
            ["udevadm", "monitor", "--udev", "--property"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        if proc.stdout is not None:
            try:
                import fcntl
                fl = fcntl.fcntl(proc.stdout, fcntl.F_GETFL)
                fcntl.fcntl(proc.stdout, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            except (ImportError, OSError):
                pass
    except (FileNotFoundError, OSError):
        proc = None

    try:
        audio_proc = subprocess.Popen(
            ["alsactl", "monitor"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        if audio_proc.stdout is not None:
            try:
                import fcntl
                fl = fcntl.fcntl(audio_proc.stdout, fcntl.F_GETFL)
                fcntl.fcntl(audio_proc.stdout, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            except (ImportError, OSError):
                pass
    except (FileNotFoundError, OSError):
        audio_proc = None

    def drain_udev_events(max_lines: int = 64) -> list[str]:
        """Return complete blank-line-delimited udev property events."""
        if not proc or proc.stdout is None:
            return []
        events: list[str] = []
        for _ in range(max_lines):
            try:
                line = proc.stdout.readline()
            except (BlockingIOError, OSError):
                break
            if not line:
                break
            stripped = line.rstrip("\r\n")
            if stripped.startswith(("UDEV ", "KERNEL ")) and udev_event_buffer:
                events.append("\n".join(udev_event_buffer))
                udev_event_buffer.clear()
            if stripped:
                udev_event_buffer.append(stripped)
            elif udev_event_buffer:
                events.append("\n".join(udev_event_buffer))
                udev_event_buffer.clear()
        return events

    def drain_audio_events(max_lines: int = 32) -> list[str]:
        if not audio_proc or audio_proc.stdout is None:
            return []
        lines: list[str] = []
        for _ in range(max_lines):
            try:
                line = audio_proc.stdout.readline()
            except (BlockingIOError, OSError):
                break
            if not line:
                break
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
        return lines

    def redraw() -> None:
        stdscr.erase()
        draw_header(stdscr, "QC - Ports")
        h, w = stdscr.getmaxyx()
        y = 3
        _safe_addstr(stdscr, y, 2,
                     "Connect each port one at a time. Each row needs its own plug/replug event.",
                     curses.A_BOLD)
        y += 1
        _safe_addstr(stdscr, y, 2,
                     "Use S only when a port cannot be detected; remaining rows become FAIL.",
                     curses.color_pair(DIM_PAIR))
        y += 1
        _safe_addstr(stdscr, y, 2,
                     manual_mapping_text()[: max(10, w - 4)],
                     curses.color_pair(DIM_PAIR))
        y += 2
        for idx, slot in enumerate(slots, start=1):
            if y >= h - 6:
                break
            ok = status.get(slot["id"], False)
            mark = "[OK]" if ok else "[  ]"
            attr = curses.A_BOLD | curses.color_pair(GREEN_PAIR) if ok else curses.A_NORMAL
            _safe_addstr(stdscr, y, 4, f"{idx:>2}. {mark} {slot['label']}", attr)
            y += 1
        done = sum(1 for slot in slots if status.get(slot["id"], False))
        y += 1
        total_attr = curses.A_BOLD | (
            curses.color_pair(GREEN_PAIR) if done == len(slots)
            else curses.color_pair(YELLOW_PAIR)
        )
        _safe_addstr(stdscr, y, 2, f"Completed: {done}/{len(slots)}", total_attr)
        y += 2
        for msg in messages[-4:]:
            if y >= h - 2:
                break
            _safe_addstr(stdscr, y, 2, msg[: max(10, w - 4)], curses.color_pair(CYAN_PAIR))
            y += 1
        draw_footer(stdscr, manual_footer_text())
        stdscr.refresh()

    redraw()
    stdscr.nodelay(True)
    result = "FAIL"
    try:
        while True:
            changed = False
            now = time.monotonic()
            for fingerprint, suppress_until in list(suppressed_usb_fingerprints.items()):
                if suppress_until <= now:
                    suppressed_usb_fingerprints.pop(fingerprint, None)

            def mark_one(kind: str, reason: str, *, use_cooldown: bool = True,
                         allow_live_add: bool = False) -> bool:
                if not _has_remaining_port(slots, status, kind):
                    if allow_live_add:
                        _append_live_port(slots, status, kind, messages, reason)
                    else:
                        return False
                if not _has_remaining_port(slots, status, kind):
                    return False
                last_kind_mark = last_mark_times.get(kind, 0.0)
                if use_cooldown and now - last_kind_mark < PHYSICAL_EVENT_COOLDOWN:
                    return False
                if _mark_next_port(slots, status, kind, messages, reason):
                    last_mark_times[kind] = now
                    return True
                return False

            def note_manual_event(kind: str, reason: str) -> bool:
                if kind not in selected_manual_kinds:
                    return False
                if not _has_remaining_port(slots, status, kind):
                    return False
                letter, label = manual_port_letters[kind]
                last_kind_mark = last_mark_times.get(f"manual-note:{kind}", 0.0)
                if now - last_kind_mark < PHYSICAL_EVENT_COOLDOWN:
                    return False
                messages.append(f"{label} detected ({reason}); press {letter} to confirm.")
                last_mark_times[f"manual-note:{kind}"] = now
                return True

            def note_manual_display_event(kinds: set[str], reason: str) -> bool:
                changed_note = False
                for kind in sorted(kinds):
                    changed_note |= note_manual_event(kind, reason)
                return changed_note

            # Type-C/power can fire together on USB-C chargers. Prefer the
            # Type-C row for that plug event; the DC/barrel row must have its
            # own later transition.
            current_typec = qc.current_typec_partners()
            typec_added = current_typec - last_typec
            new_typec_ports = typec_added - tested_typec_ports
            if new_typec_ports:
                port_id = sorted(new_typec_ports)[0]
                if mark_one("usb_c", f"Type-C partner detected on {port_id}",
                            use_cooldown=False, allow_live_add=True):
                    tested_typec_ports.update(typec_added)
                    changed = True
                # USB enumeration can precede the Type-C partner node. Recent
                # USB paths belong to this Type-C insertion and must not later
                # advance a USB-A row.
                suppress_usb_until = now + TYPEC_USB_SUPPRESS_SECONDS
                typec_guard_until = max(typec_guard_until, suppress_usb_until)
                typec_usb_candidates = (
                    set(qc.current_usb_device_keys())
                    | set(last_usb_devices)
                    | set(pending_usb_ports)
                )
                for usb_port in typec_usb_candidates:
                    suppressed_usb_fingerprints[
                        qc.usb_connector_fingerprint(usb_port)
                    ] = suppress_usb_until
                for usb_port, first_seen in list(pending_usb_ports.items()):
                    fingerprint = qc.usb_connector_fingerprint(usb_port)
                    if (
                        now - first_seen <= TYPEC_USB_SUPPRESS_SECONDS
                        or fingerprint in suppressed_usb_fingerprints
                    ):
                        pending_usb_ports.pop(usb_port, None)
                suppress_power_until = now + 2.5
            last_typec = current_typec

            current_usb_devices = qc.current_usb_device_keys()
            usb_added = current_usb_devices - last_usb_devices
            new_usb_ports = {
                port_id for port_id in usb_added
                if port_id not in tested_usb_ports
                and qc.usb_connector_fingerprint(port_id) not in tested_usb_fingerprints
            }
            if new_usb_ports and not typec_added and now < typec_guard_until:
                # Some Type-C devices expose the partner first and enumerate as
                # a normal USB device a few seconds later. Do not let that late
                # USB event consume a Type-A row.
                for port_id in new_usb_ports:
                    suppressed_usb_fingerprints[
                        qc.usb_connector_fingerprint(port_id)
                    ] = max(
                        suppressed_usb_fingerprints.get(
                            qc.usb_connector_fingerprint(port_id), 0.0
                        ),
                        typec_guard_until,
                    )
            elif new_usb_ports and not typec_added and now >= suppress_usb_until:
                for port_id in new_usb_ports:
                    fingerprint = qc.usb_connector_fingerprint(port_id)
                    if fingerprint not in suppressed_usb_fingerprints:
                        pending_usb_ports.setdefault(port_id, now)
            last_usb_devices = current_usb_devices

            current_power = qc.current_dc_power_online()
            if not current_power:
                power_armed = True
            if (
                power_armed and current_power and not last_power_online
                and now >= suppress_power_until
            ):
                changed |= mark_one("dc_power", "charger online transition",
                                    allow_live_add=True)
                power_armed = False
            last_power_online = current_power

            current_wired = qc.current_wired_link_up()
            if not current_wired:
                wired_armed = True
            if wired_armed and current_wired and not last_wired_link_up:
                changed |= mark_one("ethernet", "link/carrier transition",
                                    allow_live_add=True)
                wired_armed = False
            last_wired_link_up = current_wired

            current_audio = qc.current_audio_jack_active()
            if not current_audio:
                audio_armed = True
            if audio_armed and current_audio and not last_audio_active:
                changed |= note_manual_event("audio", "jack sense transition")
                audio_armed = False
            last_audio_active = current_audio

            current_display_connectors = qc.current_connected_display_connectors()
            current_display = set(current_display_connectors.values())
            disconnected_names = (
                set(last_display_connectors) - set(current_display_connectors)
            )
            display_armed.update(
                last_display_connectors[name]
                for name in disconnected_names
                if name in last_display_connectors
            )
            added_names = (
                set(current_display_connectors) - set(last_display_connectors)
            )
            display_added = {
                current_display_connectors[name] for name in added_names
            }
            last_display_mark = last_mark_times.get("display", 0.0)
            if display_added and now - last_display_mark >= PHYSICAL_EVENT_COOLDOWN:
                if note_manual_display_event(display_added, "display hotplug transition"):
                    last_mark_times["display"] = now
                    display_armed.difference_update(display_added)
                    changed = True
            last_display_connectors = current_display_connectors
            last_display_kinds = current_display

            audio_events = drain_audio_events()
            if audio_events:
                jack_event = any(
                    term in "\n".join(audio_events).lower()
                    for term in (
                        "headphone", "headset", "jack", "line out",
                        "line-out", "external mic", "microphone",
                    )
                )
                if jack_event:
                    current_audio = False
                    for _ in range(5):
                        current_audio = qc.current_audio_jack_active()
                        if current_audio:
                            break
                        time.sleep(0.1)
                    if audio_armed and current_audio:
                        if note_manual_event(
                            "audio",
                            "ALSA jack control transition",
                        ):
                            changed = True
                        audio_armed = False
                    last_audio_active = current_audio

            for event in drain_udev_events():
                lower = event.lower()
                action_match = re.search(r"(?m)^action=(add|change)$", lower)
                header_action = re.search(r"\b(add|change)\b", lower.splitlines()[0])
                action = (
                    action_match.group(1) if action_match
                    else header_action.group(1) if header_action else ""
                )
                if not action:
                    continue
                if "typec" in lower:
                    match = re.search(r"/typec/(port\d+)(?:-partner)?", lower)
                    port_id = match.group(1) if match else ""
                    if port_id and port_id not in tested_typec_ports:
                        if mark_one("usb_c", f"Type-C udev event on {port_id}",
                                    use_cooldown=False):
                            tested_typec_ports.add(port_id)
                            changed = True
                        suppress_usb_until = now + TYPEC_USB_SUPPRESS_SECONDS
                        typec_guard_until = max(typec_guard_until, suppress_usb_until)
                        typec_usb_candidates = (
                            set(qc.current_usb_device_keys())
                            | set(last_usb_devices)
                            | set(pending_usb_ports)
                        )
                        for usb_port in typec_usb_candidates:
                            suppressed_usb_fingerprints[
                                qc.usb_connector_fingerprint(usb_port)
                            ] = suppress_usb_until
                        for usb_port, first_seen in list(pending_usb_ports.items()):
                            fingerprint = qc.usb_connector_fingerprint(usb_port)
                            if (
                                now - first_seen <= TYPEC_USB_SUPPRESS_SECONDS
                                or fingerprint in suppressed_usb_fingerprints
                            ):
                                pending_usb_ports.pop(usb_port, None)
                    suppress_power_until = now + 2.5
                elif "drm" in lower:
                    current_display_connectors = {}
                    for _ in range(3):
                        current_display_connectors = (
                            qc.current_connected_display_connectors()
                        )
                        if (
                            set(current_display_connectors)
                            - set(last_display_connectors)
                        ):
                            break
                        time.sleep(0.1)
                    added_names = (
                        set(current_display_connectors)
                        - set(last_display_connectors)
                    )
                    display_added = {
                        current_display_connectors[name] for name in added_names
                    }
                    last_display_mark = last_mark_times.get("display", 0.0)
                    if display_added and now - last_display_mark >= PHYSICAL_EVENT_COOLDOWN:
                        if note_manual_display_event(display_added, "DRM hotplug event"):
                            last_mark_times["display"] = now
                            changed = True
                    last_display_connectors = current_display_connectors
                    last_display_kinds = set(current_display_connectors.values())
                elif any(
                    marker in lower
                    for marker in ("subsystem=sound", "subsystem=input", "/sound/", "/input/")
                ):
                    current_audio = False
                    for _ in range(3):
                        current_audio = qc.current_audio_jack_active()
                        if current_audio:
                            break
                        time.sleep(0.1)
                    if audio_armed and current_audio:
                        if note_manual_event(
                            "audio",
                            "audio jack input event",
                        ):
                            changed = True
                        audio_armed = False
                    last_audio_active = current_audio

            ready_usb_ports = {
                port_id for port_id, first_seen in pending_usb_ports.items()
                if port_id in current_usb_devices
                and now - first_seen >= USB_CLASSIFY_DELAY
                and qc.usb_connector_fingerprint(port_id) not in tested_usb_fingerprints
                and qc.usb_connector_fingerprint(port_id) not in suppressed_usb_fingerprints
            }
            for port_id in list(pending_usb_ports):
                if port_id not in current_usb_devices:
                    pending_usb_ports.pop(port_id, None)
            if ready_usb_ports:
                port_id = sorted(ready_usb_ports)[0]
                selected_fingerprint = qc.usb_connector_fingerprint(port_id)
                usb_kind = qc.usb_physical_port_kind(port_id)
                if not _has_remaining_port(slots, status, usb_kind):
                    usb_kind = ""
                if usb_kind and mark_one(
                    usb_kind,
                    (
                        f"{'Type-C' if usb_kind == 'usb_c' else 'Type-A'} "
                        f"USB device inserted on physical path {port_id}"
                    ),
                    use_cooldown=False,
                    allow_live_add=True,
                ):
                    # Treat USB 2/3 companion-controller paths for the same
                    # physical socket as one connector interaction, while
                    # leaving unrelated pending ports for their own rows.
                    tested_usb_fingerprints.add(selected_fingerprint)
                    for ready_port in list(ready_usb_ports):
                        if qc.usb_connector_fingerprint(ready_port) != selected_fingerprint:
                            continue
                        tested_usb_ports.add(ready_port)
                        pending_usb_ports.pop(ready_port, None)
                    changed = True
                elif not usb_kind:
                    pending_usb_ports.pop(port_id, None)

            if changed:
                redraw()
            if all(status.values()):
                result = "PASS"
                messages.append("All detected ports completed.")
                redraw()
                time.sleep(0.8)
                break

            ch = stdscr.getch()
            if ch in (ord("r"), ord("R")):
                messages.append("Reselecting port inventory.")
                redraw()
                time.sleep(0.15)
                return {"_action": "reselect"}
            if ch in (ord("s"), ord("S")):
                messages.append("Technician ended ports test with remaining rows.")
                redraw()
                time.sleep(0.5)
                break
            manual = manual_port_keys.get(ch)
            if manual:
                kind, label = manual
                if kind in {"hdmi", "vga", "displayport", "dvi"}:
                    stdscr.nodelay(False)
                    confirmed = _confirm_yn(
                        stdscr,
                        f"Is the VSTL screen visible on the connected {label} display?",
                    )
                    stdscr.nodelay(True)
                    if confirmed and mark_one(
                        kind,
                        f"technician visually confirmed {label} output",
                        use_cooldown=False,
                        allow_live_add=False,
                    ):
                        changed = True
                    elif not confirmed:
                        messages.append(f"{label} output was not confirmed.")
                elif kind == "audio":
                    stdscr.nodelay(False)
                    confirmed = _confirm_yn(
                        stdscr,
                        "Are headphones connected and audio confirmed through the 3.5mm jack?",
                    )
                    stdscr.nodelay(True)
                    if confirmed and mark_one(
                        kind,
                        "technician confirmed 3.5mm headphone output",
                        use_cooldown=False,
                        allow_live_add=False,
                    ):
                        changed = True
                    elif not confirmed:
                        messages.append("3.5mm headphone output was not confirmed.")
                elif mark_one(kind, f"technician-confirmed {label}",
                              use_cooldown=False, allow_live_add=False):
                    changed = True
                else:
                    messages.append(f"No remaining {label} row to confirm.")
                redraw()
                time.sleep(0.15)
                continue
            absent_manual = manual_port_keys_all.get(ch)
            if absent_manual:
                _kind, label = absent_manual
                messages.append(f"{label} is not in the selected port list.")
                redraw()
                time.sleep(0.15)
                continue
            time.sleep(0.1)
    finally:
        stdscr.nodelay(False)
        if proc:
            try:
                proc.terminate()
            except OSError:
                pass
        if audio_proc:
            try:
                audio_proc.terminate()
            except OSError:
                pass

    evidence = _ports_evidence(slots, status)
    remaining = [slot["label"] for slot in slots if not status.get(slot["id"], False)]
    remarks = "" if result == "PASS" else "not detected: " + ", ".join(remaining)
    return qc.make_result("ports", applicable=True, ran=True, result=result,
                          evidence=evidence, remarks=remarks)


def screen_qc_ports(stdscr) -> dict:
    probe = qc.PROBES["ports"]()
    while True:
        # Audio output has its own routed playback test immediately after the
        # microphone test; do not make the operator confirm the same jack twice.
        profile = [
            slot for slot in qc.detect_port_profile()
            if slot.get("kind") != "audio"
        ]
        slots = _confirm_port_inventory(stdscr, profile)
        if not slots:
            return qc.na_result("ports", probe["evidence"])
        result = _run_ports_matrix(stdscr, slots)
        if result.get("_action") == "reselect":
            continue
        return result


def screen_qc_wireless(stdscr) -> dict:
    stdscr.erase()
    draw_header(stdscr, "QC - Wi-Fi / Bluetooth")
    center_block(stdscr, [
        ("Scanning nearby Wi-Fi SSIDs and Bluetooth adapter...", curses.A_BOLD),
        ("Wi-Fi passes when at least one SSID is found.",
         curses.color_pair(DIM_PAIR)),
    ], top_offset=4)
    draw_footer(stdscr, "Please wait")
    stdscr.refresh()
    check = qc.run_wireless_check()
    result = "PASS" if check["ok"] else "FAIL"
    color = GREEN_PAIR if result == "PASS" else RED_PAIR
    ssid_text = ", ".join(check.get("ssids") or [])
    hidden_count = int(check.get("wifi_hidden_count") or 0)
    if hidden_count:
        hidden_text = f"{hidden_count} hidden network" + ("s" if hidden_count != 1 else "")
        ssid_text = f"{ssid_text}, {hidden_text}" if ssid_text else hidden_text
    if not ssid_text:
        ssid_text = "none"
    wifi_ifaces = ", ".join(check.get("wifi_interfaces") or []) or "none"
    stdscr.erase()
    draw_header(stdscr, "QC - Wi-Fi / Bluetooth")
    center_block(stdscr, [
        (f"Result: {result}", curses.A_BOLD | curses.color_pair(color)),
        ("", 0),
        (f"Wi-Fi adapter: {wifi_ifaces}",
         curses.color_pair(GREEN_PAIR if check.get("wifi_interfaces") else RED_PAIR)),
        (f"Wi-Fi SSIDs found: {ssid_text}",
         curses.color_pair(GREEN_PAIR if check["wifi_ok"] else RED_PAIR)),
        (f"Bluetooth: {'PASS' if check['bluetooth_ok'] else 'FAIL'}",
         curses.color_pair(GREEN_PAIR if check["bluetooth_ok"] else RED_PAIR)),
    ], top_offset=4)
    draw_footer(stdscr, "Continuing in 3 seconds   ENTER skip")
    stdscr.refresh()
    _wait_with_skip(stdscr, 3)
    remarks = "" if result == "PASS" else "Wi-Fi SSID scan or Bluetooth adapter failed"
    row = qc.make_result("wireless", applicable=True, ran=True, result=result,
                         evidence=check["evidence"], remarks=remarks)
    row["wifi_status"] = "PASS" if check["wifi_ok"] else "FAIL"
    row["bluetooth_status"] = "PASS" if check["bluetooth_ok"] else "FAIL"
    return row


# Map of test key → screen function — used by the controller
def _touch_event_devices():
    try:
        from evdev import InputDevice, ecodes, list_devices  # type: ignore
    except ImportError:
        return [], None

    preferred_paths = {
        item.split()[0]
        for item in getattr(qc, "touchscreen_devices", lambda: [])()
        if item.startswith("/dev/input/event")
    }
    devices = []
    # Open udev-detected paths first, but always inspect every event node.
    # HID-over-I2C panels commonly expose separate feature, pen, and touch
    # event devices. The display probe may identify one sibling while the
    # usable coordinate stream lives on another.
    discovered_paths = set(list_devices())
    try:
        discovered_paths.update(
            os.path.join("/dev/input", name)
            for name in os.listdir("/dev/input")
            if name.startswith("event")
        )
    except OSError:
        pass
    candidate_paths = sorted(preferred_paths) + sorted(discovered_paths - preferred_paths)
    for path in candidate_paths:
        try:
            dev = InputDevice(path)
            caps = dev.capabilities()
            key_codes = set(caps.get(ecodes.EV_KEY, []))
            abs_codes = set(caps.get(ecodes.EV_ABS, []))
            has_mt = ecodes.ABS_MT_POSITION_X in abs_codes and ecodes.ABS_MT_POSITION_Y in abs_codes
            has_xy = ecodes.ABS_X in abs_codes and ecodes.ABS_Y in abs_codes
            finger_code = getattr(ecodes, "BTN_TOOL_FINGER", -1)
            has_touch_key = ecodes.BTN_TOUCH in key_codes or finger_code in key_codes
            try:
                input_props = set(dev.input_props())
            except (OSError, AttributeError):
                input_props = set()
            direct_code = getattr(ecodes, "INPUT_PROP_DIRECT", -1)
            pointer_code = getattr(ecodes, "INPUT_PROP_POINTER", -1)
            udev_props = getattr(qc, "_udev_input_properties", lambda _path: {})(path)
            is_touch, _reason = qc._touch_device_classification(
                dev.name or "",
                has_mt_xy=has_mt,
                has_abs_xy=has_xy,
                has_touch_key=has_touch_key,
                input_prop_direct=direct_code in input_props,
                input_prop_pointer=pointer_code in input_props,
                udev_touchscreen=udev_props.get("ID_INPUT_TOUCHSCREEN") == "1",
                udev_touchpad=udev_props.get("ID_INPUT_TOUCHPAD") == "1",
            )
            if (path in preferred_paths and (has_mt or has_xy)) or is_touch:
                devices.append(dev)
            else:
                dev.close()
        except (OSError, PermissionError):
            continue
    return devices, ecodes


def _touch_grid_pattern(stdscr, evidence: str, timeout_sec: int = 600) -> tuple[str, str]:
    """Full-screen touch dead-spot map. Returns (PASS|FAIL, evidence)."""
    info = _fb0_geometry()
    # The touch map needs pixel-level drawing. The curses/text fallback can
    # only paint terminal cells, which makes some panels look like long black
    # stripes instead of separate squares. Use framebuffer mode by default and
    # keep curses only as an explicit fallback.
    use_text_grid = os.environ.get("VSTL_TOUCH_USE_CURSES", "").strip() == "1"
    use_fb_grid = (not use_text_grid) and bool(info and info[2] in (16, 24, 32))
    if info:
        fb_w, fb_h, _bpp, _stride = info
    else:
        fb_w, fb_h = qc.display_resolution_size() or (1280, 720)
    if not use_fb_grid and not use_text_grid:
        return "FAIL", f"{evidence}; touch grid failed: framebuffer unavailable for pixel touch map"

    devices, ecodes = [], None
    device_deadline = time.monotonic() + 8
    while time.monotonic() < device_deadline:
        devices, ecodes = _touch_event_devices()
        if devices and ecodes is not None:
            break
        try:
            subprocess.run(
                ["udevadm", "settle", "--timeout=2"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.25)
    if not devices or ecodes is None:
        return "FAIL", f"{evidence}; touch grid failed: no readable touch evdev device"

    import selectors

    if use_fb_grid:
        cols = max(12, min(24, fb_w // 70))
        rows = max(8, min(14, fb_h // 70))
    else:
        term_h, term_w = stdscr.getmaxyx()
        cols = max(10, min(20, term_w // 8))
        rows = max(6, min(10, max(1, term_h - 4) // 3))
    total = rows * cols
    filled: set[tuple[int, int]] = set()
    states = {}

    sel = selectors.DefaultSelector()
    for dev in devices:
        caps = dev.capabilities()
        abs_codes = set(caps.get(ecodes.EV_ABS, []))
        x_code = ecodes.ABS_MT_POSITION_X if ecodes.ABS_MT_POSITION_X in abs_codes else ecodes.ABS_X
        y_code = ecodes.ABS_MT_POSITION_Y if ecodes.ABS_MT_POSITION_Y in abs_codes else ecodes.ABS_Y
        try:
            x_info = dev.absinfo(x_code)
            y_info = dev.absinfo(y_code)
            states[dev.fd] = {
                "dev": dev,
                "x_code": x_code,
                "y_code": y_code,
                "x_min": x_info.min,
                "x_max": max(x_info.max, x_info.min + 1),
                "y_min": y_info.min,
                "y_max": max(y_info.max, y_info.min + 1),
                "x": None,
                "y": None,
                "active": True,
            }
            sel.register(dev, selectors.EVENT_READ)
        except (OSError, ValueError):
            continue

    if not states:
        for dev in devices:
            try:
                dev.close()
            except OSError:
                pass
        sel.close()
        return "FAIL", f"{evidence}; touch grid failed: touch coordinates unavailable"

    stdscr.erase()
    draw_header(stdscr, "QC - Touch Display")
    center_block(stdscr, [
        ("Touch display detected. Drag your finger over every square.", curses.A_BOLD),
        ("Blue squares turn green. All squares must become green to pass.", curses.color_pair(GREEN_PAIR)),
        ("If a dead spot prevents completion, press ESC twice within 1.5 seconds.", curses.color_pair(YELLOW_PAIR)),
    ], top_offset=4)
    draw_footer(stdscr, "Touch test starts now")
    stdscr.refresh()
    time.sleep(1.4)
    _set_console_cursor_visible(False)
    _physical_blank_tty(stdscr)
    graphics_mode = _set_console_graphics_mode(True) if use_fb_grid else False

    def draw_grid() -> None:
        nonlocal use_fb_grid
        if use_fb_grid:
            if _fb0_draw_touch_grid(filled, cols, rows):
                return
            raise RuntimeError("framebuffer draw failed")
        if use_text_grid:
            _curses_draw_touch_grid(stdscr, filled, cols, rows)
            return
        raise RuntimeError("touch text grid fallback disabled")

    try:
        draw_grid()
    except RuntimeError as exc:
        if graphics_mode:
            _set_console_graphics_mode(False)
        _set_console_cursor_visible(True)
        return "FAIL", f"{evidence}; touch grid failed: {exc}"
    stdscr.nodelay(True)
    previous_cursor = None
    try:
        previous_cursor = curses.curs_set(0)
    except curses.error:
        previous_cursor = None
    esc_window = 1.5
    last_esc_ts: float | None = None
    deadline = time.time() + timeout_sec
    last_draw = 0.0

    def mark_point(state: dict) -> bool:
        if state["x"] is None or state["y"] is None or not state["active"]:
            return False
        nx = (int(state["x"]) - state["x_min"]) / max(1, state["x_max"] - state["x_min"])
        ny = (int(state["y"]) - state["y_min"]) / max(1, state["y_max"] - state["y_min"])
        x = min(fb_w - 1, max(0, int(nx * fb_w)))
        y = min(fb_h - 1, max(0, int(ny * fb_h)))
        cell = (min(rows - 1, y * rows // fb_h), min(cols - 1, x * cols // fb_w))
        before = len(filled)
        filled.add(cell)
        return len(filled) != before

    try:
        while time.time() < deadline:
            ch = stdscr.getch()
            if ch == 27:
                now = time.time()
                if last_esc_ts is not None and now - last_esc_ts <= esc_window:
                    return "FAIL", f"{evidence}; touch grid operator fail {len(filled)}/{total}"
                last_esc_ts = now

            changed = False
            for key, _mask in sel.select(timeout=0.03):
                dev = key.fileobj
                state = states.get(dev.fd)
                if not state:
                    continue
                try:
                    for ev in dev.read():
                        if ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH:
                            state["active"] = ev.value != 0
                        elif ev.type == ecodes.EV_ABS:
                            if ev.code == state["x_code"]:
                                state["x"] = ev.value
                            elif ev.code == state["y_code"]:
                                state["y"] = ev.value
                            elif ev.code == ecodes.ABS_MT_TRACKING_ID:
                                state["active"] = ev.value >= 0
                            changed = mark_point(state) or changed
                except (OSError, BlockingIOError):
                    continue

            if changed or time.time() - last_draw > 1.0:
                try:
                    draw_grid()
                except RuntimeError as exc:
                    return "FAIL", f"{evidence}; touch grid failed: {exc}"
                last_draw = time.time()
            if len(filled) == total:
                return "PASS", f"{evidence}; touch grid passed {total}/{total}"
        return "FAIL", f"{evidence}; touch grid timeout {len(filled)}/{total}"
    finally:
        if graphics_mode:
            _set_console_graphics_mode(False)
        _set_console_cursor_visible(True)
        stdscr.nodelay(False)
        if previous_cursor is not None:
            try:
                curses.curs_set(previous_cursor)
            except curses.error:
                pass
        sel.close()
        for dev in devices:
            try:
                dev.close()
            except OSError:
                pass
        stdscr.erase()
        try:
            stdscr.clearok(True)
        except curses.error:
            pass
        stdscr.refresh()


def _show_touch_result(stdscr, verdict: str, evidence: str) -> None:
    passed = verdict == "PASS"
    match = re.search(r"(\d+)/(\d+)", evidence)
    coverage = f"Coverage: {match.group(1)}/{match.group(2)} blocks" if match else ""
    stdscr.erase()
    draw_header(stdscr, "QC - Touch Display Result")
    lines = [
        (
            f"RESULT: {verdict}",
            curses.A_BOLD | curses.color_pair(GREEN_PAIR if passed else RED_PAIR),
        ),
        ("", 0),
        (
            "All touch blocks were completed."
            if passed else
            "The touch map was incomplete or ended with ESC ESC.",
            curses.A_BOLD,
        ),
    ]
    if coverage:
        lines.append((coverage, curses.color_pair(CYAN_PAIR)))
    center_block(stdscr, lines, top_offset=5)
    draw_footer(stdscr, "Continuing in 3 seconds   ENTER skip")
    stdscr.refresh()
    _wait_with_skip(stdscr, 3)


def _touch_launch_failure_choice(stdscr, evidence: str) -> bool:
    """Return True to retry a touch-grid launch, False to record failure."""
    reason = evidence.rsplit("touch grid failed:", 1)[-1].strip()
    while True:
        stdscr.erase()
        draw_header(stdscr, "QC - Touch Display")
        center_block(stdscr, [
            ("Touch display detected, but the touch map could not start.",
             curses.A_BOLD | curses.color_pair(RED_PAIR)),
            ("", 0),
            (reason[:100], curses.color_pair(YELLOW_PAIR)),
            ("", 0),
            ("[ R ] Retry touch input initialization", curses.A_BOLD),
            ("[ F ] Mark touch test as FAIL and continue", curses.A_BOLD),
        ], top_offset=4)
        draw_footer(stdscr, "R retry   F fail")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("r"), ord("R")):
            return True
        if ch in (ord("f"), ord("F"), 27):
            return False


def screen_qc_touchscreen(stdscr) -> dict:
    """Auto-detect display type, then run full-screen grid only on touch LCDs."""
    probe = qc.PROBES["touchscreen"]()
    resolution = qc.display_resolution_label()
    resolution_evidence = f"display_resolution={resolution}"
    operator_override = False
    if not probe["applicable"]:
        stdscr.erase()
        draw_header(stdscr, "QC - Display Type")
        center_block(stdscr, [
            ("Auto-detected: NON-TOUCH display", curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
            (f"Display resolution: {resolution}", curses.A_BOLD | curses.color_pair(CYAN_PAIR)),
            ("Touch test skipped. Moving to the next QC step in 3 seconds.", curses.color_pair(DIM_PAIR)),
        ], top_offset=5)
        draw_footer(stdscr, "Continuing in 3 seconds   ENTER skip   T force touch test")
        stdscr.refresh()
        deadline = time.monotonic() + 3
        stdscr.nodelay(True)
        try:
            while time.monotonic() < deadline:
                ch = stdscr.getch()
                if ch in (ord("t"), ord("T")):
                    operator_override = True
                    break
                if ch in (10, 13, curses.KEY_ENTER):
                    break
                time.sleep(0.05)
        finally:
            stdscr.nodelay(False)
        if not operator_override:
            return qc.na_result("touchscreen", f"{probe['evidence']}; {resolution_evidence}")

    stdscr.erase()
    draw_header(stdscr, "QC - Display Type")
    center_block(stdscr, [
        (
            "Operator override: TOUCH display" if operator_override
            else "Auto-detected: TOUCH display",
            curses.A_BOLD | curses.color_pair(GREEN_PAIR),
        ),
        (f"Display resolution: {resolution}", curses.A_BOLD | curses.color_pair(CYAN_PAIR)),
        ("Opening full-screen touch map in 3 seconds.", curses.color_pair(DIM_PAIR)),
    ], top_offset=5)
    draw_footer(stdscr, "Touch test starts in 3 seconds   ENTER skip")
    stdscr.refresh()
    _wait_with_skip(stdscr, 3)

    touch_evidence = (
        f"display_type=touch; {resolution_evidence}; {probe['evidence']}; "
        f"operator_override={str(operator_override).lower()}"
    )
    while True:
        verdict, evidence = _touch_grid_pattern(stdscr, touch_evidence)
        if "touch grid failed:" not in evidence:
            break
        if not _touch_launch_failure_choice(stdscr, evidence):
            break
    _show_touch_result(stdscr, verdict, evidence)
    remarks = "" if verdict == "PASS" else "operator failed touch grid with ESC ESC or grid timed out"
    return qc.make_result(
        "touchscreen",
        applicable=True,
        ran=True,
        result=verdict,
        evidence=evidence,
        remarks=remarks,
    )


QC_SCREEN_DISPATCH = {
    "display":       screen_qc_display,
    "keyboard":      screen_qc_keyboard,
    "camera":        screen_qc_camera,
    "fingerprint":   screen_qc_fingerprint,
    "speaker":       screen_qc_speaker,
    "microphone":    screen_qc_microphone,
    "audio_jack":    screen_qc_audio_jack,
    "wireless":      screen_qc_wireless,
    "touchscreen":   screen_qc_touchscreen,
    "ports":         screen_qc_ports,
    "storage":       screen_qc_storage,
    "power_adapter": screen_qc_power_adapter,
}


def screen_qc_summary(stdscr, summary: dict, layer: str) -> str:
    """Final summary of all 4/8 QC tests. Returns 'continue' or 'rework'."""
    stdscr.erase()
    draw_header(stdscr, "Phase 2B — QC Summary")
    h, w = stdscr.getmaxyx()
    lines: list[tuple[str, int]] = [
        (summary["summary"], curses.A_BOLD),
        ("", 0),
    ]
    for key in ("driver_preflight", *qc.TEST_ORDER):
        t = summary["tests"].get(key)
        if not t:
            continue
        if t["result"] == "PASS":
            lines.append((f"✓  {t['label']:<14}  PASS",
                          curses.color_pair(GREEN_PAIR)))
        elif t["result"] == "FAIL":
            lines.append((f"✗  {t['label']:<14}  FAIL  —  {t['remarks'][: w - 30]}",
                          curses.A_BOLD | curses.color_pair(RED_PAIR)))
        elif t["result"] == "SKIP":
            lines.append((f"·  {t['label']:<14}  SKIP  —  {t['remarks'][: w - 30]}",
                          curses.color_pair(YELLOW_PAIR)))
        elif t["result"] == "NA":
            na_reason = (
                "present, live driver unavailable"
                if t.get("applicable") else "hardware not present"
            )
            lines.append((f"-  {t['label']:<14}  N/A ({na_reason})",
                          curses.color_pair(DIM_PAIR)))
    lines.append(("", 0))
    if not summary["failed"]:
        lines.append(("All applicable QC tests passed.",
                      curses.A_BOLD | curses.color_pair(GREEN_PAIR)))
        decision = "continue"
    elif layer == "L1":
        lines.append(("L1 layer — failures recorded with remarks. Continuing.",
                      curses.color_pair(YELLOW_PAIR)))
        decision = "continue"
    elif layer == "TESTING":
        lines.append(("Testing mode - failures shown locally only. Continuing.",
                      curses.color_pair(YELLOW_PAIR)))
        decision = "continue"
    else:  # L2
        lines.append(("L2 layer — unit will be ROUTED BACK for rework.",
                      curses.A_BOLD | curses.color_pair(RED_PAIR)))
        decision = "rework"
    center_block(stdscr, lines, top_offset=2)
    draw_footer(stdscr, "ENTER continue")
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return decision


# ---------------------------------------------------------------------------
# Phase 2C — Burn / Stress live screens
# ---------------------------------------------------------------------------
def screen_burn_intro(stdscr, layer: str, duration_sec: int) -> bool:
    """Intro screen — L1 may skip with remarks. Returns True to run, False to skip."""
    stdscr.erase()
    draw_header(stdscr, "Phase 2C — Burn / Stress Test")
    minutes = duration_sec // 60
    lines = [
        (f"This will run a {minutes}-minute concurrent burn-in:",
         curses.A_BOLD),
        ("", 0),
        ("• stress-ng    — CPU load, full duration",  curses.color_pair(DIM_PAIR)),
        ("• thermal mon  — temperature watcher",        curses.color_pair(DIM_PAIR)),
        ("• memtester    — RAM verification (alternating slices)",
         curses.color_pair(DIM_PAIR)),
        ("• fio          — disk I/O on tmpfs (alternating slices)",
         curses.color_pair(DIM_PAIR)),
        ("", 0),
        ("Threshold: thermal throttle ≥ 100°C → FAIL",
         curses.color_pair(YELLOW_PAIR)),
    ]
    if layer == "L1":
        lines += [
            ("", 0),
            ("[ ENTER ]  start the burn",
             curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
            ("[   S   ]  SKIP  —  L1 layer only, requires remarks",
             curses.color_pair(YELLOW_PAIR)),
        ]
    else:
        lines += [
            ("", 0),
            ("[ ENTER ]  start the burn (L2 cannot skip)",
             curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
        ]
    center_block(stdscr, lines, top_offset=3)
    draw_footer(stdscr, "ENTER start" + ("   S skip (L1 only)" if layer == "L1" else ""))
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return True
        if layer == "L1" and ch in (ord("s"), ord("S")):
            return False


def screen_burn_progress(stdscr, duration_sec: int, cfg: dict) -> dict:
    """Run the burn test with a live progress display, return the result dict."""

    def _draw(state: dict) -> None:
        stdscr.erase()
        draw_header(stdscr, "Phase 2C — Burn Test in progress")
        h, w = stdscr.getmaxyx()
        elapsed = state.get("elapsed_sec", 0)
        remaining = state.get("remaining_sec", 0)
        cur_t = state.get("current_temp_c")
        max_t = state.get("max_temp_c")
        fan_status = state.get("fan_status") or "NO SENSOR"
        fan_rpm = state.get("current_fan_rpm")
        phase = state.get("ram_disk_phase", "?")
        errors = state.get("errors", {})

        # Progress bar
        bar_width = max(10, w - 16)
        pct = elapsed / max(1, duration_sec)
        filled = int(bar_width * min(1.0, pct))
        bar = "█" * filled + "░" * (bar_width - filled)

        lines = [
            (f"Elapsed: {elapsed:>4}s   Remaining: {remaining:>4}s   "
             f"({int(pct * 100):>3}%)",
             curses.A_BOLD),
            ("", 0),
            (f"  [{bar}]", curses.color_pair(GREEN_PAIR) | curses.A_BOLD),
            ("", 0),
            ("CPU stress    : running",          curses.color_pair(DIM_PAIR)),
            (f"RAM/disk phase: {phase.upper()}",  curses.color_pair(YELLOW_PAIR)),
            (f"Current temp  : {cur_t if cur_t is not None else '—'} °C",
             curses.color_pair(DIM_PAIR)),
            (f"Max temp seen : {max_t if max_t is not None else '—'} °C",
             curses.A_BOLD),
            (f"CPU fan       : {fan_status}"
             + (f" ({fan_rpm} RPM)" if fan_rpm is not None else ""),
             curses.color_pair(DIM_PAIR)),
            ("", 0),
            (f"Errors  → memtester: {errors.get('memtester', 0)}   "
             f"fio: {errors.get('fio', 0)}   "
             f"stress-ng warnings: {errors.get('stress_ng_warnings', 0)}",
             curses.color_pair(DIM_PAIR)),
        ]
        center_block(stdscr, lines, top_offset=3)
        draw_footer(stdscr, "Burn-in running — DO NOT power off")
        stdscr.refresh()

    return bs.run_burn_test(
        duration_sec=duration_sec,
        progress_callback=_draw,
        cfg=cfg,
    )


def screen_burn_result(stdscr, result: dict, layer: str) -> str:
    """Final burn-test summary. Returns 'continue' or 'rework'."""
    stdscr.erase()
    draw_header(stdscr, "Phase 2C — Burn Result")
    color = (
        GREEN_PAIR if result["result"] == "PASS"
        else YELLOW_PAIR if result["result"] == "SKIP"
        else RED_PAIR
    )
    lines = [
        (f"Result : {result['result']}",
         curses.A_BOLD | curses.color_pair(color)),
        ("", 0),
        (f"Duration       : {_fmt_duration(result.get('actual_duration_sec', 0))}",
         curses.color_pair(DIM_PAIR)),
        (f"Max temp       : {result.get('max_temp_c') or '—'} °C",
         curses.color_pair(DIM_PAIR)),
        (f"Throttled      : {'YES' if result.get('throttled') else 'no'}",
         curses.color_pair(DIM_PAIR)),
        (f"CPU fan        : {result.get('fan_status') or 'NO SENSOR'}"
         + (f" (max {result.get('fan_max_rpm')} RPM)" if result.get("fan_max_rpm") else ""),
         curses.color_pair(DIM_PAIR)),
        (f"memtester errs : {result.get('errors', {}).get('memtester', 0)}",
         curses.color_pair(DIM_PAIR)),
        (f"fio errs       : {result.get('errors', {}).get('fio', 0)}",
         curses.color_pair(DIM_PAIR)),
    ]
    if result.get("remarks"):
        lines += [("", 0),
                  (f"Remarks: {result['remarks']}",
                   curses.A_BOLD | curses.color_pair(YELLOW_PAIR))]
    if result["result"] == "FAIL" and layer == "L2":
        lines += [("", 0),
                  ("L2 layer — unit will be ROUTED BACK for rework.",
                   curses.A_BOLD | curses.color_pair(RED_PAIR))]
        decision = "rework"
    else:
        decision = "continue"
    center_block(stdscr, lines, top_offset=3)
    draw_footer(stdscr, "ENTER continue")
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return decision


# ---------------------------------------------------------------------------
# Hardware-detect screens (3-second auto-advance, ENTER skips immediately)
# ---------------------------------------------------------------------------
DETECT_AUTO_ADVANCE = 3


def _wait_with_skip(stdscr, secs: int) -> None:
    """Sleep for `secs` seconds, returning early on ENTER. Other keys ignored."""
    deadline = time.monotonic() + secs
    stdscr.nodelay(True)
    try:
        while time.monotonic() < deadline:
            ch = stdscr.getch()
            if ch in (10, 13, curses.KEY_ENTER):
                return
            time.sleep(0.05)
    finally:
        stdscr.nodelay(False)


def _show_detect_screen(stdscr, title: str, lines: list[tuple[str, int]],
                        auto_secs: int = DETECT_AUTO_ADVANCE) -> None:
    """Generic detect screen: header, the lines, countdown footer, then auto-advance."""
    end = time.monotonic() + auto_secs
    stdscr.nodelay(True)
    try:
        while time.monotonic() < end:
            stdscr.erase()
            draw_header(stdscr, title)
            center_block(stdscr, lines, top_offset=5)
            remaining = max(0, int(end - time.monotonic() + 0.999))
            draw_footer(stdscr, f"Auto-advance in {remaining}s   ENTER skip")
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (10, 13, curses.KEY_ENTER):
                return
            time.sleep(0.1)
    finally:
        stdscr.nodelay(False)


def screen_model(stdscr, ident: dict) -> None:
    _show_detect_screen(stdscr, "1/7 — Brand & Model", [
        ("Brand", curses.color_pair(DIM_PAIR)),
        (ident["brand"], curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
        ("", 0),
        ("Model", curses.color_pair(DIM_PAIR)),
        (ident["model"], curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
    ])


def screen_sku(stdscr, ident: dict) -> None:
    sku_color = GREEN_PAIR if ident["sku"] != hw.UNKNOWN else YELLOW_PAIR
    mac = ident.get("mac_id") or hw.UNKNOWN
    mac_color = GREEN_PAIR if mac != hw.UNKNOWN else YELLOW_PAIR
    _show_detect_screen(stdscr, "2/7 — SKU / Part Number", [
        ("Serial No.", curses.color_pair(DIM_PAIR)),
        (ident["serial_no"], curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
        ("", 0),
        ("SKU / Unit Part Number", curses.color_pair(DIM_PAIR)),
        (ident["sku"], curses.A_BOLD | curses.color_pair(sku_color)),
        ("", 0),
        ("MAC ID", curses.color_pair(DIM_PAIR)),
        (mac, curses.A_BOLD | curses.color_pair(mac_color)),
    ])


def screen_cpu(stdscr, cpu: dict) -> None:
    _show_detect_screen(stdscr, "3/7 — CPU", [
        ("Processor", curses.color_pair(DIM_PAIR)),
        (cpu["cpu"], curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
        ("", 0),
        (f"Cores: {cpu['cores']}    Threads/core: {cpu['threads_per_core']}",
         curses.color_pair(DIM_PAIR)),
    ])


def screen_gpu(stdscr, gpu: dict) -> None:
    _show_detect_screen(stdscr, "4/7 — GPU", [
        (f"Type: {gpu['gpu_kind']}", curses.color_pair(DIM_PAIR)),
        (gpu["gpu"], curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
        ("", 0),
        (f"Memory: {gpu['gpu_memory']}", curses.color_pair(DIM_PAIR)),
    ])


def screen_ram(stdscr, ram: dict) -> None:
    _show_detect_screen(stdscr, "5/7 — RAM", [
        ("Installed Memory", curses.color_pair(DIM_PAIR)),
        (ram["ram"], curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
    ])


def screen_storage(stdscr, storage: dict) -> None:
    if not storage["drives"]:
        lines = [("No storage devices detected", curses.color_pair(RED_PAIR) | curses.A_BOLD)]
    else:
        lines = []
        for d in storage["drives"]:
            health_color = GREEN_PAIR
            try:
                pct = int(str(d["health"]).rstrip("%"))
                if pct < 80:
                    health_color = RED_PAIR
            except (ValueError, AttributeError):
                health_color = YELLOW_PAIR
            lines.append((
                f"{d['device']}  •  {d['size']}  •  {d['type']}",
                curses.color_pair(DIM_PAIR),
            ))
            lines.append((
                f"Health: {d['health']}",
                curses.A_BOLD | curses.color_pair(health_color),
            ))
            lines.append(("", 0))

        if storage["low_health"]:
            lines.append(("⚠  At least one drive < 80% — flagged Low Health",
                         curses.color_pair(YELLOW_PAIR)))

    _show_detect_screen(stdscr, f"6/7 — Storage ({storage['drive_count']} drive(s))", lines)


def screen_battery(stdscr, battery: dict) -> None:
    if battery["battery_count"] == 0:
        lines = [("No battery detected", curses.color_pair(YELLOW_PAIR) | curses.A_BOLD)]
    else:
        lines = []
        for b in battery["batteries"]:
            try:
                pct = int(str(b["health"]).rstrip("%"))
                health_color = GREEN_PAIR if pct >= 80 else RED_PAIR
            except (ValueError, AttributeError):
                health_color = YELLOW_PAIR
            lines.append((
                f"{b['name']}  •  Capacity {b['capacity_mwh']} mWh  •  Cycle {b['cycle_count']}",
                curses.color_pair(DIM_PAIR),
            ))
            lines.append((
                f"Health: {b['health']}",
                curses.A_BOLD | curses.color_pair(health_color),
            ))
            lines.append(("", 0))
        if battery["low_health"]:
            lines.append(("⚠  At least one battery < 80% — flagged Low Health",
                         curses.color_pair(YELLOW_PAIR)))

    _show_detect_screen(stdscr, f"7/7 — Battery ({battery['battery_count']})", lines)


# ---------------------------------------------------------------------------
# Final screen — submit + completion
# ---------------------------------------------------------------------------
def _apply_hardware_label_overrides(payload: dict, overrides: dict) -> None:
    raw = payload.setdefault("raw_data", {})
    paths = {
        "ram": ("ram", "modules"),
        "storage": ("storage", "drives"),
        "battery": ("battery", "batteries"),
    }
    for group_key, indexed_values in overrides.items():
        section_key, list_key = paths[group_key]
        items = (raw.get(section_key) or {}).get(list_key) or []
        for index, values in indexed_values.items():
            if 0 <= index < len(items):
                items[index].update(values)


def screen_submitting(stdscr, msg: str = "Sending data to VSTL 360 …") -> None:
    stdscr.erase()
    draw_header(stdscr, "Submitting bench audit")
    center_block(stdscr, [(msg, curses.color_pair(YELLOW_PAIR) | curses.A_BOLD)])
    draw_footer(stdscr, "Please wait")
    stdscr.refresh()


def _ingest_submission_id(payload: dict, cfg: dict) -> str:
    existing = str(payload.get("bench_submission_id") or "").strip()
    if existing:
        return existing
    basis = {
        "bench_id": _bench_id(cfg),
        "serial_no": payload.get("serial_no", ""),
        "mac_id": payload.get("mac_id", ""),
        "session_started_at": payload.get("session_started_at", ""),
        "selected_option": payload.get("selected_option", ""),
    }
    digest = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    submission_id = f"{_bench_id(cfg)}-{digest[:32]}"
    payload["bench_submission_id"] = submission_id
    return submission_id


def _local_pending_path(submission_id: str) -> str:
    safe_name = hashlib.sha256(submission_id.encode("utf-8")).hexdigest()
    return os.path.join(PENDING_INGEST_DIR, f"{safe_name}.json")


def _queue_pending_ingest(payload: dict, cfg: dict, operator: dict | None) -> tuple[bool, str]:
    submission_id = _ingest_submission_id(payload, cfg)
    envelope = {
        "submission_id": submission_id,
        "operator_user_id": str(_operator_user(operator).get("id") or ""),
        "operator_name": _operator_name(operator),
        "operator_session": _operator_session_snapshot(operator),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    ok, _data, msg = _bench_state_request(
        cfg,
        "POST",
        "queue",
        body={"bench_id": _bench_id(cfg), "envelope": envelope},
        timeout=12,
    )
    if ok:
        return True, "queued on imaging server"
    try:
        os.makedirs(PENDING_INGEST_DIR, mode=0o700, exist_ok=True)
        path = _local_pending_path(submission_id)
        if not os.path.exists(path):
            temporary = f"{path}.tmp-{os.getpid()}"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle, separators=(",", ":"))
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        return True, f"queued locally; server queue unavailable: {msg}"
    except OSError as exc:
        return False, f"could not queue completed audit: {exc}; {msg}"


def _list_pending_ingests(cfg: dict, operator_user_id: str) -> list[dict]:
    items: dict[str, dict] = {}
    ok, data, _msg = _bench_state_request(
        cfg,
        "GET",
        "queue",
        query={"operator_user_id": operator_user_id},
        timeout=10,
    )
    if ok:
        for item in data.get("items") or []:
            if isinstance(item, dict) and item.get("submission_id"):
                items[str(item["submission_id"])] = item
    try:
        for name in sorted(os.listdir(PENDING_INGEST_DIR)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(PENDING_INGEST_DIR, name), encoding="utf-8") as handle:
                item = json.load(handle)
            if (
                isinstance(item, dict)
                and str(item.get("operator_user_id") or "") == operator_user_id
                and item.get("submission_id")
            ):
                items.setdefault(str(item["submission_id"]), item)
    except (OSError, ValueError, TypeError):
        pass
    return sorted(items.values(), key=lambda item: str(item.get("created_at") or ""))


def _delete_pending_ingest(cfg: dict, submission_id: str) -> None:
    _bench_state_request(
        cfg,
        "DELETE",
        "queue",
        query={"submission_id": submission_id},
        timeout=8,
    )
    try:
        os.unlink(_local_pending_path(submission_id))
    except OSError:
        pass


def _operator_for_pending_ingest(envelope: dict, current_operator: dict | None) -> dict:
    """Prefer the saved operator token for this queued unit; fallback for legacy queues."""
    saved = envelope.get("operator_session")
    if isinstance(saved, dict) and _operator_token(saved):
        return saved
    queued_user_id = str(envelope.get("operator_user_id") or "")
    current_user_id = str(_operator_user(current_operator).get("id") or "")
    if queued_user_id and queued_user_id == current_user_id and _operator_token(current_operator):
        return current_operator or {}
    return {}


def _format_ingest_response(data: dict, payload: dict | None = None) -> str:
    details = []
    box = _normalize_box_scope(payload)
    if data.get("slot_filled"):
        destination = (
            f"{box['lot_no']} / {box['box_no']}"
            if box
            else "selected Lot / Box"
        )
        details.append(f"Imaged into {destination} - L1 complete")
    elif data.get("asset_created") or data.get("reconcile_status") == "PENDING":
        details.append("WARNING: Not in this box - flagged for reconcile")
    elif data.get("asset_matched"):
        details.append(f"asset matched by {data.get('match_method') or 'serial/MAC'}")
    if data.get("l1_completed") and not data.get("slot_filled"):
        details.append("official L1 audit completed")
    if data.get("certificate_id"):
        details.append(f"certificate={data['certificate_id']}")
    if data.get("record_id"):
        details.append(f"record_id={data['record_id']}")
    if data.get("internal_id"):
        details.append(f"internal_id={data['internal_id']}")
    if not details:
        details.append(str(data.get("message") or "cloud accepted audit"))
    return " | ".join(details)


def _post_ingest_once(
    payload: dict,
    cfg: dict,
    operator_token: str,
    clear_session_on_401: bool = True,
) -> tuple[bool, str, dict, int | None]:
    base = _api_base(cfg)
    key = cfg.get("VSTL_API_KEY", "")
    if not base or not key:
        return False, "VSTL_API_BASE / VSTL_API_KEY missing in /opt/vstl/config.env", {}, None

    url = f"{base}/imaging/ingest"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": key,
            "User-Agent": BENCH_USER_AGENT,
            **({"Authorization": f"Bearer {operator_token}"} if operator_token else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
            if data.get("success") or data.get("status") in ("success", "ok"):
                return True, _format_ingest_response(data, payload), data, resp.status
            return False, json.dumps(data)[:300], data if isinstance(data, dict) else {}, resp.status
    except urllib.error.HTTPError as e:
        if e.code == 401 and operator_token and clear_session_on_401:
            _clear_auth_session(cfg)
        return False, f"HTTP {e.code} {e.reason}: {e.read()[:300].decode('utf-8','replace')}", {}, e.code
    except urllib.error.URLError as e:
        return False, f"network error: {e.reason}", {}, None
    except (TimeoutError, OSError) as e:
        return False, f"connection error: {e}", {}, None


def flush_pending_ingests(
    cfg: dict,
    operator: dict | None,
) -> dict[str, tuple[bool, str, dict, int | None]]:
    """Upload this authenticated operator's queued audits, including old-token retries."""
    user_id = str(_operator_user(operator).get("id") or "")
    current_token = _operator_token(operator)
    if not user_id:
        return {}
    results = {}
    for envelope in _list_pending_ingests(cfg, user_id):
        submission_id = str(envelope.get("submission_id") or "")
        payload = envelope.get("payload")
        if not submission_id or not isinstance(payload, dict):
            continue
        queued_operator = _operator_for_pending_ingest(envelope, operator)
        token = _operator_token(queued_operator)
        if not token:
            continue
        result = _post_ingest_once(
            payload,
            cfg,
            token,
            clear_session_on_401=bool(current_token and token == current_token),
        )
        results[submission_id] = result
        if result[0]:
            _delete_pending_ingest(cfg, submission_id)
            _refresh_operator_boxes_after_ingest(cfg, operator)
            continue
        if result[3] == 401 or result[3] is None:
            break
    return results


def post_ingest(payload: dict, cfg: dict, operator: dict | None = None) -> tuple[bool, str]:
    """Durably queue and then upload one completed unit under its operator."""
    submission_id = _ingest_submission_id(payload, cfg)
    queued, queue_msg = _queue_pending_ingest(payload, cfg, operator)
    if not queued:
        return False, queue_msg
    results = flush_pending_ingests(cfg, operator)
    if submission_id not in results:
        return False, f"saved for cloud retry; {queue_msg}"
    ok, message, data, status = results[submission_id]
    if ok:
        return True, message
    if status == 401:
        return False, "operator session expired; audit queued safely - login again to send"
    return False, f"audit queued safely for retry; {message}"


def _attach_audit_submission_status(payload: dict, ok: bool, message: str) -> None:
    """Persist the final cloud submission outcome for local/server reports."""
    recorded_at = datetime.now(timezone.utc).isoformat()
    status = "Audit Submitted" if ok else "Submission Failed"
    payload["audit_submission_status"] = status
    payload["audit_submission_ok"] = bool(ok)
    payload["audit_submission_message"] = str(message or "")
    payload["audit_submission_recorded_at"] = recorded_at
    payload["cloud_audit"] = {
        "ok": bool(ok),
        "status": status,
        "message": str(message or ""),
        "recorded_at": recorded_at,
    }


# ---------------------------------------------------------------------------
# Phase 3 — Generic API helpers + Erase / Capture / Restore screens
# ---------------------------------------------------------------------------
def _cfg_enabled(cfg: dict, name: str, default: bool = True) -> bool:
    raw = str(cfg.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off", "disabled"}


def _read_first_line(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.readline().strip()
    except OSError:
        return ""


def _default_network_interface() -> str:
    try:
        result = subprocess.run(
            ["ip", "-o", "route", "show", "default"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=4,
            check=False,
        )
        if result.returncode == 0:
            parts = result.stdout.split()
            if "dev" in parts:
                iface = parts[parts.index("dev") + 1].strip()
                if iface:
                    return iface
    except (OSError, subprocess.TimeoutExpired):
        pass

    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=4,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    blocked_prefixes = ("lo", "br-", "docker", "tailscale", "veth", "virbr")
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        iface = fields[1].split("@", 1)[0]
        if iface and not iface.startswith(blocked_prefixes):
            return iface
    return ""


def _log_dhcp_release(message: str, cfg: dict | None = None) -> None:
    path = (cfg or {}).get("VSTL_DHCP_RELEASE_LOG_FILE") or DHCP_RELEASE_LOG_FILE
    line = f"{datetime.now(timezone.utc).isoformat()} {message}\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        if path == "/tmp/vstl-dhcp-release.log":
            return
        try:
            with open("/tmp/vstl-dhcp-release.log", "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


def _run_release_command(command: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=8,
            check=False,
        )
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def release_successful_audit_dhcp_lease(cfg: dict, ingest_ok: bool) -> tuple[bool, str]:
    """Release runtime DHCP only after the cloud accepted this unit audit."""
    if not ingest_ok:
        message = "not releasing DHCP lease; audit was not submitted"
        _log_dhcp_release(message, cfg)
        return False, message
    if not _cfg_enabled(cfg, "VSTL_RELEASE_DHCP_ON_AUDIT_SUBMITTED", True):
        return False, "DHCP release disabled"

    iface = str(cfg.get("VSTL_DHCP_RELEASE_IFACE", "")).strip()
    if not iface:
        iface_file = cfg.get("VSTL_DHCP_RELEASE_IFACE_FILE") or DHCP_RELEASE_IFACE_FILE
        iface = _read_first_line(iface_file)
    if not iface:
        iface = _default_network_interface()
    if not iface:
        message = "could not determine DHCP interface to release"
        _log_dhcp_release(message, cfg)
        return False, message

    commands: list[list[str]] = []
    if shutil.which("dhclient"):
        commands.append(["dhclient", "-r", iface])
    if shutil.which("dhcpcd"):
        commands.append(["dhcpcd", "-k", iface])
    if shutil.which("ip"):
        commands.append(["ip", "addr", "flush", "dev", iface])
    if not commands:
        message = f"no DHCP release tools available for {iface}"
        _log_dhcp_release(message, cfg)
        return False, message

    last_message = ""
    for command in commands:
        rc, output = _run_release_command(command)
        joined = " ".join(command)
        if rc == 0:
            message = f"released DHCP lease on {iface} using {joined}"
            _log_dhcp_release(message, cfg)
            return True, message
        last_message = f"{joined} rc={rc} {output}".strip()

    message = f"DHCP lease release failed on {iface}: {last_message}"
    _log_dhcp_release(message, cfg)
    return False, message


def post_local_report(payload: dict, cfg: dict) -> tuple[bool, str]:
    """Mirror the completed audit to the on-prem reporting service."""
    base = cfg.get("VSTL_REPORT_BASE", "").rstrip("/")
    if not base:
        server = (
            cfg.get("VSTL_SERVER_IP")
            or cfg.get("SERVER_IP")
            or "10.255.0.75"
        ).strip()
        base = f"http://{server}/vstl-reports"
    token = cfg.get("VSTL_REPORT_TOKEN") or cfg.get("VSTL_API_KEY", "")
    if not token:
        return False, "local report token unavailable; cloud audit still uses VSTL_API_KEY"
    url = f"{base}/ingest.php"
    attempts = int(os.environ.get("VSTL_REPORT_POST_ATTEMPTS", "4") or "4")
    attempts = max(1, attempts)
    delay = float(os.environ.get("VSTL_REPORT_POST_RETRY_DELAY", "2") or "2")
    last_msg = ""
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-VSTL-Report-Token": token,
                "User-Agent": BENCH_USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=18) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
                ok = bool(data.get("success"))
                msg = str(data.get("message") or "stored")
                if ok:
                    if attempt > 1:
                        return True, f"{msg} after {attempt} attempts"
                    return True, msg
                last_msg = msg
        except urllib.error.HTTPError as e:
            last_msg = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_msg = str(e)
        if attempt < attempts:
            time.sleep(delay)
    return False, f"local report failed after {attempts} attempts: {last_msg}"


def _attach_report_details(
    payload: dict,
    qc_summary: dict | None,
    preserved_os_info: dict | None = None,
) -> None:
    raw_data = payload.setdefault("raw_data", {})
    storage = raw_data.get("storage") or {}
    drives = storage.get("drives") or []
    primary_device = (drives[0] or {}).get("device", "") if drives else ""
    os_info = preserved_os_info if isinstance(preserved_os_info, dict) else None
    if not os_info:
        try:
            os_info = ic.inspect_installed_windows(primary_device)
        except Exception as exc:
            os_info = {
                "os_name": "Unknown",
                "os_version": "Unknown",
                "os_build": "",
                "evidence": f"inspection error: {exc}",
            }
    raw_data["os"] = os_info
    payload["installed_os"] = os_info.get("os_name", "Unknown")
    payload["os_version"] = os_info.get("os_version", "Unknown")

    if not qc_summary:
        return
    tests = qc_summary.get("tests") or {}
    display = tests.get("display") or {}
    touch = tests.get("touchscreen") or {}
    ports = tests.get("ports") or {}
    payload["display"] = {
        "resolution": qc.display_resolution_label(),
        "type": "Touch" if touch.get("applicable") else "Non-touch",
        "result": display.get("result", ""),
        "remarks": display.get("remarks", ""),
    }
    payload["ports_availability"] = ports.get("evidence", "")
    payload["ports_remarks"] = ports.get("remarks", "")


def _secure_erase_drive_report_row(drive: dict, result: dict) -> dict:
    size_gb = result.get("device_size_gb") or drive.get("device_size_gb") or ""
    return {
        "size": f"{size_gb} GB" if size_gb else "",
        "health": "",
        "vendor": "",
        "model_description": (
            result.get("device_model")
            or drive.get("device_model")
            or ""
        ),
        "storage_type": (
            result.get("device_type")
            or drive.get("device_type")
            or ""
        ),
        "serial_number": drive.get("device_serial", ""),
        "ct_number": "",
        "part_number": "",
    }


def _post_secure_erase_local_report(
    ident: dict,
    drive: dict,
    result: dict,
    certificate: dict,
    gate_record: dict,
    cfg: dict,
    tech: str,
    operator: dict | None = None,
) -> tuple[bool, str]:
    """Submit a durable local-report row as soon as erase is verified."""
    cert_id = str(
        certificate.get("certificate_id")
        or result.get("certificate_id")
        or ""
    )
    payload = hw.collect_phase1(technician_level=tech, bench_id=_bench_id(cfg))
    _attach_operator_to_payload(payload, operator)
    payload.update({
        "schema": "vstl_secure_erase_immediate_report_v2",
        "report_source": "bench_secure_erase_immediate",
        "session_started_at": datetime.now(timezone.utc).isoformat(),
        "technician_level": tech,
        "bench_id": _bench_id(cfg),
        "serial_no": ident.get("serial_no", "") or payload.get("serial_no", ""),
        "sku": ident.get("sku", "") or payload.get("sku", ""),
        "mac_id": ident.get("mac_id", "") or payload.get("mac_id", ""),
        "brand": ident.get("brand", "") or payload.get("brand", ""),
        "model": ident.get("model", "") or payload.get("model", ""),
        "status": "completed" if result.get("ok") else "failed",
        "selected_option": 3,
        "selected_option_label": "Certified Secure Erase",
        "secure_erase_reg_id": cert_id,
    })
    raw_storage = payload.setdefault("raw_data", {}).setdefault("storage", {})
    if not raw_storage.get("drives"):
        raw_storage["drive_count"] = 1
        raw_storage["drives"] = [_secure_erase_drive_report_row(drive, result)]
    payload["phase3"] = {
        "erase": {
            "ok": bool(result.get("ok")),
            "verified": bool(result.get("verified")),
            "method": result.get("method", ""),
            "wipe_method": result.get("method", ""),
            "wipe_standard": certificate.get("wipe_standard", ""),
            "clear_only_exception": bool(result.get("clear_only_exception")),
            "clear_only_exception_reason": result.get("clear_only_exception_reason", ""),
            "device": result.get("device", ""),
            "device_model": result.get("device_model", ""),
            "duration_sec": result.get("duration_sec", 0),
            "certificate_id": cert_id,
            "secure_erase_reg_id": cert_id,
            "verification_hash": certificate.get("verification_hash", ""),
            "certificate_status": certificate.get("certificate_status", ""),
            "remote_post_ok": certificate.get("remote_post_ok", False),
            "remote_error": certificate.get("remote_error", ""),
            "capture_gate_recorded": bool(gate_record.get("ok")),
            "capture_gate_path": gate_record.get("path", ""),
            "certificate": certificate,
        },
    }
    _attach_report_details(payload, None)
    return post_local_report(payload, cfg)


def _api_post(path: str, body: dict, cfg: dict,
               timeout: int = 30) -> tuple[bool, dict, str]:
    """POST JSON to /api{path} using X-API-Key auth. Returns (ok, parsed, raw_err)."""
    base = cfg.get("VSTL_API_BASE", "").rstrip("/")
    key = cfg.get("VSTL_API_KEY", "")
    if not base or not key:
        return False, {}, "VSTL_API_BASE / VSTL_API_KEY missing"
    req = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": key,
            "User-Agent": BENCH_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8") or "{}"), ""
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            detail = {"detail": e.reason}
        return False, detail, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return False, {}, f"network error: {e}"


def _api_get(path: str, cfg: dict, timeout: int = 30) -> tuple[bool, dict, str]:
    base = cfg.get("VSTL_API_BASE", "").rstrip("/")
    key = cfg.get("VSTL_API_KEY", "")
    if not base or not key:
        return False, {}, "VSTL_API_BASE / VSTL_API_KEY missing"
    req = urllib.request.Request(
        f"{base}{path}",
        headers={"X-API-Key": key, "User-Agent": BENCH_USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode("utf-8") or "{}"), ""
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            detail = {"detail": e.reason}
        return False, detail, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return False, {}, f"network error: {e}"


def _fallback_nfs_settings(cfg: dict) -> dict:
    """Local FOG image export used when backend NFS settings are unavailable."""
    return {
        "nfs_host": cfg.get("SERVER_IP") or cfg.get("FOG_IP") or "10.255.0.75",
        "nfs_share": cfg.get("VSTL_CAPTURE_NFS_SHARE") or "/images/dev",
        "mount_options": cfg.get("VSTL_CAPTURE_NFS_OPTIONS") or "rw,nolock,vers=3",
        "source": "local_fog_fallback",
    }


def _get_bench_nfs_settings(cfg: dict) -> dict:
    """Return backend NFS settings, silently falling back to local FOG defaults."""
    ok_s, nfs_settings, _err = _api_get("/imaging/bench-settings", cfg)
    if ok_s and isinstance(nfs_settings, dict):
        if not nfs_settings.get("nfs_host"):
            nfs_settings["nfs_host"] = cfg.get("SERVER_IP") or cfg.get("FOG_IP") or "10.255.0.75"
        if not nfs_settings.get("nfs_share"):
            nfs_settings["nfs_share"] = cfg.get("VSTL_CAPTURE_NFS_SHARE") or "/images/dev"
        if not nfs_settings.get("mount_options"):
            nfs_settings["mount_options"] = cfg.get("VSTL_CAPTURE_NFS_OPTIONS") or "rw,nolock,vers=3"
        return nfs_settings
    return _fallback_nfs_settings(cfg)


def _compact_api_error(body: dict, raw_err: str) -> str:
    """Return an operator-readable API error without exposing headers/secrets."""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):
        parts = []
        for item in detail[:3]:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            loc = ".".join(str(part) for part in item.get("loc", []) if part != "body")
            msg = str(item.get("msg") or item.get("type") or "").strip()
            parts.append(f"{loc}: {msg}" if loc and msg else msg or loc)
        detail_text = "; ".join(part for part in parts if part)
    elif isinstance(detail, dict):
        detail_text = json.dumps(detail, sort_keys=True)[:240]
    else:
        detail_text = str(detail or body.get("message") or body.get("error") or "").strip()
    combined = " - ".join(part for part in [raw_err, detail_text] if part)
    return combined[:300] if combined else raw_err[:300]


def _clear_only_exception_reason(ident: dict | None) -> str:
    text = " ".join(
        str((ident or {}).get(key) or "")
        for key in ("brand", "model", "model_label", "product_name")
    )
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower())
    compact = normalized.replace(" ", "")
    words = normalized.split()
    for vendor_tokens, family, model_tokens, reason in (
        (("hp", "hewlettpackard"), "elitebook", ("640", "g10"), "temporary HP EliteBook 640 G10 clear-only policy"),
        (("hp", "hewlettpackard"), "elitebook", ("850", "g5"), "temporary HP EliteBook 850 G5 clear-only policy"),
        (("hp", "hewlettpackard"), "elitebook", ("850", "g6"), "temporary HP EliteBook 850 G6 clear-only policy"),
        (("dell",), "latitude", ("5520",), "temporary Dell Latitude 5520 clear-only policy"),
    ):
        if (
            any(token in words or token in compact for token in vendor_tokens)
            and family in normalized
            and all(re.search(rf"\b{re.escape(token)}\b", normalized) is not None for token in model_tokens)
        ):
            return reason
    return ""


def _clear_exception_allowed(result: dict | None, ident: dict | None) -> bool:
    method = str((result or {}).get("method") or "").strip()
    return (
        bool((result or {}).get("clear_only_exception"))
        and method in _CLEAR_WIPE_METHOD_STANDARDS
        and bool(_clear_only_exception_reason(ident))
    )


def _wipe_standard(method: str, allow_clear_exception: bool = False) -> str:
    method = (method or "").strip()
    if method in _WIPE_METHOD_STANDARDS:
        return _WIPE_METHOD_STANDARDS[method]
    if allow_clear_exception and method in _CLEAR_WIPE_METHOD_STANDARDS:
        return _CLEAR_WIPE_METHOD_STANDARDS[method]
    return _UNSUPPORTED_WIPE_STANDARD


def _is_certifiable_wipe_method(method: str, allow_clear_exception: bool = False) -> bool:
    method = (method or "").strip()
    return (
        method in _WIPE_METHOD_STANDARDS
        or (allow_clear_exception and method in _CLEAR_WIPE_METHOD_STANDARDS)
    )


def _is_certifiable_wipe_result(result: dict | None, ident: dict | None) -> bool:
    method = str((result or {}).get("method") or "").strip()
    return _is_certifiable_wipe_method(
        method,
        allow_clear_exception=_clear_exception_allowed(result, ident),
    )


def _cloud_certificate_compat_wipe_method(method: str) -> str:
    """Return a temporary remote enum fallback, if one is allowed.

    NVMe Clear-class methods are not allowed for new certified wipes, so the
    bench must not translate them to BLKDISCARD for remote certificate posting.
    Keeping this hook as a no-op preserves the call site while making the
    policy explicit.
    """
    return ""


def _hash_json(value: dict) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _local_secure_erase_certificate(
    ident: dict,
    drive: dict,
    result: dict,
    cfg: dict,
    tech: str,
) -> dict:
    """Issue the bench-local certificate for a verified successful erase.

    The cloud certificate endpoint is still called, but it is not allowed to
    be the only source of a certificate. A successful, verified sanitize must
    leave a local certificate ID in the report and NFS authorization record.
    """
    method = str(result.get("method") or "")
    allow_clear_exception = _clear_exception_allowed(result, ident)
    if not _is_certifiable_wipe_method(method, allow_clear_exception):
        raise ValueError(_UNSUPPORTED_WIPE_MESSAGE)
    evidence = str(result.get("evidence") or "")
    basis = {
        "schema": "vstl_secure_erase_certificate_v1",
        "serial_no": str(ident.get("serial_no") or ""),
        "mac_id": str(ident.get("mac_id") or ""),
        "bench_id": _bench_id(cfg),
        "brand": str(ident.get("brand") or ""),
        "model": str(ident.get("model") or ""),
        "device": str(result.get("device") or drive.get("device") or ""),
        "device_type": str(result.get("device_type") or drive.get("device_type") or ""),
        "device_model": str(result.get("device_model") or drive.get("device_model") or ""),
        "device_serial": str(drive.get("device_serial") or ""),
        "device_wwn": str(drive.get("device_wwn") or ""),
        "device_size_bytes": int(drive.get("device_size_bytes") or 0),
        "device_size_gb": result.get("device_size_gb") or drive.get("device_size_gb") or 0,
        "wipe_method": method,
        "wipe_standard": _wipe_standard(method, allow_clear_exception),
        "wipe_passes": int(result.get("passes") or 0),
        "wipe_started_at": str(result.get("started_at") or ""),
        "wipe_completed_at": str(result.get("completed_at") or ""),
        "duration_sec": int(result.get("duration_sec") or 0),
        "wipe_verified": bool(result.get("verified")),
        "verification_method": str(result.get("verification_method") or ""),
        "technician_level": str(tech or ""),
        "clear_only_exception": bool(result.get("clear_only_exception")),
        "clear_only_exception_reason": str(result.get("clear_only_exception_reason") or ""),
        "evidence_sha256": hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
    }
    verification_hash = _hash_json(basis)
    completed_at = str(result.get("completed_at") or "")
    date_token = re.sub(r"[^0-9]", "", completed_at[:10]) or datetime.now(timezone.utc).strftime("%Y%m%d")
    return {
        **basis,
        "certificate_id": f"SE-{date_token}-{verification_hash[:16].upper()}",
        "verification_hash": verification_hash,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "issuer": "VSTL Bench Local Certificate",
        "certificate_status": "issued",
        "remote_post_ok": False,
        "remote_error": "",
    }


def _merge_secure_erase_certificate(
    local_certificate: dict,
    remote_ok: bool,
    remote_resp: dict,
    remote_error: str,
) -> dict:
    certificate = dict(local_certificate)
    certificate["remote_post_ok"] = bool(remote_ok)
    certificate["remote_error"] = "" if remote_ok else remote_error[:300]
    if isinstance(remote_resp, dict):
        remote_id = str(remote_resp.get("certificate_id") or "").strip()
        remote_hash = str(remote_resp.get("verification_hash") or "").strip()
        if remote_id and remote_id != certificate.get("certificate_id"):
            certificate["remote_certificate_id"] = remote_id
        if remote_hash and remote_hash != certificate.get("verification_hash"):
            certificate["remote_verification_hash"] = remote_hash
        for key in ("pdf_url", "certificate_url", "download_url", "record_id"):
            if remote_resp.get(key):
                certificate[key] = remote_resp.get(key)
    return certificate


def _restore_backup_available(stdscr, ident: dict, cfg: dict,
                              cpu_info: Optional[dict] = None) -> bool:
    """Block restore before QC/erase when no exact or fallback image exists."""
    nfs_settings = _get_bench_nfs_settings(cfg)
    local_lookup = ir.find_local_golden_copies(
        nfs_settings.get("nfs_host", ""),
        nfs_settings.get("nfs_share", ""),
        nfs_settings.get("mount_options", "rw,nolock,vers=3"),
        ident.get("model", ""),
        ident.get("sku") or ident.get("part_number") or "",
        (cpu_info or {}).get("cpu", "") or (cpu_info or {}).get("cpu_model", ""),
    )
    ir.umount_nfs()
    if local_lookup.get("status") == "found" and local_lookup.get("copies"):
        return True
    screen_restore_picker(stdscr, [])
    return False


# --- Phase 3 — Certified Secure Erase --------------------------------------
def screen_erase_intro(stdscr, drive: dict) -> bool:
    """Show detected drive details + IRREVERSIBLE warning. Returns True
    if the operator confirms Y, False on N (per founder choice 5b)."""
    while True:
        stdscr.erase()
        draw_header(stdscr, "Phase 3 — Certified Secure Erase")
        h, w = stdscr.getmaxyx()
        lines = [
            ("⚠  IRREVERSIBLE  ⚠",
             curses.A_BOLD | curses.color_pair(RED_PAIR)),
            ("All data on the drive below will be PERMANENTLY destroyed.",
             curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
            ("", 0),
            (f"Device   : {drive.get('device','(none detected)')}",
             curses.A_BOLD),
            (f"Type     : {drive.get('device_type','UNKNOWN')}",
             curses.A_BOLD),
            (f"Model    : {drive.get('device_model','(unknown)')}",
             curses.color_pair(DIM_PAIR)),
            (f"Capacity : {drive.get('device_size_gb',0)} GB",
             curses.color_pair(DIM_PAIR)),
            ("", 0),
            ("The strongest applicable sanitize will be auto-picked",
             curses.color_pair(DIM_PAIR)),
            ("(NVMe Sanitize → ATA Sanitize/Security-Erase → nwipe DoD 3-pass).",
             curses.color_pair(DIM_PAIR)),
            ("", 0),
            ("[ Y ]  Proceed — wipe the drive now",
             curses.A_BOLD | curses.color_pair(RED_PAIR)),
            ("[ N ]  Cancel — return to menu",
             curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
        ]
        center_block(stdscr, lines, top_offset=3)
        draw_footer(stdscr, "Press Y to wipe, N to cancel")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27):
            return False


def _draw_erase_progress(stdscr, drive: dict,
                          state: dict) -> None:
    stdscr.erase()
    draw_header(stdscr, "Phase 3 — Wiping drive…")
    lines = [
        (f"Device   : {drive.get('device','')}", curses.A_BOLD),
        (f"Method   : {state.get('method','auto-detect…')}",
         curses.color_pair(CYAN_PAIR) | curses.A_BOLD),
        (f"Phase    : {state.get('phase','starting…')}",
         curses.color_pair(YELLOW_PAIR)),
        (f"Elapsed  : {state.get('elapsed_sec',0)}s",
         curses.color_pair(DIM_PAIR)),
    ]
    if state.get("percent") is not None:
        lines.append((f"Progress : {state['percent']}%", curses.A_BOLD))
    lines += [
        ("", 0),
        ("Do NOT power off the bench during erase.",
         curses.A_BOLD | curses.color_pair(RED_PAIR)),
    ]
    center_block(stdscr, lines, top_offset=4)
    draw_footer(stdscr, "Wipe in progress — please wait")
    stdscr.refresh()


def screen_run_erase(stdscr, drive: dict) -> dict:
    """Execute the secure-erase run with a live progress redraw via
    the vstl_secure_erase.run_secure_erase progress_callback hook."""
    state = {"method": "auto-detect…", "phase": "starting…",
             "elapsed_sec": 0, "percent": None}

    def _progress(ev: dict) -> None:
        state.update({
            "method":      ev.get("method", state["method"]),
            "phase":       ev.get("phase", state["phase"]),
            "elapsed_sec": ev.get("elapsed_sec", state["elapsed_sec"]),
            "percent":     ev.get("percent", state["percent"]),
        })
        _draw_erase_progress(stdscr, drive, state)

    _draw_erase_progress(stdscr, drive, state)
    result = se.run_secure_erase(drive=drive, progress_callback=_progress)
    state["method"] = result.get("method") or state["method"]
    state["phase"] = "completed" if result.get("ok") else "failed"
    _draw_erase_progress(stdscr, drive, state)
    time.sleep(1.0)
    return result


def screen_erase_result(stdscr, result: dict, cert_resp: dict,
                         cert_ok: bool,
                         reporting_suppressed: bool = False) -> None:
    """Show summary + verification hash. Operator presses ENTER to continue."""
    stdscr.erase()
    _h, w = stdscr.getmaxyx()
    if result.get("ok") and reporting_suppressed:
        draw_header(stdscr, "Phase 3 - Erase OK / Testing Mode")
        top_attr = curses.A_BOLD | curses.color_pair(GREEN_PAIR)
        top_line = ("Erase completed; report submission is disabled", top_attr)
        detail_lines = [(
            "Testing mode did not issue a certificate or send a bench report.",
            curses.color_pair(DIM_PAIR),
        )]
    elif result.get("ok") and cert_ok:
        remote_ok = cert_resp.get("remote_post_ok")
        if remote_ok is False:
            draw_header(stdscr, "Phase 3 — Erase Certified / Server Pending")
            top_attr = curses.A_BOLD | curses.color_pair(GREEN_PAIR)
            top_line = ("✓  Erase completed and local certificate issued", top_attr)
            detail_lines = [(
                "Server certificate POST failed; local certificate is saved in the audit/report.",
                curses.color_pair(YELLOW_PAIR),
            )]
            remote_error = str(cert_resp.get("remote_error") or "").strip()
            if remote_error:
                wrapped = textwrap.wrap(
                    remote_error,
                    width=max(28, min(96, w - 12)),
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                detail_lines.extend(
                    (line, curses.color_pair(DIM_PAIR)) for line in wrapped[:3]
                )
        else:
            draw_header(stdscr, "Phase 3 — Erase Certified ✓")
            top_attr = curses.A_BOLD | curses.color_pair(GREEN_PAIR)
            top_line = ("✓  Erase completed and certificate issued", top_attr)
            detail_lines: list[tuple[str, int]] = []
    elif result.get("ok") and not cert_ok:
        draw_header(stdscr, "Phase 3 — Erase OK / Cert FAILED")
        top_attr = curses.A_BOLD | curses.color_pair(YELLOW_PAIR)
        top_line = ("⚠  Erase OK but certificate POST failed", top_attr)
        detail_lines = []
    else:
        draw_header(stdscr, "Phase 3 — Erase FAILED")
        top_attr = curses.A_BOLD | curses.color_pair(RED_PAIR)
        top_line = ("✗  Erase failed", top_attr)
        error_message = str(result.get("error_message", "erase failed")).strip()
        wrapped = textwrap.wrap(
            error_message,
            width=max(28, min(96, w - 12)),
            break_long_words=False,
            break_on_hyphens=False,
        ) or ["erase failed"]
        detail_lines = [(line, curses.color_pair(RED_PAIR)) for line in wrapped]

    lines = [top_line, ("", 0)]
    if detail_lines:
        lines.extend(detail_lines)
        lines.append(("", 0))
    lines += [
        (f"Device   : {result.get('device','')}", curses.A_BOLD),
        (f"Method   : {result.get('method','')}",
         curses.color_pair(CYAN_PAIR)),
        (f"Standard : {cert_resp.get('wipe_standard','')}",
         curses.color_pair(CYAN_PAIR)),
        (f"Duration : {_fmt_duration(result.get('duration_sec',0))}",
         curses.color_pair(DIM_PAIR)),
        (f"Verified : {'YES' if result.get('verified') else 'NO'}",
         curses.color_pair(GREEN_PAIR if result.get('verified') else YELLOW_PAIR)),
    ]
    if not reporting_suppressed:
        lines.append((
            f"Capture  : {'AUTHORIZED' if result.get('capture_gate_recorded') else 'NOT AUTHORIZED'}",
            curses.color_pair(
                GREEN_PAIR if result.get("capture_gate_recorded") else RED_PAIR
            ) | curses.A_BOLD,
        ))
    if not reporting_suppressed and cert_ok and cert_resp.get("certificate_id"):
        lines.append(("", 0))
        lines.append((f"Cert ID  : {cert_resp.get('certificate_id','')}",
                      curses.A_BOLD))
        lines.append((f"Verify   : ...{cert_resp.get('verification_hash','')[-32:]}",
                      curses.color_pair(DIM_PAIR)))
    center_block(stdscr, lines, top_offset=3)
    draw_footer(stdscr, "ENTER to continue   Q to quit")
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return
        if ch in (ord("q"), ord("Q")):
            sys.exit(2)


# --- Phase 3 — Capture Full System Image -----------------------------------
def screen_capture_readiness(stdscr, readiness: dict) -> bool:
    """Show the strict pre-capture gate result.

    Capture is allowed only when SKU, lock, account, credential, partition,
    and exact laptop-plus-drive secure-erase checks all pass.
    """
    stdscr.erase()
    ok = bool(readiness.get("ok"))
    draw_header(stdscr, "Phase 3 - Capture Readiness")
    if ok:
        lines = [
            ("Capture readiness checks passed.", curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
            ("", 0),
            (f"SKU / Part No.: {readiness.get('sku','')}", curses.A_NORMAL),
            (f"Model        : {readiness.get('model','')}", curses.A_NORMAL),
            (f"CPU          : {readiness.get('cpu','')}", curses.A_NORMAL),
            ("Secure erase : AUTHORIZED for this laptop and drive",
             curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
            ("", 0),
            ("ENTER to continue", curses.color_pair(DIM_PAIR)),
        ]
        center_block(stdscr, lines, top_offset=3)
        draw_footer(stdscr, "ENTER continue")
        stdscr.refresh()
        while True:
            ch = stdscr.getch()
            if ch in (10, 13, curses.KEY_ENTER, 27):
                return True

    issues = readiness.get("issues") or []
    lines = [
        ("System not ready for image capture. Please fix the issues and try again.",
         curses.A_BOLD | curses.color_pair(RED_PAIR)),
        ("", 0),
    ]
    for issue in issues[:10]:
        lines.append((f"- {issue}"[:110], curses.color_pair(YELLOW_PAIR)))
    if len(issues) > 10:
        lines.append((f"... {len(issues) - 10} more issue(s)", curses.color_pair(YELLOW_PAIR)))
    lines.extend([
        ("", 0),
        ("No backup was captured or stored.", curses.A_BOLD | curses.color_pair(RED_PAIR)),
    ])
    center_block(stdscr, lines, top_offset=2)
    draw_footer(stdscr, "ENTER return to menu")
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER, 27):
            return False


def _os_display(name: str = "", version: str = "", build: str = "") -> str:
    parts = [p for p in [name, version] if p]
    text = " ".join(parts).strip()
    if build and build not in text:
        text = f"{text} ({build})" if text else build
    return text or "--"


def screen_capture_intro(stdscr, ident: dict, drive: dict,
                          nfs_settings: dict,
                          image_plan: Optional[dict] = None) -> bool:
    """Confirm capture from the SUT into the configured NFS share."""
    image_plan = image_plan or {}
    os_text = _os_display(
        image_plan.get("os_name", ""),
        image_plan.get("os_version", ""),
        image_plan.get("os_build", ""),
    )
    while True:
        stdscr.erase()
        draw_header(stdscr, "Phase 3 — Capture Full System Image")
        lines = [
            ("Capture the SUT's primary disk as a new GOLDEN COPY.",
             curses.A_BOLD),
            ("", 0),
            (f"Model    : {ident.get('model','(unknown)')}",
             curses.A_BOLD),
            (f"Part No  : {ident.get('sku','(unknown)')}",
             curses.A_BOLD),
            (f"Serial   : {ident.get('serial_no','(unknown)')}",
             curses.color_pair(DIM_PAIR)),
            ("", 0),
            (f"Device   : {drive.get('device','(none)')}",
             curses.A_BOLD),
            (f"Capacity : {drive.get('device_size_gb',0)} GB",
             curses.color_pair(DIM_PAIR)),
            ("", 0),
            (f"OS       : {os_text}",
             curses.A_BOLD | curses.color_pair(CYAN_PAIR)),
            (f"Image    : {image_plan.get('image_name','(pending)')}",
             curses.color_pair(DIM_PAIR)),
            ("", 0),
            (f"NFS      : {nfs_settings.get('nfs_host','?')}:"
             f"{nfs_settings.get('nfs_share','?')}",
             curses.color_pair(CYAN_PAIR)),
            ("", 0),
            ("This may take 30–90 minutes depending on disk size.",
             curses.color_pair(YELLOW_PAIR)),
            ("", 0),
            ("[ Y ]  Start capture",
             curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
            ("[ N ]  Cancel",
             curses.A_BOLD | curses.color_pair(DIM_PAIR)),
        ]
        center_block(stdscr, lines, top_offset=3)
        draw_footer(stdscr, "Press Y to capture, N to cancel")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27):
            return False


def screen_capture_existing_prompt(stdscr, image_plan: dict) -> bool:
    """Ask whether to rotate an existing exact image before capture."""
    image_name = image_plan.get("image_name", "(unknown)")
    while True:
        stdscr.erase()
        draw_header(stdscr, "Phase 3 - Existing Image Backup")
        lines = [
            ("The exact image backup already exists in the database. Press ENTER to Update the new file or press N to cancel the capturing process.",
             curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
            ("", 0),
            (f"Image : {image_name}", curses.color_pair(CYAN_PAIR)),
            (f"Old   : {image_plan.get('image_old_name','')}",
             curses.color_pair(DIM_PAIR)),
        ]
        center_block(stdscr, lines, top_offset=3)
        draw_footer(stdscr, "ENTER update existing image   N cancel")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return True
        if ch in (ord("n"), ord("N"), 27):
            return False


def _fmt_duration(seconds) -> str:
    try:
        sec = int(seconds)
    except (TypeError, ValueError):
        return "--"
    if sec < 0:
        return "--"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d} hr : {m:02d} min : {s:02d} sec"


def _fmt_percent(value) -> str:
    try:
        pct = max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return " --%"
    if pct >= 99.95:
        return "100%"
    if pct <= 0:
        return "  0%"
    if pct < 1.0:
        return f"{pct:5.2f}%"
    if abs(pct - round(pct)) < 0.05:
        return f"{int(round(pct)):3d}%"
    return f"{pct:5.1f}%"


def _progress_bar(value, width: int = 34) -> str:
    try:
        pct = max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        pct = 0.0
    width = max(8, width)
    filled = int(width * pct / 100.0)
    if pct >= 99.95:
        filled = width
    elif pct > 0 and filled == 0:
        filled = 1
    filled = max(0, min(width, filled))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


_MAIN_CONSOLE_FONTS = (
    "/usr/share/consolefonts/Lat15-TerminusBold32x16.psf.gz",
    "/usr/share/consolefonts/Lat2-TerminusBold32x16.psf.gz",
    "/usr/share/consolefonts/Uni3-TerminusBold32x16.psf.gz",
    "/usr/share/consolefonts/Lat15-Terminus32x16.psf.gz",
    "/usr/share/consolefonts/Lat2-Terminus32x16.psf.gz",
    "Lat15-TerminusBold32x16",
    "Lat2-TerminusBold32x16",
    "Uni3-TerminusBold32x16",
    "Lat15-Terminus32x16",
    "Lat2-Terminus32x16",
)

_PHASE3_PROGRESS_CONSOLE_FONTS = (
    "/usr/share/consolefonts/Lat15-Terminus16.psf.gz",
    "/usr/share/consolefonts/Lat2-Terminus16.psf.gz",
    "/usr/share/consolefonts/Uni3-Terminus16.psf.gz",
    "/usr/share/consolefonts/Lat15-Terminus20x10.psf.gz",
    "/usr/share/consolefonts/Lat2-Terminus20x10.psf.gz",
    "Lat15-Terminus16",
    "Lat2-Terminus16",
    "Uni3-Terminus16",
    "Lat15-Terminus20x10",
    "Lat2-Terminus20x10",
)


def _try_set_console_font(candidates: tuple[str, ...]) -> bool:
    """Best-effort console font switch for dense progress screens."""
    if os.environ.get("VSTL_DISABLE_SETFONT") == "1":
        return False
    for font in candidates:
        try:
            rc = subprocess.run(
                ["setfont", font],
                capture_output=True,
                timeout=2,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if rc.returncode == 0:
            return True
    return False


def _refresh_after_console_font(stdscr) -> None:
    try:
        curses.update_lines_cols()
    except curses.error:
        pass
    try:
        stdscr.clear()
        stdscr.refresh()
    except curses.error:
        pass


def _enter_phase3_progress_font(stdscr) -> None:
    if _try_set_console_font(_PHASE3_PROGRESS_CONSOLE_FONTS):
        _refresh_after_console_font(stdscr)


def _restore_main_console_font(stdscr) -> None:
    if _try_set_console_font(_MAIN_CONSOLE_FONTS):
        _refresh_after_console_font(stdscr)


def _clip_cell(value: object, max_width: Optional[int]) -> str:
    text = str(value)
    if max_width is None or max_width <= 0 or len(text) <= max_width:
        return text
    if max_width <= 3:
        return text[:max_width]
    return text[: max_width - 3] + "..."


def _phase3_progress_layout(width: int) -> dict:
    content_width = max(76, min(width - 2, 140))
    left = max(1, (width - content_width) // 2)
    two_col = width >= 118
    right = left + 58
    if two_col and right + 26 >= width:
        right = max(left + 42, width - 42)
    left_value_width = (
        max(10, right - (left + 18) - 2)
        if two_col else max(12, width - (left + 18) - 2)
    )
    right_value_width = max(10, width - (right + 18) - 2)
    bar_width = max(18, min(54, width - left - 34))
    return {
        "left": left,
        "right": right,
        "two_col": two_col,
        "left_value_width": left_value_width,
        "right_value_width": right_value_width,
        "bar_width": bar_width,
    }


def _draw_kv(stdscr, y: int, x: int, label: str, value: str,
             attr: int = 0, max_value_width: Optional[int] = None) -> None:
    _safe_addstr(stdscr, y, x, f"{label:<16}: ", curses.color_pair(DIM_PAIR))
    _safe_addstr(stdscr, y, x + 18, _clip_cell(value, max_value_width), attr)


def _draw_capture_progress(stdscr, ident: dict, drive: dict,
                            state: dict) -> None:
    stdscr.erase()
    draw_header(stdscr, "Phase 3 - Capturing image")
    h, w = stdscr.getmaxyx()
    layout = _phase3_progress_layout(w)
    left = layout["left"]
    right = layout["right"]
    two_col = layout["two_col"]
    left_value_width = layout["left_value_width"]
    right_value_width = layout["right_value_width"]
    bar_width = layout["bar_width"]
    y = 3

    spinner = "|/-\\"[int(time.time() * 4) % 4]
    image_name = state.get("image_subdir") or state.get("image_name") or "(creating)"
    current = state.get("current_partition") or "waiting"
    part_total = state.get("parts_total")
    part_idx = state.get("partition_index")
    parts_done = state.get("parts_done")
    parts_left = state.get("parts_left")
    part_label = current
    if part_idx and part_total:
        part_label = f"{part_idx}/{part_total} - {current}"
    elif part_total:
        part_label = f"{current} ({part_total} partitions)"

    _safe_addstr(stdscr, y, left, f"{spinner} Capture is running",
                 curses.A_BOLD | curses.color_pair(GREEN_PAIR))
    y += 2
    _draw_kv(stdscr, y, left, "Model", ident.get("model", ""),
             max_value_width=left_value_width)
    if two_col:
        _draw_kv(stdscr, y, right, "Serial", ident.get("serial_no", ""),
                 max_value_width=right_value_width)
    else:
        y += 1
        _draw_kv(stdscr, y, left, "Serial", ident.get("serial_no", ""),
                 max_value_width=left_value_width)
    y += 1
    _draw_kv(stdscr, y, left, "Device", drive.get("device", state.get("device", "")),
             max_value_width=left_value_width)
    if two_col:
        _draw_kv(stdscr, y, right, "Image", image_name,
                 max_value_width=right_value_width)
    else:
        y += 1
        _draw_kv(stdscr, y, left, "Image", image_name,
                 max_value_width=left_value_width)
    y += 1
    _draw_kv(stdscr, y, left, "OS",
             _os_display(state.get("os_name", ""), state.get("os_version", ""),
                         state.get("os_build", "")),
             curses.color_pair(CYAN_PAIR), max_value_width=left_value_width)
    y += 2

    _draw_kv(stdscr, y, left, "Current part", part_label,
             curses.A_BOLD | curses.color_pair(CYAN_PAIR),
             max_value_width=left_value_width)
    if two_col:
        _draw_kv(stdscr, y, right, "Filesystem", state.get("filesystem", "--"),
                 max_value_width=right_value_width)
    y += 1
    _draw_kv(stdscr, y, left, "Part size", state.get("partition_size", "--"),
             max_value_width=left_value_width)
    if two_col:
        _draw_kv(stdscr, y, right, "Used / Free",
                 f"{state.get('partition_used','--')} / {state.get('partition_free','--')}",
                 max_value_width=right_value_width)
    else:
        y += 1
        _draw_kv(stdscr, y, left, "Used / Free",
                 f"{state.get('partition_used','--')} / {state.get('partition_free','--')}",
                 max_value_width=left_value_width)
    y += 2

    part_pct = state.get("partition_percent")
    overall_pct = state.get("overall_percent")
    _safe_addstr(stdscr, y, left, "Partition progress", curses.color_pair(DIM_PAIR))
    _safe_addstr(stdscr, y, left + 22,
                 f"{_progress_bar(part_pct, bar_width)} {_fmt_percent(part_pct)}",
                 curses.A_BOLD | curses.color_pair(GREEN_PAIR))
    y += 1
    _safe_addstr(stdscr, y, left, "Overall progress", curses.color_pair(DIM_PAIR))
    _safe_addstr(stdscr, y, left + 22,
                 f"{_progress_bar(overall_pct, bar_width)} {_fmt_percent(overall_pct)}",
                 curses.A_BOLD | curses.color_pair(CYAN_PAIR))
    y += 2

    _draw_kv(stdscr, y, left, "Elapsed", _fmt_duration(state.get("elapsed_sec", 0)),
             max_value_width=left_value_width)
    if two_col:
        _draw_kv(stdscr, y, right, "Part ETA",
                 state.get("partition_eta_text") or _fmt_duration(state.get("partition_eta_sec")),
                 max_value_width=right_value_width)
    y += 1
    _draw_kv(stdscr, y, left, "Overall ETA", _fmt_duration(state.get("overall_eta_sec")),
             max_value_width=left_value_width)
    if two_col:
        _draw_kv(stdscr, y, right, "Upload",
                 state.get("network_rate") or state.get("transfer_rate") or state.get("write_speed") or "--",
                 max_value_width=right_value_width)
    y += 1
    _draw_kv(stdscr, y, left, "Written", state.get("bytes_written_text", "--"),
             max_value_width=left_value_width)
    if part_total is not None:
        if two_col:
            _draw_kv(stdscr, y, right, "Partitions",
                     f"done {parts_done or 0}, left {parts_left if parts_left is not None else '--'}",
                     max_value_width=right_value_width)
        else:
            y += 1
            _draw_kv(stdscr, y, left, "Partitions",
                     f"done {parts_done or 0}, left {parts_left if parts_left is not None else '--'}",
                     max_value_width=left_value_width)
    y += 2

    last = str(state.get("last_line") or state.get("phase") or "")
    if "Current Block:" in last and "Complete:" in last:
        last = last.split("Complete:", 1)[0].rstrip(" ,")
    if last:
        _safe_addstr(stdscr, y, left, "Last activity:", curses.color_pair(DIM_PAIR))
        _safe_addstr(stdscr, y + 1, left, last[: max(10, w - left - 3)],
                     curses.color_pair(YELLOW_PAIR))
        y += 3

    _safe_addstr(stdscr, min(y, h - 3), left,
                 "Do NOT power off the bench during capture.",
                 curses.A_BOLD | curses.color_pair(RED_PAIR))
    draw_footer(stdscr, "Capture in progress - partition, overall %, ETA, and speed update live")
    stdscr.refresh()


def screen_run_capture(stdscr, ident: dict, drive: dict,
                        nfs_settings: dict, cpu_info: Optional[dict] = None,
                        validation: Optional[dict] = None,
                        image_plan: Optional[dict] = None,
                        replace_existing: bool = False,
                        secure_erase_gate: Optional[dict] = None) -> dict:
    image_plan = image_plan or {}
    state = {
        "phase": "mounting NFS...",
        "elapsed_sec": 0,
        "image_name": image_plan.get("image_name", ""),
        "image_subdir": image_plan.get("image_subdir", ""),
        "os_name": image_plan.get("os_name", ""),
        "os_version": image_plan.get("os_version", ""),
        "os_build": image_plan.get("os_build", ""),
        "device": drive.get("device", ""),
    }

    def _progress(ev: dict) -> None:
        state.update(ev)
        _draw_capture_progress(stdscr, ident, drive, state)

    _enter_phase3_progress_font(stdscr)
    try:
        _draw_capture_progress(stdscr, ident, drive, state)
        result = ic.run_capture(
            device=drive.get("device", ""),
            brand=ident.get("brand", ""),
            model=ident.get("model", ""),
            part_number=ident.get("sku", ""),
            source_serial=ident.get("serial_no", ""),
            nfs_host=nfs_settings.get("nfs_host", ""),
            nfs_share=nfs_settings.get("nfs_share", ""),
            mount_options=nfs_settings.get("mount_options", "rw,nolock,vers=3"),
            cpu_model=(cpu_info or {}).get("cpu", ""),
            validation=validation or {},
            image_plan=image_plan,
            replace_existing=replace_existing,
            secure_erase_authorized=bool((secure_erase_gate or {}).get("ok")),
            progress_callback=_progress,
        )
        return result
    finally:
        _restore_main_console_font(stdscr)


def screen_capture_result(stdscr, result: dict, register_resp: dict,
                           registered: bool) -> None:
    stdscr.erase()
    if result.get("ok") and registered:
        draw_header(stdscr, "Phase 3 — Capture OK ✓")
        top = ("✓  Image captured and registered as golden copy",
                curses.A_BOLD | curses.color_pair(GREEN_PAIR))
    elif result.get("ok") and not registered:
        draw_header(stdscr, "Phase 3 — Capture OK / Register FAILED")
        top = ("⚠  Image captured but backend registration failed",
                curses.A_BOLD | curses.color_pair(YELLOW_PAIR))
    else:
        draw_header(stdscr, "Phase 3 — Capture FAILED")
        top = (f"✗  {result.get('error_message','capture failed')[:80]}",
                curses.A_BOLD | curses.color_pair(RED_PAIR))

    lines = [top, ("", 0),
             (f"Image    : {result.get('image_name','(none)')}",
             curses.A_BOLD),
             (f"Size     : {result.get('image_size_gb',0)} GB",
              curses.color_pair(DIM_PAIR)),
             (f"OS       : {_os_display(result.get('os_name',''), result.get('os_version',''), result.get('os_build',''))}",
              curses.color_pair(CYAN_PAIR)),
             (f"Duration : {_fmt_duration(result.get('duration_sec',0))}",
              curses.color_pair(DIM_PAIR))]
    if result.get("replaced_existing"):
        lines.append((f"Replaced : previous exact image moved to _old",
                      curses.color_pair(YELLOW_PAIR)))
    if register_resp.get("golden_copy_id"):
        lines.append(("", 0))
        lines.append((f"Copy ID  : {register_resp['golden_copy_id']}",
                      curses.A_BOLD))
        lines.append((f"Action   : {register_resp.get('action','')}",
                      curses.color_pair(CYAN_PAIR)))
    center_block(stdscr, lines, top_offset=3)
    draw_footer(stdscr, "ENTER to continue   Q to quit")
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return
        if ch in (ord("q"), ord("Q")):
            sys.exit(2)


# --- Phase 3 — Restore Approved System Image -------------------------------
def screen_restore_picker(stdscr, copies: list[dict]) -> Optional[dict]:
    """Pick the OS/version image to restore from exact or fallback matches."""
    if not copies:
        stdscr.erase()
        draw_header(stdscr, "Phase 3 - Restore")
        center_block(stdscr, [
            ("Backup for this system is not found.",
             curses.A_BOLD | curses.color_pair(RED_PAIR)),
            ("", 0),
            ("Restore cannot start until a matching image backup exists.",
             curses.color_pair(DIM_PAIR)),
        ])
        draw_footer(stdscr, "ENTER continue")
        stdscr.refresh()
        while True:
            ch = stdscr.getch()
            if ch in (10, 13, curses.KEY_ENTER, 27):
                return None

    selected = 0
    while True:
        stdscr.erase()
        draw_header(stdscr, "Phase 3 - Select OS / Version to Restore")
        h, w = stdscr.getmaxyx()
        _safe_addstr(stdscr, 2, 2,
                     "Use arrows to select OS/version, ENTER to restore, ESC to cancel",
                     curses.color_pair(DIM_PAIR))
        for i, c in enumerate(copies[:h - 8]):
            y = 4 + i
            os_text = _os_display(c.get("os_name", ""), c.get("os_version", ""),
                                  c.get("os_build", ""))
            match = "SKU" if c.get("match_type") == "sku" else "MODEL+CPU"
            size = c.get("image_size_gb", "?")
            label = (
                f"{os_text:<24} {match:<10} "
                f"{str(size):>7} GB  {c.get('image_name','')[:48]} "
            )
            _draw_selectable_row(stdscr, y, 2, label[: w - 8], i == selected)
        draw_footer(stdscr, "ENTER select   ESC cancel")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(copies)
        elif ch in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(copies)
        elif ch in (10, 13, curses.KEY_ENTER):
            return copies[selected]
        elif ch == 27:
            return None

def screen_restore_model_cpu_fallback(stdscr, golden_copy: dict) -> bool:
    while True:
        stdscr.erase()
        draw_header(stdscr, "Phase 3 - Restore Fallback Match")
        lines = [
            ("SKU/Unit Part Number match not found, but an exact match for Model Name and CPU exists.",
             curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
            ("", 0),
            (f"Model : {golden_copy.get('model','')}", curses.A_NORMAL),
            (f"CPU   : {golden_copy.get('cpu','')}", curses.A_NORMAL),
            (f"Image : {golden_copy.get('image_name','')}", curses.A_NORMAL),
            ("", 0),
            ("Press ENTER to continue or press C to cancel.",
             curses.A_BOLD | curses.color_pair(CYAN_PAIR)),
        ]
        center_block(stdscr, lines, top_offset=3)
        draw_footer(stdscr, "ENTER continue   C cancel")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return True
        if ch in (ord("c"), ord("C"), 27):
            return False


def screen_restore_intro(stdscr, ident: dict, drive: dict,
                          golden_copy: dict, nfs_settings: dict) -> bool:
    while True:
        stdscr.erase()
        draw_header(stdscr, "Phase 3 — Restore Approved System Image")
        lines = [
            ("⚠  Restore will OVERWRITE the current disk.",
             curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
            ("", 0),
            (f"Serial   : {ident.get('serial_no','(unknown)')}",
             curses.A_BOLD),
            (f"Model    : {ident.get('model','(unknown)')}",
             curses.A_BOLD),
            (f"Device   : {drive.get('device','(none)')}",
             curses.color_pair(DIM_PAIR)),
            ("", 0),
            (f"Image    : {golden_copy.get('image_name','(unknown)')}",
             curses.A_BOLD | curses.color_pair(CYAN_PAIR)),
            (f"For model: {golden_copy.get('model','-')}",
             curses.color_pair(DIM_PAIR)),
            (f"Size     : {golden_copy.get('image_size_gb','?')} GB",
             curses.color_pair(DIM_PAIR)),
            ("", 0),
            (f"NFS      : {nfs_settings.get('nfs_host','?')}:"
             f"{nfs_settings.get('nfs_share','?')}",
             curses.color_pair(DIM_PAIR)),
            ("", 0),
            ("[ Y ]  Start restore",
             curses.A_BOLD | curses.color_pair(GREEN_PAIR)),
            ("[ N ]  Cancel",
             curses.A_BOLD | curses.color_pair(DIM_PAIR)),
        ]
        center_block(stdscr, lines, top_offset=3)
        draw_footer(stdscr, "Press Y to restore, N to cancel")
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("y"), ord("Y")):
            return True
        if ch in (ord("n"), ord("N"), 27):
            return False


def _draw_restore_progress(stdscr, ident: dict, drive: dict,
                            golden_copy: dict, state: dict) -> None:
    stdscr.erase()
    draw_header(stdscr, "Phase 3 - Restoring image")
    h, w = stdscr.getmaxyx()
    layout = _phase3_progress_layout(w)
    left = layout["left"]
    right = layout["right"]
    two_col = layout["two_col"]
    left_value_width = layout["left_value_width"]
    right_value_width = layout["right_value_width"]
    bar_width = layout["bar_width"]
    y = 3

    spinner = "|/-\\"[int(time.time() * 4) % 4]
    image_name = (
        state.get("image_subdir") or state.get("image_name")
        or golden_copy.get("image_subdir") or golden_copy.get("image_name")
        or "(restoring)"
    )
    current = state.get("current_partition") or "waiting"
    part_total = state.get("parts_total")
    part_idx = state.get("partition_index")
    parts_done = state.get("parts_done")
    parts_left = state.get("parts_left")
    part_label = current
    if part_idx and part_total:
        part_label = f"{part_idx}/{part_total} - {current}"
    elif part_total:
        part_label = f"{current} ({part_total} partitions)"

    _safe_addstr(stdscr, y, left, f"{spinner} Restore is running",
                 curses.A_BOLD | curses.color_pair(GREEN_PAIR))
    y += 2
    _draw_kv(stdscr, y, left, "Model", ident.get("model", ""),
             max_value_width=left_value_width)
    if two_col:
        _draw_kv(stdscr, y, right, "Serial", ident.get("serial_no", ""),
                 max_value_width=right_value_width)
    else:
        y += 1
        _draw_kv(stdscr, y, left, "Serial", ident.get("serial_no", ""),
                 max_value_width=left_value_width)
    y += 1
    _draw_kv(stdscr, y, left, "Device", drive.get("device", state.get("device", "")),
             max_value_width=left_value_width)
    if two_col:
        _draw_kv(stdscr, y, right, "Image", image_name,
                 max_value_width=right_value_width)
    else:
        y += 1
        _draw_kv(stdscr, y, left, "Image", image_name,
                 max_value_width=left_value_width)
    y += 1
    _draw_kv(stdscr, y, left, "OS",
             _os_display(state.get("os_name", ""), state.get("os_version", ""),
                         state.get("os_build", "")),
             curses.color_pair(CYAN_PAIR), max_value_width=left_value_width)
    match = state.get("match_type") or golden_copy.get("match_type") or ""
    if two_col:
        _draw_kv(stdscr, y, right, "Match", match.upper() if match else "--",
                 max_value_width=right_value_width)
    y += 2

    _draw_kv(stdscr, y, left, "Current part", part_label,
             curses.A_BOLD | curses.color_pair(CYAN_PAIR),
             max_value_width=left_value_width)
    if two_col:
        _draw_kv(stdscr, y, right, "Filesystem", state.get("filesystem", "--"),
                 max_value_width=right_value_width)
    y += 1
    _draw_kv(stdscr, y, left, "Part size", state.get("partition_size", "--"),
             max_value_width=left_value_width)
    if two_col:
        _draw_kv(stdscr, y, right, "Used / Free",
                 f"{state.get('partition_used','--')} / {state.get('partition_free','--')}",
                 max_value_width=right_value_width)
    else:
        y += 1
        _draw_kv(stdscr, y, left, "Used / Free",
                 f"{state.get('partition_used','--')} / {state.get('partition_free','--')}",
                 max_value_width=left_value_width)
    y += 2

    part_pct = state.get("partition_percent")
    overall_pct = state.get("overall_percent")
    _safe_addstr(stdscr, y, left, "Partition progress", curses.color_pair(DIM_PAIR))
    _safe_addstr(stdscr, y, left + 22,
                 f"{_progress_bar(part_pct, bar_width)} {_fmt_percent(part_pct)}",
                 curses.A_BOLD | curses.color_pair(GREEN_PAIR))
    y += 1
    _safe_addstr(stdscr, y, left, "Overall progress", curses.color_pair(DIM_PAIR))
    _safe_addstr(stdscr, y, left + 22,
                 f"{_progress_bar(overall_pct, bar_width)} {_fmt_percent(overall_pct)}",
                 curses.A_BOLD | curses.color_pair(CYAN_PAIR))
    y += 2

    _draw_kv(stdscr, y, left, "Elapsed", _fmt_duration(state.get("elapsed_sec", 0)),
             max_value_width=left_value_width)
    if two_col:
        _draw_kv(stdscr, y, right, "Part ETA",
                 state.get("partition_eta_text") or _fmt_duration(state.get("partition_eta_sec")),
                 max_value_width=right_value_width)
    y += 1
    _draw_kv(stdscr, y, left, "Overall ETA", _fmt_duration(state.get("overall_eta_sec")),
             max_value_width=left_value_width)
    if two_col:
        _draw_kv(stdscr, y, right, "Download",
                 state.get("network_rate") or state.get("transfer_rate") or state.get("write_speed") or "--",
                 max_value_width=right_value_width)
    y += 1
    _draw_kv(stdscr, y, left, "Written", state.get("bytes_written_text", "--"),
             max_value_width=left_value_width)
    if part_total is not None:
        if two_col:
            _draw_kv(stdscr, y, right, "Partitions",
                     f"done {parts_done or 0}, left {parts_left if parts_left is not None else '--'}",
                     max_value_width=right_value_width)
        else:
            y += 1
            _draw_kv(stdscr, y, left, "Partitions",
                     f"done {parts_done or 0}, left {parts_left if parts_left is not None else '--'}",
                     max_value_width=left_value_width)
    y += 2

    last = str(state.get("last_line") or state.get("phase") or "")
    if "Current Block:" in last and "Complete:" in last:
        last = last.split("Complete:", 1)[0].rstrip(" ,")
    if last:
        _safe_addstr(stdscr, y, left, "Last activity:", curses.color_pair(DIM_PAIR))
        _safe_addstr(stdscr, y + 1, left, last[: max(10, w - left - 3)],
                     curses.color_pair(YELLOW_PAIR))
        y += 3

    _safe_addstr(stdscr, min(y, h - 3), left,
                 "Do NOT power off the bench during restore.",
                 curses.A_BOLD | curses.color_pair(RED_PAIR))
    draw_footer(stdscr, "Restore in progress - partition, overall %, ETA, and speed update live")
    stdscr.refresh()


def screen_run_restore(stdscr, ident: dict, drive: dict,
                        golden_copy: dict, nfs_settings: dict) -> dict:
    image_subdir = (
        golden_copy.get("image_subdir")
        or str(golden_copy.get("image_name", "")).removesuffix(".img")
    )
    state = {
        "phase": "mounting NFS...",
        "elapsed_sec": 0,
        "image_name": golden_copy.get("image_name", ""),
        "image_subdir": image_subdir,
        "os_name": golden_copy.get("os_name", ""),
        "os_version": golden_copy.get("os_version", ""),
        "os_build": golden_copy.get("os_build", ""),
        "match_type": golden_copy.get("match_type", ""),
        "device": drive.get("device", ""),
    }

    def _progress(ev: dict) -> None:
        state.update(ev)
        _draw_restore_progress(stdscr, ident, drive, golden_copy, state)

    _enter_phase3_progress_font(stdscr)
    try:
        _draw_restore_progress(stdscr, ident, drive, golden_copy, state)
        result = ir.run_restore(
            image_subdir=image_subdir,
            device=drive.get("device", ""),
            nfs_host=nfs_settings.get("nfs_host", ""),
            nfs_share=nfs_settings.get("nfs_share", ""),
            mount_options=nfs_settings.get("mount_options", "rw,nolock,vers=3"),
            golden_copy_id=golden_copy.get("id", ""),
            golden_copy=golden_copy,
            progress_callback=_progress,
        )
        return result
    finally:
        _restore_main_console_font(stdscr)


def screen_restore_result(stdscr, result: dict, log_ok: bool,
                          reporting_suppressed: bool = False) -> None:
    stdscr.erase()
    if result.get("ok"):
        draw_header(stdscr, "Phase 3 - Restore OK")
        top = ("Image restored successfully",
                curses.A_BOLD | curses.color_pair(GREEN_PAIR))
    else:
        draw_header(stdscr, "Phase 3 - Restore FAILED")
        top = (f"{result.get('error_message','restore failed')[:80]}",
                curses.A_BOLD | curses.color_pair(RED_PAIR))
    lines = [top, ("", 0),
             (f"Image    : {result.get('image_name','(none)')[:48]}",
              curses.A_BOLD),
             (f"OS       : {_os_display(result.get('os_name',''), result.get('os_version',''), result.get('os_build',''))}",
              curses.color_pair(CYAN_PAIR)),
             (f"Duration : {_fmt_duration(result.get('duration_sec',0))}",
              curses.color_pair(DIM_PAIR)),
             (f"Verified : {'YES' if result.get('verified') else 'NO'}",
              curses.color_pair(GREEN_PAIR if result.get('verified') else YELLOW_PAIR))]
    if reporting_suppressed:
        lines.append(("", 0))
        lines.append(("Testing mode: restore log was not posted.",
                       curses.color_pair(DIM_PAIR)))
    elif not log_ok:
        lines.append(("", 0))
        lines.append(("Restore log POST to backend failed",
                       curses.color_pair(YELLOW_PAIR)))
    center_block(stdscr, lines, top_offset=3)
    draw_footer(stdscr, "ENTER to continue   Q to quit")
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return
        if ch in (ord("q"), ord("Q")):
            sys.exit(2)

def _record_secure_erase_authorization(
    ident: dict,
    drive: dict,
    result: dict,
    certificate: dict,
    certificate_ok: bool,
    cfg: dict,
) -> dict:
    nfs_settings = _get_bench_nfs_settings(cfg)
    mounted, mount_evidence = ic.mount_nfs(
        nfs_settings.get("nfs_host", ""),
        nfs_settings.get("nfs_share", ""),
        nfs_settings.get("mount_options", "rw,nolock,vers=3"),
    )
    if not mounted:
        return {
            "ok": False,
            "issues": [f"Could not reach server erase-record storage: {mount_evidence}"],
        }
    try:
        record = ic.write_secure_erase_record(
            ident.get("serial_no", ""),
            drive,
            result,
            certificate=certificate,
            certificate_ok=certificate_ok,
        )
        record["mount_evidence"] = mount_evidence
        return record
    finally:
        ic.umount_nfs()


def _check_secure_erase_authorization(
    ident: dict,
    drive: dict,
    nfs_settings: dict,
) -> dict:
    mounted, mount_evidence = ic.mount_nfs(
        nfs_settings.get("nfs_host", ""),
        nfs_settings.get("nfs_share", ""),
        nfs_settings.get("mount_options", "rw,nolock,vers=3"),
    )
    if not mounted:
        return {
            "ok": False,
            "issues": [f"Could not verify secure-erase history on server: {mount_evidence}"],
        }
    try:
        gate = ic.check_secure_erase_record(ident.get("serial_no", ""), drive)
        gate["mount_evidence"] = mount_evidence
        return gate
    finally:
        ic.umount_nfs()


def phase3_secure_erase(stdscr, ident: dict, cfg: dict,
                         tech: str, operator: dict | None = None,
                         suppress_reporting: bool = False) -> Optional[dict]:
    """Run the full Phase-3 Secure Erase sub-flow:
       detect drive -> confirm -> wipe -> POST certificate -> show result.
    Returns the wipe result dict (or None if operator cancelled)."""
    drive = se.detect_primary_drive()
    if not drive.get("device"):
        _show_message(stdscr,
                      f"Drive detection failed: {drive.get('error','no disk found')}",
                      color=RED_PAIR, secs=4)
        return None
    if not screen_erase_intro(stdscr, drive):
        return None  # cancelled
    result = screen_run_erase(stdscr, drive)
    allow_clear_exception = _clear_exception_allowed(result, ident)
    if result.get("ok") and not _is_certifiable_wipe_result(result, ident):
        refused_method = str(result.get("method") or "UNKNOWN").strip() or "UNKNOWN"
        refusal = f"Unsupported wipe method {refused_method}: {_UNSUPPORTED_WIPE_MESSAGE}"
        result = dict(result)
        result["ok"] = False
        result["verified"] = False
        result["capture_gate_recorded"] = False
        result["certificate_status"] = "refused"
        result["error_message"] = refusal
        evidence = str(result.get("evidence") or "")
        result["evidence"] = (evidence + "\n" if evidence else "") + refusal

    # Issue a local certificate first. The backend POST enriches it when
    # available, but a verified erase must not lose its certificate just
    # because the cloud registration hop fails.
    cert_resp: dict = {}
    cert_ok = False
    if suppress_reporting and result.get("ok"):
        cert_resp = {
            "wipe_standard": _wipe_standard(
                str(result.get("method") or ""),
                allow_clear_exception,
            ),
            "certificate_status": "testing_mode_suppressed",
            "remote_post_ok": False,
            "remote_error": "testing mode suppressed",
        }
        result["testing_mode_reporting_suppressed"] = True
    elif result.get("ok") and result.get("method"):
        local_cert = _local_secure_erase_certificate(
            ident, drive, result, cfg, tech,
        )
        cert_body = {
            "serial_no":          ident.get("serial_no", ""),
            "mac_id":             ident.get("mac_id", ""),
            "bench_id":           _bench_id(cfg),
            "device":             result.get("device", ""),
            "device_type":        result.get("device_type", ""),
            "device_model":       result.get("device_model", ""),
            "device_serial":      drive.get("device_serial", ""),
            "device_wwn":         drive.get("device_wwn", ""),
            "device_size_bytes":  drive.get("device_size_bytes", 0),
            "device_size_gb":     result.get("device_size_gb", 0),
            "wipe_method":        result.get("method", ""),
            "wipe_passes":        result.get("passes", 1),
            "wipe_standard":      local_cert.get("wipe_standard", ""),
            "clear_only_exception": bool(result.get("clear_only_exception")),
            "clear_only_exception_reason": result.get("clear_only_exception_reason", ""),
            "wipe_started_at":    result.get("started_at", ""),
            "wipe_completed_at":  result.get("completed_at", ""),
            "wipe_verified":      result.get("verified", False),
            "verification_method": result.get("verification_method", ""),
            "certificate_id":     local_cert.get("certificate_id", ""),
            "verification_hash":   local_cert.get("verification_hash", ""),
            "evidence_sha256":     local_cert.get("evidence_sha256", ""),
            "operator_name":      "",
            "technician_level":   tech,
            "brand":              ident.get("brand", ""),
            "model":              ident.get("model", ""),
            "evidence":           result.get("evidence", ""),
        }
        remote_http_ok, remote_resp, remote_err = _api_post(
            "/imaging/secure-erase/certificate", cert_body, cfg, timeout=60,
        )
        remote_success = bool(
            remote_http_ok
            and isinstance(remote_resp, dict)
            and (
                remote_resp.get("success")
                or remote_resp.get("status") in ("success", "ok")
                or remote_resp.get("certificate_id")
            )
        )
        if not remote_success and remote_http_ok:
            remote_err = _compact_api_error(remote_resp, "certificate POST rejected")
        elif not remote_success:
            remote_err = _compact_api_error(remote_resp, remote_err)
        compatibility_post: dict[str, str] = {}
        compat_method = _cloud_certificate_compat_wipe_method(result.get("method", ""))
        if not remote_success and compat_method:
            compat_body = dict(cert_body)
            compat_body["wipe_method"] = compat_method
            compat_body["wipe_standard"] = _wipe_standard(compat_method)
            retry_http_ok, retry_resp, retry_err = _api_post(
                "/imaging/secure-erase/certificate", compat_body, cfg, timeout=60,
            )
            retry_success = bool(
                retry_http_ok
                and isinstance(retry_resp, dict)
                and (
                    retry_resp.get("success")
                    or retry_resp.get("status") in ("success", "ok")
                    or retry_resp.get("certificate_id")
                )
            )
            if retry_success:
                remote_http_ok = retry_http_ok
                remote_resp = retry_resp
                remote_success = True
                compatibility_post = {
                    "remote_post_wipe_method": compat_method,
                    "remote_actual_wipe_method": str(result.get("method") or ""),
                    "remote_compatibility_note": (
                        "cloud certificate endpoint did not yet accept "
                        f"{result.get('method')}; posted compatible Clear method "
                        f"{compat_method}"
                    ),
                }
                remote_err = ""
            else:
                retry_err = (
                    _compact_api_error(retry_resp, "compatibility certificate POST rejected")
                    if retry_http_ok else _compact_api_error(retry_resp, retry_err)
                )
                remote_err = (remote_err + "; compatibility retry: " + retry_err)[:300]
        cert_resp = _merge_secure_erase_certificate(
            local_cert, remote_success, remote_resp, remote_err,
        )
        if compatibility_post:
            cert_resp.update(compatibility_post)
        cert_ok = True

    gate_record = {"ok": False, "issues": ["Secure erase did not complete successfully."]}
    if suppress_reporting:
        gate_record = {"ok": False, "issues": ["Testing mode: capture authorization not recorded."]}
    elif result.get("ok") and result.get("verified"):
        gate_record = _record_secure_erase_authorization(
            ident, drive, result, cert_resp, cert_ok, cfg,
        )
    result["capture_gate_recorded"] = bool(gate_record.get("ok"))
    result["capture_gate_evidence"] = (
        gate_record.get("path")
        or "; ".join(gate_record.get("issues") or [])
    )
    if (
        result.get("ok")
        and result.get("verified")
        and cert_resp
        and not suppress_reporting
    ):
        report_ok, report_msg = _post_secure_erase_local_report(
            ident, drive, result, cert_resp, gate_record, cfg, tech, operator,
        )
        result["local_report_post_ok"] = bool(report_ok)
        result["local_report_post_message"] = report_msg

    screen_erase_result(
        stdscr,
        result,
        cert_resp,
        cert_ok,
        reporting_suppressed=suppress_reporting,
    )
    return {
        "result": result,
        "certificate": cert_resp,
        "cert_ok": cert_ok,
        "capture_gate": gate_record,
    }


def phase3_capture(stdscr, ident: dict, cfg: dict,
                    tech: str, cpu_info: Optional[dict] = None,
                    audit: Optional[dict] = None) -> Optional[dict]:
    """Run the full Phase-3 Capture sub-flow:
       detect drive -> fetch NFS settings -> confirm -> capture -> register."""
    nfs_settings = _get_bench_nfs_settings(cfg)
    drive = se.detect_primary_drive()
    if not drive.get("device"):
        _show_message(stdscr,
                      f"Drive detection failed: {drive.get('error','no disk found')}",
                      color=RED_PAIR, secs=4)
        return None

    readiness = ic.validate_capture_readiness(
        drive.get("device", ""), ident, (cpu_info or {}).get("cpu", ""), audit,
    )
    secure_erase_gate = _check_secure_erase_authorization(
        ident, drive, nfs_settings,
    )
    readiness["secure_erase_gate"] = secure_erase_gate
    if not secure_erase_gate.get("ok"):
        gate_issues = secure_erase_gate.get("issues") or [
            "Certified Secure Erase has not been recorded for this exact laptop and drive."
        ]
        readiness["issues"] = list(readiness.get("issues") or []) + gate_issues
        readiness["ok"] = False
    if not screen_capture_readiness(stdscr, readiness):
        return {"result": {
            "ok": False,
            "device": drive.get("device", ""),
            "error_message": "System not ready for image capture.",
            "readiness": readiness,
        }, "registered": False, "register": {}}

    image_plan = ic.build_capture_image_plan(
        ident.get("brand", ""),
        ident.get("model", ""),
        ident.get("sku", ""),
        (cpu_info or {}).get("cpu", ""),
        (readiness or {}).get("os_info", {}),
    )

    ok_m, mount_ev = ic.mount_nfs(
        nfs_settings.get("nfs_host", ""),
        nfs_settings.get("nfs_share", ""),
        nfs_settings.get("mount_options", "rw,nolock,vers=3"),
    )
    if not ok_m:
        _show_message(stdscr, f"NFS mount failed: {mount_ev}"[:80],
                      color=RED_PAIR, secs=4)
        return None

    replace_existing = False
    try:
        if ic.capture_target_exists(image_plan):
            replace_existing = screen_capture_existing_prompt(stdscr, image_plan)
            if not replace_existing:
                ic.umount_nfs()
                return None
    finally:
        ic.umount_nfs()

    if not screen_capture_intro(stdscr, ident, drive, nfs_settings,
                                image_plan=image_plan):
        return None

    result = screen_run_capture(stdscr, ident, drive, nfs_settings,
                                cpu_info=cpu_info, validation=readiness,
                                image_plan=image_plan,
                                replace_existing=replace_existing,
                                secure_erase_gate=secure_erase_gate)
    ic.umount_nfs()

    register_resp: dict = {}
    registered = False
    if result.get("ok"):
        body = {
            "model":          ident.get("model", ""),
            "brand":          ident.get("brand", ""),
            "part_number":    ident.get("sku", ""),
            "image_name":     result.get("image_name", ""),
            "image_path":     result.get("image_path", ""),
            "image_subdir":   result.get("image_subdir", ""),
            "image_size_gb":  result.get("image_size_gb", 0),
            "image_sha256":   result.get("image_sha256", ""),
            "compression":    "zstd",
            "cpu":            (cpu_info or {}).get("cpu", ""),
            "captured_from_serial": ident.get("serial_no", ""),
            "bench_id":       _bench_id(cfg),
            "os_name":        result.get("os_name", ""),
            "os_version":     result.get("os_version", ""),
            "os_build":       result.get("os_build", ""),
            "os_token":       result.get("os_token", ""),
            "replaced_existing": bool(result.get("replaced_existing")),
            "notes":          f"Captured by bench (technician {tech})",
        }
        registered, register_resp, _err = _api_post(
            "/imaging/golden-copies/capture-complete", body, cfg, timeout=60,
        )

    screen_capture_result(stdscr, result, register_resp, registered)
    return {"result": result, "register": register_resp,
            "registered": registered}


def phase3_restore(stdscr, ident: dict, cfg: dict,
                    tech: str, cpu_info: Optional[dict] = None,
                    suppress_reporting: bool = False) -> Optional[dict]:
    """Run the full Phase-3 Restore sub-flow:
       lookup golden copy -> mount NFS -> restore -> POST result.
    The Server Process lookup order is exact SKU/Unit Part Number first,
    then exact Model Name + exact CPU fallback."""
    nfs_settings = _get_bench_nfs_settings(cfg)
    drive = se.detect_primary_drive()
    if not drive.get("device"):
        _show_message(stdscr,
                      f"Drive detection failed: {drive.get('error','no disk found')}",
                      color=RED_PAIR, secs=4)
        return None

    local_lookup = ir.find_local_golden_copies(
        nfs_settings.get("nfs_host", ""),
        nfs_settings.get("nfs_share", ""),
        nfs_settings.get("mount_options", "rw,nolock,vers=3"),
        ident.get("model", ""),
        ident.get("sku", ""),
        (cpu_info or {}).get("cpu", ""),
    )
    if local_lookup.get("status") != "found":
        ir.umount_nfs()
        _show_message(stdscr,
                      local_lookup.get("error") or "Backup for this system is not found.",
                      color=RED_PAIR, secs=4)
        screen_restore_picker(stdscr, [])
        return None

    copies = local_lookup.get("copies") or []
    golden_copy = screen_restore_picker(stdscr, copies)
    if not golden_copy:
        ir.umount_nfs()
        return None
    if golden_copy.get("match_type") == "model_cpu":
        if not screen_restore_model_cpu_fallback(stdscr, golden_copy):
            ir.umount_nfs()
            return None

    if not screen_restore_intro(stdscr, ident, drive, golden_copy, nfs_settings):
        ir.umount_nfs()
        return None

    result = screen_run_restore(stdscr, ident, drive, golden_copy, nfs_settings)
    ir.umount_nfs()

    # Log to backend regardless of success, except for the hidden testing mode
    # where every app/report submission is intentionally suppressed.
    log_body = {
        "serial_no":          ident.get("serial_no", ""),
        "image_name":         result.get("image_name", ""),
        "device":             result.get("device", ""),
        "bench_id":           _bench_id(cfg),
        "operator_name":      "",
        "technician_level":   tech,
        "restore_started_at": result.get("started_at", ""),
        "restore_completed_at": result.get("completed_at", ""),
        "restore_verified":   result.get("verified", False),
        "result":             result.get("result", "FAIL"),
        "evidence":           result.get("evidence", ""),
        "error_message":      result.get("error_message", ""),
        "golden_copy_id":     golden_copy.get("id", ""),
        "image_subdir":       result.get("image_subdir", ""),
        "os_name":            result.get("os_name", ""),
        "os_version":         result.get("os_version", ""),
        "os_build":           result.get("os_build", ""),
        "os_token":           result.get("os_token", ""),
        "match_type":         result.get("match_type", golden_copy.get("match_type", "")),
    }
    log_resp: dict = {}
    if suppress_reporting:
        log_ok = True
        result["testing_mode_reporting_suppressed"] = True
    else:
        log_ok, log_resp, _err = _api_post(
            "/imaging/restore/complete", log_body, cfg, timeout=60,
        )
    screen_restore_result(
        stdscr,
        result,
        log_ok,
        reporting_suppressed=suppress_reporting,
    )
    return {
        "result": result,
        "log_ok": log_ok,
        "log_resp": log_resp,
        "log_suppressed": bool(suppress_reporting),
    }


def _detect_and_show_hardware_profile(stdscr) -> tuple[dict, dict, dict]:
    ident = hw.detect_identity()
    cpu = hw.detect_cpu()
    gpu = hw.detect_gpu()
    ram = hw.detect_ram()
    storage = hw.detect_storage()
    battery = hw.detect_battery()

    screen_model(stdscr, ident)
    screen_sku(stdscr, ident)
    screen_cpu(stdscr, cpu)
    screen_gpu(stdscr, gpu)
    screen_ram(stdscr, ram)
    screen_storage(stdscr, storage)
    screen_battery(stdscr, battery)
    return ident, cpu, storage


def _run_qc_and_burn_flow(
    stdscr,
    cfg: dict,
    tech: str,
) -> tuple[dict | None, dict | None, int | None, dict | None]:
    qc_started_ts = time.monotonic()
    driver_preflight = qc.driver_preflight_result()
    test_keys = qc.applicable_tests_for(tech)
    _qc_intro(stdscr, tech, len(test_keys))
    results: list[dict] = [driver_preflight]
    completed_qc_results: dict[str, dict] = {}
    qc_index = 0
    while True:
        while qc_index < len(test_keys):
            key = test_keys[qc_index]
            screen_fn = QC_SCREEN_DISPATCH[key]
            completed_qc_results[key] = screen_fn(stdscr)
            nav_action = screen_qc_step_complete(
                stdscr,
                completed_qc_results[key],
                has_previous=qc_index > 0,
            )
            if nav_action == "back":
                qc_index = max(0, qc_index - 1)
                continue
            if nav_action == "retest":
                continue
            qc_index += 1
        results = [driver_preflight]
        results.extend(
            completed_qc_results[key]
            for key in test_keys
            if key in completed_qc_results
        )
        qc_summary = qc.summarize(results)
        qc_decision = screen_qc_summary(stdscr, qc_summary, tech)
        post_qc_details = screen_post_qc_details(
            stdscr,
            allow_back_to_qc=bool(test_keys),
        )
        if post_qc_details.get("_nav") == "back_to_qc":
            qc_index = max(0, len(test_keys) - 1)
            post_qc_details = None
            continue
        qc_summary["post_qc"] = dict(post_qc_details)
        break

    # 5. Phase 2C - Burn / Stress (always 5 min, L1 may skip per 3c)
    if qc_decision == "rework":
        burn_result = bs.skipped_result(
            "Auto-skip: L2 QC failed, unit routing back for rework"
        )
    else:
        duration = _read_burn_duration(cfg)
        run_it = screen_burn_intro(stdscr, tech, duration)
        if not run_it:
            remarks = _qc_remarks_dialog(
                stdscr,
                "Why are you skipping the 5-min burn test?"
            )
            burn_result = bs.skipped_result(
                f"L1 operator skipped - {remarks or 'no remarks'}"
            )
        else:
            burn_result = screen_burn_progress(stdscr, duration, cfg)
            screen_burn_result(stdscr, burn_result, tech)
    qc_elapsed_sec = int(time.monotonic() - qc_started_ts)
    return qc_summary, burn_result, qc_elapsed_sec, post_qc_details


def screen_completion(stdscr, ident: dict, ingest_ok: bool, ingest_msg: str,
                      technician: str, choice_idx: int,
                      lock_audit: Optional[dict] = None,
                      qc_summary: Optional[dict] = None,
                      burn_result: Optional[dict] = None,
                      local_report_ok: bool = True,
                      local_report_msg: str = "") -> None:
    """Final summary. Operator presses ENTER to restart the laptop."""
    stdscr.erase()
    draw_header(stdscr, "Phase 2 Complete")
    h, w = stdscr.getmaxyx()

    serial = ident["serial_no"]
    cloud_saved = bool(ingest_ok)
    reconcile_warning = "flagged for reconcile" in str(ingest_msg or "").lower()
    color = YELLOW_PAIR if reconcile_warning else (GREEN_PAIR if cloud_saved else RED_PAIR)
    if cloud_saved and reconcile_warning:
        status_text = "WARNING: Audit saved - unit flagged for reconcile"
    elif cloud_saved and local_report_ok:
        status_text = "✅  Report saved and audit submitted"
    elif cloud_saved:
        status_text = "✅  Cloud audit submitted"
    else:
        status_text = "⚠  Audit submission failed"

    lines = [
        (status_text, curses.A_BOLD | curses.color_pair(color)),
        ("", 0),
        (f"Serial No.: {serial}", curses.A_BOLD),
        (f"Brand / Model: {ident['brand']}  {ident['model']}", curses.A_NORMAL),
        (f"Technician: {technician}    Selected option: {choice_idx + 1}", curses.color_pair(DIM_PAIR)),
        ("", 0),
    ]

    # Phase 2A — surface the lock-audit outcome on the completion screen
    if lock_audit:
        if lock_audit.get("halted") and lock_audit.get("override"):
            ov = lock_audit["override"] or {}
            lines.append((
                f"Lock audit: LOCKED — override by {ov.get('user_name', '?')}",
                curses.A_BOLD | curses.color_pair(YELLOW_PAIR),
            ))
        elif lock_audit.get("halted") and lock_audit.get("decision") == "continue_l1":
            lines.append((
                f"Lock audit: MDM warning - L1 continued ({lock_audit.get('summary', '')[: w - 36]})",
                curses.A_BOLD | curses.color_pair(YELLOW_PAIR),
            ))
        elif lock_audit.get("halted"):
            lines.append((
                f"Lock audit: HALTED — {lock_audit.get('summary', '')[: w - 24]}",
                curses.A_BOLD | curses.color_pair(RED_PAIR),
            ))
            lines.append(("Set this unit aside.  Do NOT process further.",
                          curses.color_pair(RED_PAIR)))
        else:
            lines.append(("Lock audit: All clear ✓",
                          curses.color_pair(GREEN_PAIR)))

    # Phase 2B — QC summary
    if qc_summary:
        if qc_summary.get("failed"):
            qc_color = RED_PAIR if technician == "L2" else YELLOW_PAIR
            verb = "ROUTED TO REWORK" if technician == "L2" else "FAIL recorded with remarks"
            lines.append((
                f"QC tests : {qc_summary['summary']} — {verb}",
                curses.A_BOLD | curses.color_pair(qc_color),
            ))
        else:
            lines.append((
                f"QC tests : {qc_summary['summary']} ✓",
                curses.color_pair(GREEN_PAIR),
            ))

    # Phase 2C — Burn result
    if burn_result:
        if burn_result.get("result") == "PASS":
            lines.append((
                f"Burn test: PASS  (max {burn_result.get('max_temp_c', '—')}°C)",
                curses.color_pair(GREEN_PAIR),
            ))
        elif burn_result.get("result") == "SKIP":
            lines.append((
                f"Burn test: SKIPPED — {burn_result.get('remarks', '')[: w - 24]}",
                curses.color_pair(YELLOW_PAIR),
            ))
        else:  # FAIL
            verb = "ROUTED TO REWORK" if technician == "L2" else "FAIL recorded"
            lines.append((
                f"Burn test: FAIL — {burn_result.get('remarks', '')[: w - 24]} — {verb}",
                curses.A_BOLD | curses.color_pair(RED_PAIR),
            ))

    lines.append(("", 0))
    local_report_status = "saved" if local_report_ok else "warning"
    lines.append((
        f"Server report: {local_report_status} - {local_report_msg[: max(20, w - 28)]}",
        curses.color_pair(GREEN_PAIR if local_report_ok else YELLOW_PAIR),
    ))
    lines.append((
        f"Cloud audit  : {ingest_msg[: max(20, w - 24)]}",
        curses.color_pair(YELLOW_PAIR if reconcile_warning else DIM_PAIR),
    ))

    center_block(stdscr, lines, top_offset=4)
    draw_footer(stdscr, "ENTER restart system   Q drop to shell")
    stdscr.refresh()

    while True:
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return
        if ch in (ord("q"), ord("Q")):
            sys.exit(2)  # signals entrypoint shell to skip auto-shutdown


def screen_testing_process_completion(
    stdscr,
    title: str,
    ident: dict,
    status: str,
    ok: bool,
    detail_lines: list[tuple[str, int]],
) -> str:
    """Final screen for hidden testing workflows."""
    stdscr.erase()
    draw_header(stdscr, title)
    color = GREEN_PAIR if ok else RED_PAIR
    lines = [
        (status, curses.A_BOLD | curses.color_pair(color)),
        ("", 0),
        (f"Serial No.: {ident.get('serial_no', '(unknown)')}", curses.A_BOLD),
        (f"Brand / Model: {ident.get('brand', 'UNKNOWN')}  {ident.get('model', 'UNKNOWN')}", curses.A_NORMAL),
        ("", 0),
    ]
    lines.extend(detail_lines)
    lines.extend([
        ("", 0),
        ("No VSTL app audit or bench report was submitted.",
         curses.A_BOLD | curses.color_pair(YELLOW_PAIR)),
        ("", 0),
        ("[ ENTER ]  Restart", curses.A_BOLD),
        ("[   M   ]  Return to Main Menu", curses.color_pair(CYAN_PAIR)),
        ("[   L   ]  Return to Login", curses.color_pair(CYAN_PAIR)),
    ])
    center_block(stdscr, lines, top_offset=4)
    draw_footer(stdscr, "ENTER restart   M main menu   L login   Q drop to shell")
    stdscr.refresh()

    while True:
        ch = stdscr.getch()
        if ch in (10, 13, curses.KEY_ENTER):
            return TESTING_END_RESTART
        if ch in (ord("m"), ord("M")):
            return TESTING_END_MAIN_MENU
        if ch in (ord("l"), ord("L")):
            return TESTING_END_LOGIN
        if ch in (ord("q"), ord("Q")):
            sys.exit(2)


def screen_testing_restore_completion(stdscr, ident: dict,
                                      erase_result: dict | None,
                                      restore_result: dict | None) -> str:
    """Final screen for the hidden restore-only testing workflow."""
    erase_ok = bool(
        erase_result
        and erase_result.get("ok")
        and erase_result.get("verified")
    )
    restore_ok = bool(
        restore_result
        and restore_result.get("ok")
        and restore_result.get("verified")
    )
    _, w = stdscr.getmaxyx()
    detail_lines = [
        (f"Wipe    : {'PASS' if erase_ok else 'FAIL / SKIPPED'}",
         curses.color_pair(GREEN_PAIR if erase_ok else RED_PAIR)),
        (f"Restore : {'PASS' if restore_ok else 'FAIL / SKIPPED'}",
         curses.color_pair(GREEN_PAIR if restore_ok else RED_PAIR)),
    ]
    if restore_result and restore_result.get("image_name"):
        detail_lines.insert(
            1,
            (f"Image   : {str(restore_result.get('image_name'))[: max(20, w - 18)]}",
             curses.color_pair(CYAN_PAIR)),
        )
    return screen_testing_process_completion(
        stdscr,
        "Testing Mode - Restore Only OS",
        ident,
        "Restore-only OS completed" if restore_ok else "Restore-only OS did not complete",
        restore_ok,
        detail_lines,
    )

def screen_testing_qc_completion(stdscr, ident: dict,
                                 qc_summary: dict | None,
                                 burn_result: dict | None,
                                 erase_result: dict | None = None) -> str:
    failed = bool(qc_summary and qc_summary.get("failed"))
    qc_text = (qc_summary or {}).get("summary") or "QC not completed"
    burn_text = str((burn_result or {}).get("result") or "SKIPPED")
    detail_lines = [
        (f"QC      : {qc_text}", curses.color_pair(YELLOW_PAIR if failed else GREEN_PAIR)),
        (f"Burn    : {burn_text}", curses.color_pair(GREEN_PAIR if burn_text == "PASS" else YELLOW_PAIR)),
    ]
    if erase_result is not None:
        erase_ok = bool(erase_result.get("ok") and erase_result.get("verified"))
        detail_lines.append(
            (f"Erase   : {'PASS' if erase_ok else 'FAIL / SKIPPED'}",
             curses.color_pair(GREEN_PAIR if erase_ok else RED_PAIR))
        )
    return screen_testing_process_completion(
        stdscr,
        "Testing Mode - QC",
        ident,
        "QC testing completed" if qc_summary else "QC testing did not complete",
        bool(qc_summary),
        detail_lines,
    )


def screen_testing_secure_erase_completion(stdscr, ident: dict,
                                           erase_result: dict | None) -> str:
    erase_ok = bool(
        erase_result
        and erase_result.get("ok")
        and erase_result.get("verified")
    )
    method = str((erase_result or {}).get("method") or "SKIPPED")
    detail_lines = [
        (f"Erase   : {'PASS' if erase_ok else 'FAIL / SKIPPED'}",
         curses.color_pair(GREEN_PAIR if erase_ok else RED_PAIR)),
        (f"Method  : {method}", curses.color_pair(DIM_PAIR)),
    ]
    return screen_testing_process_completion(
        stdscr,
        "Testing Mode - Secure Erase",
        ident,
        "Secure erase completed" if erase_ok else "Secure erase did not complete",
        erase_ok,
        detail_lines,
    )


def run_testing_restore_only(stdscr, cfg: dict) -> str:
    """Hidden testing workflow: wipe then restore an OS image without reporting."""
    tech = "TESTING"
    _begin_screen_frame(stdscr, "Testing Mode - Restore Only OS")
    center_block(stdscr, [
        ("Detecting system identity and available image backup...", curses.A_BOLD),
        ("Reports are disabled for this run.", curses.color_pair(DIM_PAIR)),
    ], top_offset=5)
    draw_footer(stdscr, "Please wait")
    stdscr.refresh()

    ident = hw.detect_identity()
    cpu = hw.detect_cpu()
    if not _restore_backup_available(stdscr, ident, cfg, cpu):
        return screen_testing_restore_completion(stdscr, ident, None, None)

    erase = phase3_secure_erase(
        stdscr,
        ident,
        cfg,
        tech,
        operator=None,
        suppress_reporting=True,
    )
    erase_result = (erase or {}).get("result") or {}
    if not (erase_result.get("ok") and erase_result.get("verified")):
        _show_message(
            stdscr,
            "Restore cancelled because drive wipe did not complete.",
            color=RED_PAIR,
            secs=4,
        )
        return screen_testing_restore_completion(stdscr, ident, erase_result, None)

    restore = phase3_restore(
        stdscr,
        ident,
        cfg,
        tech,
        cpu_info=cpu,
        suppress_reporting=True,
    )
    restore_result = (restore or {}).get("result") or {}
    return screen_testing_restore_completion(stdscr, ident, erase_result, restore_result)


def run_testing_qc_only(stdscr, cfg: dict) -> str:
    """Hidden testing workflow: run QC locally without app/server reporting."""
    tech = "TESTING"
    _begin_screen_frame(stdscr, "Testing Mode - QC")
    center_block(stdscr, [
        ("Detecting system hardware before local QC...", curses.A_BOLD),
        ("Reports are disabled for this run.", curses.color_pair(DIM_PAIR)),
    ], top_offset=5)
    draw_footer(stdscr, "Please wait")
    stdscr.refresh()

    ident, _cpu, _storage = _detect_and_show_hardware_profile(stdscr)
    qc_summary, burn_result, _qc_elapsed_sec, _post_qc_details = (
        _run_qc_and_burn_flow(stdscr, cfg, tech)
    )
    erase_result: dict | None = None
    if _confirm_yn(
        stdscr,
        "Run Certified Secure Erase now?\n"
        "(Y = wipe drive locally, N = skip)",
    ):
        erase = phase3_secure_erase(
            stdscr,
            ident,
            cfg,
            tech,
            operator=None,
            suppress_reporting=True,
        )
        erase_result = (erase or {}).get("result") or {}
    return screen_testing_qc_completion(stdscr, ident, qc_summary, burn_result, erase_result)


def run_testing_secure_erase(stdscr, cfg: dict) -> str:
    """Hidden testing workflow: run secure erase locally without reporting."""
    tech = "TESTING"
    _begin_screen_frame(stdscr, "Testing Mode - Secure Erase")
    center_block(stdscr, [
        ("Detecting system hardware before local secure erase...", curses.A_BOLD),
        ("Reports are disabled for this run.", curses.color_pair(DIM_PAIR)),
    ], top_offset=5)
    draw_footer(stdscr, "Please wait")
    stdscr.refresh()

    ident, _cpu, _storage = _detect_and_show_hardware_profile(stdscr)
    erase = phase3_secure_erase(
        stdscr,
        ident,
        cfg,
        tech,
        operator=None,
        suppress_reporting=True,
    )
    erase_result = (erase or {}).get("result") or {}
    return screen_testing_secure_erase_completion(stdscr, ident, erase_result)


# ---------------------------------------------------------------------------
# Main controller
# ---------------------------------------------------------------------------
def run(stdscr) -> int:
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.leaveok(True)
    _setup_colors()
    cfg = load_config()

    # 0. Operator login gates all bench screens. The operator's VSTL 360
    # account is cloud-authenticated, but the L1/L2 bench mode is chosen
    # locally so either layer can process either workflow when required.
    while True:
        operator = screen_login(stdscr, cfg)
        _drain_pending_input(stdscr)
        if _operator_testing_mode(operator):
            while True:
                choice = screen_testing_mode_menu(stdscr)
                if choice == TESTING_RESTORE_ONLY_CHOICE:
                    testing_action = run_testing_restore_only(stdscr, cfg)
                elif choice == TESTING_QC_ONLY_CHOICE:
                    testing_action = run_testing_qc_only(stdscr, cfg)
                elif choice == TESTING_SECURE_ERASE_CHOICE:
                    testing_action = run_testing_secure_erase(stdscr, cfg)
                else:
                    continue
                if testing_action == TESTING_END_MAIN_MENU:
                    continue
                if testing_action == TESTING_END_LOGIN:
                    break
                return 0
            continue
        if not operator.get("_bench_layer_confirmed") and _operator_has_layer_access(operator):
            selected_layer = screen_working_layer(stdscr, _operator_name(operator))
            if not selected_layer:
                continue
            _set_operator_working_layer(operator, selected_layer, cfg)
        _drain_pending_input(stdscr)
        tech = _operator_bench_mode(operator)
        if not tech:
            auth_logout(cfg, _operator_token(operator))
            _begin_screen_frame(stdscr, "VSTL 360 Login")
            center_block(stdscr, [
                ("Please login with an L1/L2 layer or Capture permission.",
                 curses.A_BOLD | curses.color_pair(RED_PAIR)),
                ("Capture-only Imaging users can continue without L1/L2.",
                 curses.color_pair(DIM_PAIR)),
            ], top_offset=5)
            draw_footer(stdscr, "ENTER return to login")
            stdscr.refresh()
            stdscr.getch()
            continue

        _drain_pending_input(stdscr)
        choice = screen_main_menu(stdscr, tech, operator)
        if tech == "L1" and _menu_choice_requires_l1_box(choice):
            if not screen_box_picker(stdscr, cfg, operator):
                continue
            _drain_pending_input(stdscr)
        break

    # 2A. Phase 2A — Lock & MDM/BIOS Audit (runs FIRST, per spec)
    audit = screen_lock_audit_run(stdscr)

    # Manual confirm prompts for hardware-only locks (per design choice 2c).
    # We ALWAYS prompt for BIOS password and ATA security regardless of the
    # auto-detect result — false negatives are too common on enterprise
    # hardware where vendors don't expose these flags via SMBIOS.
    bios_chk = audit["checks"]["bios_password"]
    if bios_chk.get("manual_confirm_required"):
        is_locked = screen_lock_manual_confirm(
            stdscr,
            "BIOS Admin Password",
            "Did the bench laptop prompt you for a BIOS / Setup password\n"
            "when you tried to enter F10 / DEL / F1 BIOS setup?",
        )
        if is_locked:
            bios_chk["present"] = True
            bios_chk["status"] = "ENABLED"
            bios_chk["evidence"] = (
                (bios_chk.get("evidence") or "")
                + " | manual_confirm=YES"
            ).strip(" |")
            if "bios_password" not in audit["detected_locks"]:
                audit["detected_locks"].append("bios_password")
        bios_chk["manually_confirmed"] = True

    ata_chk = audit["checks"]["ata_security"]
    if ata_chk.get("manual_confirm_required"):
        is_locked = screen_lock_manual_confirm(
            stdscr,
            "Drive Password / ATA Security",
            "Has the storage drive (SSD / HDD / NVMe) been previously\n"
            "set with a drive password (ATA Security or NVMe lock)?",
        )
        if is_locked:
            ata_chk["present"] = True
            ata_chk["status"] = "ENABLED"
            ata_chk["evidence"] = (
                (ata_chk.get("evidence") or "")
                + " | manual_confirm=YES"
            ).strip(" |")
            if "ata_security" not in audit["detected_locks"]:
                audit["detected_locks"].append("ata_security")
        ata_chk["manually_confirmed"] = True

    # Recompute halted after manual confirms
    audit["halted"] = bool(audit["detected_locks"])
    audit["summary"] = (
        ", ".join(la.LOCK_LABELS[k] for k in audit["detected_locks"])
        if audit["detected_locks"] else "All clear"
    )

    override_info = None
    l1_lock_continue = False
    if audit["halted"]:
        decision = screen_lock_halt(stdscr, audit, tech)
        if decision == "shell":
            sys.exit(2)  # entrypoint will skip auto-shutdown
        if decision == "continue_l1":
            l1_lock_continue = True
        if decision == "override":
            override_info = screen_lock_admin_override(stdscr, cfg, audit["summary"])
            if not override_info:
                # Operator cancelled override flow — submit halt and power off
                decision = "submit_halt"
        # Whether decision == submit_halt OR override succeeded, we still
        # POST the audit; the only difference is the override metadata.
        audit["override"] = override_info  # None or {user_id, user_name, role, timestamp}
        audit["decision"] = decision  # "override" | "submit_halt"
        audit["l1_continue"] = l1_lock_continue
    else:
        screen_lock_audit_clear(stdscr, audit)
        audit["override"] = None
        audit["decision"] = "clear"
        audit["l1_continue"] = False

    # 3. Hardware detection — only if cleared OR override granted
    qc_summary = None
    burn_result = None
    qc_elapsed_sec = None
    phase3_results: dict = {}
    pre_phase3_os_info: dict | None = None
    post_qc_details: dict | None = None
    hardware_label_overrides: dict = {}
    # Menu choices 0 (Full restore) and 1 (QC Only) require QC + Burn.
    # Choices 2 (Erase only) and 3 (Capture only) skip the QC test run so
    # the operator isn't forced through 20 minutes of QC for a 90-second
    # NVMe sanitize or a 60-minute capture from a known-good unit.
    needs_qc = choice in (0, 1)
    if (not audit["halted"]) or override_info or l1_lock_continue:
        ident = hw.detect_identity()
        cpu = hw.detect_cpu()
        if choice == 0 and not _restore_backup_available(stdscr, ident, cfg, cpu):
            return 0
        gpu = hw.detect_gpu()
        ram = hw.detect_ram()
        storage = hw.detect_storage()
        battery = hw.detect_battery()
        storage_drives = storage.get("drives") or []
        primary_device = (
            (storage_drives[0] or {}).get("device", "")
            if storage_drives else ""
        )
        try:
            pre_phase3_os_info = ic.inspect_installed_windows(primary_device)
        except Exception:
            pre_phase3_os_info = None

        screen_model(stdscr, ident)
        screen_sku(stdscr, ident)
        screen_cpu(stdscr, cpu)
        screen_gpu(stdscr, gpu)
        screen_ram(stdscr, ram)
        screen_storage(stdscr, storage)
        screen_battery(stdscr, battery)
        # Firmware does not expose every OEM CT/spare label on all laptops.
        # Keep the bench flow automatic and report only values detected from
        # SMBIOS/ACPI/NVMe/SPD sources.
        hardware_label_overrides = {}

        if needs_qc:
            # 4/5. Phase 2B QC + Phase 2C Burn / Stress.
            qc_summary, burn_result, qc_elapsed_sec, post_qc_details = (
                _run_qc_and_burn_flow(stdscr, cfg, tech)
            )

        # ---------------- Phase 3 action dispatch ----------------
        # Skip Phase 3 entirely on L2 rework routing — the unit is going
        # back for repair, the bench shouldn't erase or restore it yet.
        _l2_rework = (
            (qc_summary and qc_summary.get("failed") and tech == "L2")
            or (burn_result and burn_result.get("result") == "FAIL"
                and tech == "L2")
        )
        if not _l2_rework:
            if choice == 0:
                # Restore Approved: erase first, then restore from golden copy
                erase = phase3_secure_erase(stdscr, ident, cfg, tech, operator)
                if erase:
                    phase3_results["erase"] = erase
                    erase_result = (erase or {}).get("result") or {}
                    if erase_result.get("ok") and erase_result.get("verified"):
                        restore = phase3_restore(stdscr, ident, cfg, tech, cpu_info=cpu)
                        if restore:
                            phase3_results["restore"] = restore
                    else:
                        _show_message(
                            stdscr,
                            "Restore cancelled because drive wipe did not complete.",
                            color=RED_PAIR,
                            secs=4,
                        )
            elif choice == 1:
                # QC Only with OPTIONAL secure erase per founder design
                if _confirm_yn(stdscr,
                                "Run Certified Secure Erase now?\n"
                                "(Y = wipe drive, N = skip)"):
                    erase = phase3_secure_erase(stdscr, ident, cfg, tech, operator)
                    if erase:
                        phase3_results["erase"] = erase
            elif choice == 2:
                # Certified Secure Erase only
                erase = phase3_secure_erase(stdscr, ident, cfg, tech, operator)
                if erase:
                    phase3_results["erase"] = erase
            elif choice == 3:
                # Capture Full System Image only
                capture = phase3_capture(stdscr, ident, cfg, tech, cpu_info=cpu, audit=audit)
                if capture:
                    phase3_results["capture"] = capture
    else:
        # HALT — set aside. We still build a minimal payload using whatever
        # identity dmidecode returned during the lock audit so the asset
        # can still be linked in the API.
        ident = hw.detect_identity()

    # Build the payload (compose without re-running every detector)
    payload = hw.collect_phase1(technician_level=tech, bench_id=_bench_id(cfg))
    _attach_operator_to_payload(payload, operator)
    if _menu_choice_requires_l1_box(choice):
        _attach_box_scope_to_payload(payload, operator)
    _apply_hardware_label_overrides(payload, hardware_label_overrides)
    payload["selected_option"] = choice + 1  # 1-indexed for human reading
    payload["selected_option_label"] = MENU_OPTIONS[choice]
    payload["session_started_at"] = datetime.now(timezone.utc).isoformat()
    payload["lock_audit"] = audit
    if qc_summary is not None:
        payload["qc_tests"] = qc_summary
        payload["qc_elapsed_sec"] = qc_elapsed_sec if qc_elapsed_sec is not None else 0
        if post_qc_details:
            payload["cosmetic_grade"] = post_qc_details.get("cosmetic_grade", "")
            payload["parts_required"] = post_qc_details.get("parts_required", "")
            payload["additional_remarks"] = post_qc_details.get("additional_remarks", "")
        keyboard_result = (qc_summary.get("tests") or {}).get("keyboard") or {}
        keyboard_profile = keyboard_result.get("keyboard_profile") or {}
        if keyboard_profile:
            payload["keyboard_type"] = keyboard_profile.get("name", "")
            payload["keyboard_language"] = keyboard_profile.get("print_format", "")
            payload["keyboard_status"] = keyboard_profile.get("backlight", "")
            payload["keyboard_profile"] = keyboard_profile
            payload.setdefault("raw_data", {})["keyboard"] = keyboard_profile
    if burn_result is not None:
        payload["burn_test"] = burn_result
    if phase3_results:
        payload["phase3"] = {
            k: {
                "ok": (v.get("result") or {}).get("ok", False),
                "verified": (v.get("result") or {}).get("verified", False),
                "method": (v.get("result") or {}).get("method", ""),
                "wipe_method": (v.get("result") or {}).get("method", ""),
                "device": (v.get("result") or {}).get("device", ""),
                "duration_sec": (v.get("result") or {}).get("duration_sec", 0),
                "image_name": (v.get("result") or {}).get("image_name", ""),
                "certificate_id": (v.get("certificate") or {}).get("certificate_id", "") if k == "erase" else "",
                "secure_erase_reg_id": (v.get("certificate") or {}).get("certificate_id", "") if k == "erase" else "",
                "verification_hash": (v.get("certificate") or {}).get("verification_hash", "") if k == "erase" else "",
                "wipe_standard": (v.get("certificate") or {}).get("wipe_standard", "") if k == "erase" else "",
                "clear_only_exception": (v.get("result") or {}).get("clear_only_exception", False) if k == "erase" else False,
                "clear_only_exception_reason": (v.get("result") or {}).get("clear_only_exception_reason", "") if k == "erase" else "",
                "certificate_status": (v.get("certificate") or {}).get("certificate_status", "") if k == "erase" else "",
                "remote_post_ok": (v.get("certificate") or {}).get("remote_post_ok", False) if k == "erase" else False,
                "remote_error": (v.get("certificate") or {}).get("remote_error", "") if k == "erase" else "",
                "remote_certificate_id": (v.get("certificate") or {}).get("remote_certificate_id", "") if k == "erase" else "",
                "certificate": (v.get("certificate") or {}) if k == "erase" else {},
                "golden_copy_id": (v.get("register") or {}).get("golden_copy_id", "") if k == "capture" else "",
                "restore_result": (v.get("result") or {}).get("result", "") if k == "restore" else "",
                "error_message": (v.get("result") or {}).get("error_message", ""),
            }
            for k, v in phase3_results.items()
        }
    if audit["halted"] and not override_info and not l1_lock_continue:
        # Set-aside path — flag the test_type so admin dashboards can filter
        payload["test_type"] = "phase2a_halt"
        payload["status"] = "halted"
    elif audit["halted"] and l1_lock_continue:
        payload["test_type"] = "phase2a_l1_continue"
        payload["status"] = "mdm_warning_l1_continue"
    elif (qc_summary and qc_summary.get("failed") and tech == "L2") or \
         (burn_result and burn_result.get("result") == "FAIL" and tech == "L2"):
        # L2 QC or burn failure → backend will flip Status=L2_Rework
        payload["test_type"] = "phase2b_qc"
        payload["status"] = "rework_required"

    # Save a local copy first — survives any network failure for forensics
    preserved_os_info = (
        pre_phase3_os_info
        if "erase" in phase3_results and "restore" not in phase3_results
        else None
    )
    _attach_report_details(payload, qc_summary, preserved_os_info)
    try:
        with open(LOCAL_AUDIT_FILE, "w") as f:
            json.dump(payload, f, indent=2)
    except OSError:
        pass

    screen_submitting(stdscr)
    ok, msg = post_ingest(payload, cfg, operator)
    _attach_audit_submission_status(payload, ok, msg)
    try:
        with open(LOCAL_AUDIT_FILE, "w") as f:
            json.dump(payload, f, indent=2)
    except OSError:
        pass
    local_ok, local_msg = post_local_report(payload, cfg)
    screen_completion(stdscr, ident, ok, msg, tech, choice,
                      lock_audit=audit, qc_summary=qc_summary,
                      burn_result=burn_result,
                      local_report_ok=local_ok,
                      local_report_msg=local_msg)
    release_successful_audit_dhcp_lease(cfg, ok)

    return 0


def main() -> int:
    try:
        return curses.wrapper(run)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
