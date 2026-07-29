#!/usr/bin/env bash
# =============================================================================
# VSTL Imaging — One-shot installer for the FOG server
# =============================================================================
# Run this on the FOG/imaging server after extracting the tarball.
# It performs the full Phase-1 deployment in one go:
#
#   1. Sanity-check the .env file (creates it from .env.example if missing).
#   2. Rebuild the bootable Live ISO with the new Python TUI baked in.
#   3. Publish the ISO + kernel/initrd/squashfs to FOG over HTTP for PXE boot.
#   4. Install the protected local CSV/XLSX reporting endpoint.
#
# Usage:
#   sudo ./INSTALL.sh                          # full reinstall (default)
#   sudo ./INSTALL.sh --iso-only               # only rebuild the ISO
#   sudo ./INSTALL.sh --pxe-only               # only re-publish PXE assets
#   sudo ./INSTALL.sh --skip-iso               # alias for --pxe-only
#   sudo ./INSTALL.sh --help
#
# Re-runnable: each step is idempotent. Re-running after changing any
# bench-client/* file will cleanly rebuild everything.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

c_ok()   { printf '\e[32m✔\e[0m %s\n' "$*"; }
c_warn() { printf '\e[33m⚠\e[0m %s\n' "$*"; }
c_err()  { printf '\e[31m✗\e[0m %s\n' "$*" >&2; }
c_step() { printf '\n\e[1;35m▶ %s\e[0m\n' "$*"; }

usage() {
    sed -n '3,17p' "$0" | sed 's/^# \?//'
    exit 0
}

DO_ISO=1
DO_PXE=1
for arg in "$@"; do
    case "$arg" in
        --iso-only)            DO_PXE=0 ;;
        --pxe-only|--skip-iso) DO_ISO=0 ;;
        -h|--help)             usage ;;
        *)
            c_err "Unknown flag: $arg"
            usage
            ;;
    esac
done

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    c_err "Run as root (sudo). The ISO build needs mount/unsquashfs privileges."
    exit 1
fi

# ---------- 1. Config sanity --------------------------------------------------
c_step "Step 1/3 — Config sanity"
if [[ ! -f .env ]]; then
    if [[ -f .env.example ]]; then
        cp .env.example .env
        chmod 600 .env
        c_warn "Created .env from .env.example — edit VSTL_API_BASE / VSTL_API_KEY before benches use it"
    else
        c_err ".env not found AND .env.example missing — abort"
        exit 1
    fi
fi
chmod 600 .env

REQ_KEYS=(VSTL_API_BASE VSTL_API_KEY)
for k in "${REQ_KEYS[@]}"; do
    if ! grep -qE "^${k}=.+" .env; then
        c_err ".env missing or empty: $k — abort"
        exit 1
    else
        c_ok ".env has $k"
    fi
done

# Hard-fail on placeholder values so an unedited .env never produces a non-functional ISO.
# (Real-world incident 2026-05-08: a fresh deployment dir was rebuilt with the
# placeholder key and shipped to PXE — bench got HTTP 401 because the key was
# literally "REPLACE_ME_WITH_REAL_API_KEY". This guard catches that class of
# mistake before any ISO is built.)
PLACEHOLDER_PATTERNS=(
    'REPLACE_ME'
    'REPLACE_AT_ISO_BUILD_TIME'
    'YOUR_API_KEY_HERE'
    'CHANGE_?ME'
)
for pat in "${PLACEHOLDER_PATTERNS[@]}"; do
    if grep -qiE "VSTL_API_KEY=.*${pat}" .env; then
        c_err ".env still has a placeholder VSTL_API_KEY (matched /${pat}/) — edit .env with the real key from VSTL360 → Imaging → API Keys, then re-run."
        exit 1
    fi
done

# Sanity-check the API key prefix matches the expected format (`vstl_img_` + hex).
# This catches typos like missing prefix, accidental whitespace, leftover quotes.
KEY_VALUE=$(grep -E '^VSTL_API_KEY=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')
if [[ ! "$KEY_VALUE" =~ ^vstl_img_[a-f0-9]{40,}$ ]]; then
    c_err ".env has VSTL_API_KEY in an unexpected format. Expected: vstl_img_<48+ hex chars>. Got: ${KEY_VALUE:0:20}…"
    c_err "Generate a fresh key from VSTL360 → Imaging → API Keys."
    exit 1
fi
c_ok "VSTL_API_KEY format looks valid (vstl_img_${KEY_VALUE:9:8}…)"

# Install reporting before rebuilding boot media so its generated token is
# baked into both PXE and USB runtime config.
c_step "Installing local CSV/XLSX reporting"
if [[ ! -x ./07_install_reporting.sh ]]; then
    c_err "07_install_reporting.sh missing"
    exit 1
fi
./07_install_reporting.sh
c_ok "Local reporting endpoint installed"

# ---------- 2. Rebuild the Live ISO -------------------------------------------
if [[ "$DO_ISO" -eq 1 ]]; then
    c_step "Step 2/3 — Building the bootable VSTL Live ISO (Clonezilla-based)"
    if [[ ! -x ./04_build_live_iso_clonezilla.sh ]]; then
        c_err "04_build_live_iso_clonezilla.sh missing — abort"
        exit 1
    fi
    ./04_build_live_iso_clonezilla.sh
    if [[ ! -f build/vstl-live-amd64.iso ]]; then
        c_err "ISO build did not produce build/vstl-live-amd64.iso"
        exit 1
    fi
    c_ok "ISO built: $(ls -lh build/vstl-live-amd64.iso | awk '{print $5,$9}')"
else
    c_warn "Skipping ISO rebuild (per --pxe-only)"
fi

# ---------- 3. Publish PXE assets to FOG over HTTP ----------------------------
if [[ "$DO_PXE" -eq 1 ]]; then
    c_step "Step 3/3 — Publishing kernel + initrd + squashfs to FOG for PXE"
    if [[ ! -x ./06_setup_pxe_netboot.sh ]]; then
        c_err "06_setup_pxe_netboot.sh missing — abort"
        exit 1
    fi
    ./06_setup_pxe_netboot.sh
    c_ok "PXE assets published. See 06_pxe_netboot.md for the FOG menu entry."
else
    c_warn "Skipping PXE publish (per --iso-only)"
fi

# ---------- Done --------------------------------------------------------------
echo
c_ok "VSTL Imaging deployment complete (Phase 1 + Phase 2A/2B/2C v2 bundled)."
echo ""
echo "Phase 2 v2 (2026-05-11) changes:"
echo "  • Display QC — true full-screen R/G/B/W color fill via /dev/fb0"
echo "  • Camera QC — live framebuffer preview with fswebcam/fbi fallback"
echo "  • Keyboard QC — full evdev raw capture (standard keys, F-keys, numpad)"
echo "  • Speaker / Microphone QC — ALSA Master/PCM forced to 90% + unmuted"
echo "  • Storage QC — new screen, hdparm read throughput on primary disk"
echo "  • Ports QC — interactive USB udev + HDMI hotplug detection"
echo "  • Every test — [R] Retest option (re-run without losing place)"
echo "  • L1 layer — Microphone test added (operator feedback)"
echo "  • NVMe — smartctl -d nvme flag fix for older smartmontools"
echo "  • Submit — User-Agent header (fixes Cloudflare 1010 block)"
echo ""
cat <<'NEXT'

Next steps:
  • If a bench laptop is plugged into the same VLAN as this FOG server,
    PXE-boot it and confirm the full pipeline:
        – Technician selection screen appears
        – Main menu auto-selects Option 1 after 5 seconds
        – Phase 1 hardware-detect screens auto-advance every 3 seconds
        – Phase 2A Lock & MDM/BIOS audit runs (halts on locks, else passes)
        – Phase 2B Interactive QC tests run (Display / KB / Camera / etc.)
        – Phase 2C 5-min Burn/Stress test runs (CPU + RAM + disk + thermal)
        – Completion screen shows ✅ + record_id + auto-poweroff in 10 sec
  • Phase 3 (Certified Secure Erase + Capture/Restore) will ship as the next
    update — re-run INSTALL.sh after each new release tarball.
  • Operator quickstart: PHASE1_TUI_QUICKSTART.md

NEXT
