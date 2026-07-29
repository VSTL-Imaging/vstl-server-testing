#!/usr/bin/env bash
# =============================================================================
# 04_build_live_iso.sh — Builds the VSTL Live Linux ISO
# =============================================================================
# Run as: sudo /opt/vstl-imaging/04_build_live_iso.sh
#
# What this does:
#  1. Installs Debian's `live-build` toolchain
#  2. Creates a build tree with the VSTL bench client baked in
#  3. Substitutes secrets from .env into config.env baked into the ISO
#  4. Builds a bootable ISO (~700 MB)
#  5. Drops the result at /opt/vstl-imaging/build/vstl-live-amd64.iso
#
# Why live-build?
# - It's the upstream Debian tool — well-documented, predictable.
# - We don't have to ship Ubuntu's licence-encumbered installer.
# - Output works with FOG's PXE chain (FOG supports custom ISO targets).
#
# Build time: 20-40 min on a 4-core box (mostly downloads + squashfs compress).
# =============================================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn() { echo -e "${YELLOW}[$(date +%H:%M:%S)] WARN:${NC} $*"; }
die()  { echo -e "${RED}[$(date +%H:%M:%S)] FATAL:${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Must be run as root (use sudo)."

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
[[ -f "$SCRIPT_DIR/.env" ]] || die "Missing $SCRIPT_DIR/.env — copy .env.example and fill in values first."
# shellcheck disable=SC1091
source "$SCRIPT_DIR/.env"

: "${VSTL_API_BASE:?VSTL_API_BASE missing in .env}"
: "${VSTL_API_KEY:?VSTL_API_KEY missing in .env}"
: "${SERVER_IP:?SERVER_IP missing in .env}"
[[ "$VSTL_API_KEY" == "REPLACE_ME_WITH_REAL_API_KEY" ]] && \
    die "VSTL_API_KEY in .env still has the placeholder value. Replace it first."

LIVE_ISO_NAME="${LIVE_ISO_NAME:-vstl-live-amd64.iso}"
LIVE_ISO_HOSTNAME="${LIVE_ISO_HOSTNAME:-vstl-bench}"
LIVE_ISO_USER="${LIVE_ISO_USER:-vstl}"
LIVE_ISO_PASS="${LIVE_ISO_PASS:-vstl@2026}"

BUILD_DIR="$SCRIPT_DIR/build"
LB_DIR="$BUILD_DIR/lb"

# --- 1. Install live-build + helpers ---
log "Installing live-build toolchain..."
apt-get update -qq
apt-get install -y -qq live-build live-boot live-config debootstrap squashfs-tools \
    xorriso isolinux syslinux-common mtools dosfstools \
    || die "Failed to install live-build"

# --- 1a. Workaround: Ubuntu's live-build 3.0~a57 hardcodes legacy Ubuntu theme
# packages `syslinux-themes-ubuntu-oneiric` (Ubuntu 11.10, 2011!) and
# `gfxboot-theme-ubuntu` across MULTIPLE files. Sweep everything under
# /usr/lib/live, /usr/share/live, /etc/live and strip these references.
# Idempotent + safe to re-run — creates a `.vstl.bak` once per file.
log "Scanning Ubuntu live-build for obsolete theme references..."
THEME_OFFENDERS=$(grep -rl 'syslinux-themes-ubuntu-oneiric\|gfxboot-theme-ubuntu' \
    /usr/lib/live /usr/share/live /etc/live 2>/dev/null || true)
if [[ -n "$THEME_OFFENDERS" ]]; then
    while IFS= read -r f; do
        log "  Patching: $f"
        cp -n "$f" "${f}.vstl.bak" || true
        sed -i -E 's/[[:space:]]*syslinux-themes-ubuntu-oneiric//g; s/[[:space:]]*gfxboot-theme-ubuntu//g' "$f"
    done <<< "$THEME_OFFENDERS"
fi

# --- 1b. Surgical patch: even with LB_SYSLINUX_THEME="" the hook's case "*)"
# branch still tries to install `syslinux-themes-` (trailing dash). Comment
# out the offending Check_package lines. We keep `syslinux` + `syslinux-common`
# installed; we only skip the theme (cosmetic graphics we don't need).
LB_SYSLINUX_HOOK=$(find /usr/lib/live /usr/share/live -name 'lb_binary_syslinux' 2>/dev/null | head -1)
if [[ -n "$LB_SYSLINUX_HOOK" ]] && grep -Pq '^\s*Check_package[^#].*syslinux-themes' "$LB_SYSLINUX_HOOK"; then
    log "Patching $LB_SYSLINUX_HOOK to skip cosmetic theme install..."
    cp -n "$LB_SYSLINUX_HOOK" "${LB_SYSLINUX_HOOK}.vstl.bak2" || true
    python3 - "$LB_SYSLINUX_HOOK" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
src = p.read_text()
# Comment out the syslinux-themes Check_package and its bootlogo sibling
out = []
for ln in src.splitlines():
    if ('syslinux-themes' in ln or 'bootlogo.tar.gz' in ln) and 'Check_package' in ln and not ln.lstrip().startswith('#'):
        out.append(ln.replace('Check_package', '# [VSTL-patched] Check_package', 1))
    else:
        out.append(ln)
p.write_text('\n'.join(out) + '\n')
PY
fi

# --- 1b. Detect host OS family so we pick a distribution live-build will
# actually fetch successfully. Ubuntu's live-build hard-codes Ubuntu mirrors;
# Debian's hard-codes Debian mirrors. Mismatch → 404 on InRelease.
DISTRIBUTION="${LIVE_ISO_DISTRIBUTION:-}"
KERNEL_PKG=""
if [[ -z "$DISTRIBUTION" ]]; then
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        if [[ "${ID:-}" == "ubuntu" ]]; then
            DISTRIBUTION="noble"
            KERNEL_PKG="linux-image-generic"
        else
            DISTRIBUTION="bookworm"
            KERNEL_PKG="linux-image-amd64"
        fi
    else
        DISTRIBUTION="bookworm"
        KERNEL_PKG="linux-image-amd64"
    fi
fi
# Allow explicit override via env
[[ -n "$DISTRIBUTION" ]] || DISTRIBUTION="bookworm"
[[ -n "$KERNEL_PKG" ]] || {
    case "$DISTRIBUTION" in
        noble|jammy|focal) KERNEL_PKG="linux-image-generic" ;;
        *) KERNEL_PKG="linux-image-amd64" ;;
    esac
}
log "Using base distribution: $DISTRIBUTION  (kernel package: $KERNEL_PKG)"

# --- 2. Prepare build tree ---
log "Setting up build tree at $LB_DIR ..."
rm -rf "$LB_DIR"
mkdir -p "$LB_DIR"
cd "$LB_DIR"

# Configure live-build for Debian 12 (bookworm), amd64, with iPXE-friendly bootloader
#
# Compatibility note: Ubuntu's live-build (3.0~a57) uses the older singular
# `--bootloader` flag and does NOT accept `--bootloaders` (plural) or
# `--image-name`. We detect which flag is available and adapt. The output
# filename defaults to `live-image-amd64.hybrid.iso` which step 8 already
# handles below.
if lb config --help 2>&1 | grep -q -- '--bootloaders'; then
    BOOTLOADER_FLAGS=(--bootloaders "syslinux,grub-efi")
else
    # Legacy live-build: syslinux only (BIOS PXE). FOG still chainloads UEFI.
    BOOTLOADER_FLAGS=(--bootloader syslinux)
fi

# Enable universe/multiverse so packages like nwipe, stress-ng, fio,
# live-boot, live-config resolve. --parent-archive-areas is only present
# on newer live-build — detect and skip on older versions.
ARCHIVE_FLAGS=(--archive-areas "main restricted universe multiverse")
if lb config --help 2>&1 | grep -q -- '--parent-archive-areas'; then
    ARCHIVE_FLAGS+=(--parent-archive-areas "main restricted universe multiverse")
fi

lb config \
    --architectures amd64 \
    --distribution "$DISTRIBUTION" \
    --binary-images iso-hybrid \
    "${BOOTLOADER_FLAGS[@]}" \
    "${ARCHIVE_FLAGS[@]}" \
    --apt-recommends false \
    --memtest none \
    --iso-application "VSTL Imaging Live" \
    --iso-publisher "VSTL 360" \
    --iso-volume "VSTL_LIVE"

# --- 2b. Clear obsolete theme defaults baked into config/binary by lb config.
# Set theme to "live-build" — that's live-build's own built-in minimal theme
# (no apt install, no missing assets). Empty string causes path concatenation
# bugs ("/usr/share/syslinux/themes//isolinux-live").
if [[ -f config/binary ]]; then
    log "Setting LB_SYSLINUX_THEME=live-build (built-in minimal theme)..."
    sed -i -E 's/^LB_SYSLINUX_THEME=.*/LB_SYSLINUX_THEME="live-build"/' config/binary
    sed -i -E 's/^LB_BOOTLOADER_SYSLINUX_THEME=.*/LB_BOOTLOADER_SYSLINUX_THEME="live-build"/' config/binary
fi

# --- 3. Package list ---
mkdir -p config/package-lists
cat > config/package-lists/vstl-imaging.list.chroot <<'EOF'
# Hardware probing
dmidecode
smartmontools
hdparm
nvme-cli
pciutils
usbutils

# Network / DNS / TLS
curl
ca-certificates
iproute2
iputils-ping
network-manager
wpasupplicant
wireless-tools
iw

# Wipe / disk
util-linux
parted
gdisk
nwipe

# Stress / diagnostics
stress-ng
fio

# JSON parsing for the bench client
jq

# Bluetooth / audio detection helpers
bluez
alsa-utils
firmware-sof-signed
ethtool
evtest

# Fingerprint QC capture
fprintd
libfprint-2-2
libpam-fprintd

# DRM/KMS display connector detection
libdrm-tests

# Quality of life
vim-tiny
less
tmux
openssh-server

# Boot helpers (so live-build can detect kernel/initrd correctly)
# NOTE: The kernel package name is distro-specific. A sed hook below
# rewrites __KERNEL_PKG__ → the actual package for the chosen base.
__KERNEL_PKG__
live-boot
live-config
systemd-sysv
EOF

# Rewrite the kernel placeholder to the detected package name
sed -i "s|__KERNEL_PKG__|$KERNEL_PKG|" config/package-lists/vstl-imaging.list.chroot

# --- 4. Bake the bench client into /opt/vstl/ in the chroot ---
log "Baking VSTL bench client into the ISO..."
mkdir -p config/includes.chroot/opt/vstl
cp "$SCRIPT_DIR/bench-client/vstl-imaging-client.sh" config/includes.chroot/opt/vstl/
chmod +x config/includes.chroot/opt/vstl/vstl-imaging-client.sh

# Substitute real secrets into config.env (live in the ISO)
sed \
    -e "s#REPLACE_AT_ISO_BUILD_TIME#__PLACEHOLDER__#g" \
    "$SCRIPT_DIR/bench-client/config.env.example" > /tmp/config.env.tpl

# Now the actual substitution
escape() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
API_BASE_E=$(escape "$VSTL_API_BASE")
API_KEY_E=$(escape "$VSTL_API_KEY")
SRV_E=$(escape "http://$SERVER_IP/fog")

sed \
    -e "s|VSTL_API_BASE=\".*\"|VSTL_API_BASE=\"$API_BASE_E\"|" \
    -e "s|VSTL_API_KEY=\".*\"|VSTL_API_KEY=\"$API_KEY_E\"|" \
    -e "s|FOG_SERVER=\".*\"|FOG_SERVER=\"$SRV_E\"|" \
    /tmp/config.env.tpl > config/includes.chroot/opt/vstl/config.env
chmod 600 config/includes.chroot/opt/vstl/config.env

# --- 5. Install the systemd service file ---
mkdir -p config/includes.chroot/etc/systemd/system
cp "$SCRIPT_DIR/bench-client/vstl-imaging-client.service" \
    config/includes.chroot/etc/systemd/system/

# Enable the service in the chroot at build time via a hook
mkdir -p config/hooks/normal
cat > config/hooks/normal/0001-enable-vstl.hook.chroot <<'EOF'
#!/bin/sh
set -e
echo "[VSTL] Enabling vstl-imaging-client.service..."
systemctl enable vstl-imaging-client.service
systemctl enable ssh
EOF
chmod +x config/hooks/normal/0001-enable-vstl.hook.chroot

# --- 6. Auto-login bench user ---
mkdir -p config/includes.chroot/etc/skel
mkdir -p config/includes.chroot/lib/systemd/system/getty@tty1.service.d
cat > config/includes.chroot/lib/systemd/system/getty@tty1.service.d/override.conf <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $LIVE_ISO_USER --noclear %I \$TERM
EOF

# Create the user with the configured password
cat > config/hooks/normal/0002-create-user.hook.chroot <<EOF
#!/bin/sh
set -e
echo "[VSTL] Creating bench user $LIVE_ISO_USER ..."
if ! id $LIVE_ISO_USER >/dev/null 2>&1; then
    useradd -m -s /bin/bash -G sudo $LIVE_ISO_USER
fi
echo "$LIVE_ISO_USER:$LIVE_ISO_PASS" | chpasswd
echo "$LIVE_ISO_USER ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers.d/vstl
chmod 440 /etc/sudoers.d/vstl
hostnamectl set-hostname $LIVE_ISO_HOSTNAME 2>/dev/null || echo "$LIVE_ISO_HOSTNAME" > /etc/hostname
EOF
chmod +x config/hooks/normal/0002-create-user.hook.chroot

# --- 7. Build! ---
log "Starting live-build (this takes 20-40 minutes)..."
log "Output is verbose; tail -F $LB_DIR/build.log to watch in another shell."
if ! lb build 2>&1 | tee build.log; then
    die "live-build failed. Last 30 lines:\n$(tail -30 build.log)"
fi

# --- 8. Move the ISO to the well-known output path ---
ISO_OUT="$BUILD_DIR/$LIVE_ISO_NAME"
if [[ -f "$LB_DIR/vstl-live-amd64.hybrid.iso" ]]; then
    mv "$LB_DIR/vstl-live-amd64.hybrid.iso" "$ISO_OUT"
elif [[ -f "$LB_DIR/live-image-amd64.hybrid.iso" ]]; then
    mv "$LB_DIR/live-image-amd64.hybrid.iso" "$ISO_OUT"
else
    die "Could not find the built ISO. Check $LB_DIR/ for *.iso files."
fi

ISO_SIZE=$(du -h "$ISO_OUT" | awk '{print $1}')
ISO_SHA=$(sha256sum "$ISO_OUT" | awk '{print $1}')

cat <<EOF

${GREEN}========================================================================
LIVE ISO BUILT
========================================================================${NC}

Path:    $ISO_OUT
Size:    $ISO_SIZE
SHA-256: $ISO_SHA

Bench credentials baked into the ISO:
  username: $LIVE_ISO_USER
  password: $LIVE_ISO_PASS
  hostname: $LIVE_ISO_HOSTNAME

Backend baked in:
  API base: $VSTL_API_BASE
  API key:  ${VSTL_API_KEY:0:12}…${VSTL_API_KEY: -4}  (truncated for display)
  FOG:      http://$SERVER_IP/fog

Next step: register this ISO with FOG so PXE benches receive it.
See  $SCRIPT_DIR/05_register_iso_with_fog.md  for the 5-min webUI walkthrough.

You can also test the ISO immediately by burning it to a USB stick:
    sudo dd if=$ISO_OUT of=/dev/sdX bs=4M status=progress oflag=sync

EOF
