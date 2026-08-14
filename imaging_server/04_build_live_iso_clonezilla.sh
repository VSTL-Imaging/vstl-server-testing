#!/usr/bin/env bash
#
# VSTL 360 — Bootable Imaging Live ISO (Clonezilla-based)
# ========================================================
#
# Builds a bootable ISO that auto-runs the VSTL bench client on every boot.
# Based on official Clonezilla Live (purpose-built imaging distro that ships
# with nwipe, smartmontools, fio, stress-ng, dmidecode, ethtool, etc. already
# included).
#
# This replaces `04_build_live_iso.sh` which relied on Ubuntu's broken
# `live-build 3.0~a57` tool. Clonezilla Live is prebuilt by upstream, we just
# inject our bench client + systemd service + config.
#
# Output: build/vstl-live-amd64.iso (~450 MB)
#
# Usage:
#   sudo ./04_build_live_iso_clonezilla.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
. "$SCRIPT_DIR/tools/live_rootfs_permissions.sh"

BUILD_DIR="$SCRIPT_DIR/build"
WORK_DIR="$BUILD_DIR/cz"
ISO_OUT="$BUILD_DIR/vstl-live-amd64.iso"
BUILD_TS="${VSTL_BUILD_TS:-$(date +%Y%m%d_%H%M%S)}"
GIT_SHA="$(git -C "$SCRIPT_DIR" rev-parse --short=12 HEAD 2>/dev/null || true)"
GIT_BRANCH="$(git -C "$SCRIPT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
BUILD_BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BUILD_VERSION="${VSTL_BUILD_VERSION:-VSTL-iso-${BUILD_TS}${GIT_SHA:+-$GIT_SHA}}"

# Official Clonezilla Live AMD64 — Debian Stable base, x86_64 arch
# Version is pinned so builds are reproducible; bump manually to upgrade.
CZ_VERSION="3.2.2-15"
CZ_ISO_URL="https://sourceforge.net/projects/clonezilla/files/clonezilla_live_stable/${CZ_VERSION}/clonezilla-live-${CZ_VERSION}-amd64.iso/download"
CZ_ISO_SHA256="skip"   # Keep "skip" to accept any checksum; set actual hash here to enforce verification
CZ_ISO_CACHED="$BUILD_DIR/clonezilla-live-${CZ_VERSION}-amd64.iso"

log() { echo "[$(date +%H:%M:%S)] $*"; }
die() { echo "FATAL: $*" >&2; exit 1; }
json_escape() {
    local value="${1:-}"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//$'\n'/ }"
    value="${value//$'\r'/ }"
    printf '%s' "$value"
}

write_build_info_json() {
    local out="$1"
    {
        printf '{\n'
        printf '  "build_version": "%s",\n' "$(json_escape "$BUILD_VERSION")"
        printf '  "built_at": "%s",\n' "$(json_escape "$BUILD_BUILT_AT")"
        printf '  "server_role": "iso",\n'
        printf '  "git_sha": "%s",\n' "$(json_escape "$GIT_SHA")"
        printf '  "git_branch": "%s",\n' "$(json_escape "$GIT_BRANCH")"
        printf '  "source_root": "%s"\n' "$(json_escape "$SCRIPT_DIR")"
        printf '}\n'
    } > "$out"
}

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    die "This script must be run as root (use sudo)."
fi

# --- 1. Dependencies -----------------------------------------------------------
log "Installing minimal dependencies (wget, xorriso, squashfs-tools, rsync)..."
apt-get update -qq
apt-get install -y -qq wget xorriso squashfs-tools rsync isolinux syslinux-utils \
    || die "Dependency install failed"

# --- 2. Download Clonezilla Live base ISO --------------------------------------
mkdir -p "$BUILD_DIR"
if [[ ! -f "$CZ_ISO_CACHED" ]]; then
    log "Downloading Clonezilla Live ${CZ_VERSION} (~450 MB) ..."
    wget --quiet --show-progress -O "$CZ_ISO_CACHED" "$CZ_ISO_URL" \
        || die "Failed to download Clonezilla Live from $CZ_ISO_URL"
else
    log "Using cached Clonezilla ISO at $CZ_ISO_CACHED"
fi

if [[ "$CZ_ISO_SHA256" != "skip" ]]; then
    ACTUAL_SHA=$(sha256sum "$CZ_ISO_CACHED" | awk '{print $1}')
    if [[ "$ACTUAL_SHA" != "$CZ_ISO_SHA256" ]]; then
        die "Clonezilla ISO checksum mismatch. Expected $CZ_ISO_SHA256, got $ACTUAL_SHA"
    fi
fi

# --- 3. Extract the ISO --------------------------------------------------------
log "Extracting Clonezilla base ISO to $WORK_DIR ..."
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR/extract"
MOUNT_POINT=$(mktemp -d)
mount -o loop,ro "$CZ_ISO_CACHED" "$MOUNT_POINT"
rsync -aH "$MOUNT_POINT/" "$WORK_DIR/extract/"
umount "$MOUNT_POINT"
rmdir "$MOUNT_POINT"

# Make extracted tree writable
chmod -R u+w "$WORK_DIR/extract"

# --- 4. Unpack the Clonezilla root squashfs so we can inject our files --------
log "Unpacking filesystem.squashfs (Clonezilla's root) ..."
SQUASH_IN="$WORK_DIR/extract/live/filesystem.squashfs"
[[ -f "$SQUASH_IN" ]] || die "Expected $SQUASH_IN but not found — Clonezilla layout changed?"
unsquashfs -d "$WORK_DIR/rootfs" -no-progress "$SQUASH_IN"

# --- 5. Inject the VSTL bench client + config + systemd service --------------
log "Injecting VSTL bench client ..."
mkdir -p "$WORK_DIR/rootfs/opt/vstl"
log "Bench build version: $BUILD_VERSION"

# Phase-1 entrypoint + Python TUI + hardware detection module. The legacy
# shell client is retained as a fallback (vstl-bench-entry.sh execs it if the
# TUI is missing) so any existing benches still work after a partial upgrade.
BENCH_DIR="$SCRIPT_DIR/bench-client"
for f in \
    vstl-imaging-client.sh \
    vstl-bench-entry.sh \
    vstl_network_setup.py \
    vstl-imaging-tui.py \
    vstl_hw_detect.py \
    vstl_lock_audit.py \
    vstl_qc_tests.py \
    vstl_burn_stress.py \
    vstl_secure_erase.py \
    vstl_image_capture.py \
    vstl_image_restore.py; do
    src="$BENCH_DIR/$f"
    [[ -f "$src" ]] || die "Bench client file not found: $src"
    install -m 0755 "$src" "$WORK_DIR/rootfs/opt/vstl/$f"
done
if [[ -d "$BENCH_DIR/sounds" ]]; then
    rm -rf "$WORK_DIR/rootfs/opt/vstl/sounds"
    cp -a "$BENCH_DIR/sounds" "$WORK_DIR/rootfs/opt/vstl/sounds"
fi
write_build_info_json "$WORK_DIR/rootfs/opt/vstl/vstl_build_info.json"

# .env (with the API key) sits at the imaging_server root. It was created by
# the operator when they chmod 600'd it. If missing, copy the example so the
# script is still bootable (admin can edit /opt/vstl/config.env from recovery).
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    install -m 0600 "$SCRIPT_DIR/.env" "$WORK_DIR/rootfs/opt/vstl/config.env"
else
    install -m 0600 "$SCRIPT_DIR/.env.example" "$WORK_DIR/rootfs/opt/vstl/config.env" \
        || die "Neither .env nor .env.example found — run with .env present"
    log "  WARNING: using .env.example — edit /opt/vstl/config.env before booting"
fi

cat > "$WORK_DIR/rootfs/etc/systemd/system/vstl-imaging.service" <<'UNIT'
[Unit]
Description=VSTL 360 Imaging Bench Client
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/opt/vstl/config.env
# Phase-1 entrypoint: bootstraps network → launches Python TUI → falls back
# to the legacy shell client if the TUI is missing.
ExecStart=/opt/vstl/vstl-bench-entry.sh
StandardOutput=journal+console
StandardError=journal+console
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

# Do not enable the systemd unit by default. PXE/ISO boot menus already launch
# /opt/vstl/vstl-bench-entry.sh through ocs_live_run; enabling this service as
# well creates a duplicate TUI owner and can drop the visible console to
# user@debian after the first VSTL screen appears.
mkdir -p "$WORK_DIR/rootfs/etc/systemd/system/multi-user.target.wants"
rm -f "$WORK_DIR/rootfs/etc/systemd/system/multi-user.target.wants/vstl-imaging.service"

# --- 5a. Install runtime dependencies INTO the rootfs chroot -------------------
# 2026-05-11 Phase 2 v2: the upgraded TUI needs evdev (raw keyboard capture),
# v4l-utils (camera capture/direct framebuffer preview), fswebcam+fbi fallback,
# hdparm (storage throughput test), alsa-utils (speaker+mic), Intel SOF audio
# firmware, evtest (headset jack switches), libdrm-tests/modetest, and udev
# (USB port monitoring). Clonezilla's base squashfs has *some* of these but
# not all, so we install them via chroot+apt against the unpacked rootfs.
log "Installing Phase 2 runtime dependencies into rootfs chroot ..."

cleanup_chroot_mounts() {
    umount -l "$WORK_DIR/rootfs/dev/pts" 2>/dev/null || true
    umount -l "$WORK_DIR/rootfs/dev" 2>/dev/null || true
    umount -l "$WORK_DIR/rootfs/sys" 2>/dev/null || true
    umount -l "$WORK_DIR/rootfs/proc" 2>/dev/null || true
    umount -l "$WORK_DIR/rootfs/etc/resolv.conf" 2>/dev/null || true
}
trap cleanup_chroot_mounts EXIT

# Bind-mount the host's resolv.conf so apt can reach the Debian mirrors
mount --bind /etc/resolv.conf "$WORK_DIR/rootfs/etc/resolv.conf" 2>/dev/null || \
    cp -L /etc/resolv.conf "$WORK_DIR/rootfs/etc/resolv.conf"
mount --bind /proc "$WORK_DIR/rootfs/proc"
mount --bind /sys  "$WORK_DIR/rootfs/sys"
mount --bind /dev  "$WORK_DIR/rootfs/dev"
mount --bind /dev/pts "$WORK_DIR/rootfs/dev/pts" 2>/dev/null || true

set +e
chroot "$WORK_DIR/rootfs" /bin/bash -c '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    export LC_ALL=C
    # 2026-05-12 hotfix #2: prevent systemd/udev postinst scripts from
    # trying to start services inside the chroot (which deadlocks dpkg
    # because the chroot has no PID 1). Standard Debian chroot practice:
    # /usr/sbin/policy-rc.d returning 101 tells invoke-rc.d "deny all",
    # and a stub start-stop-daemon prevents systemd postinst from blocking
    # on systemctl daemon-reexec.
    echo "#!/bin/sh" > /usr/sbin/policy-rc.d
    echo "exit 101" >> /usr/sbin/policy-rc.d
    chmod +x /usr/sbin/policy-rc.d
    if [ -x /sbin/start-stop-daemon ] && [ ! -f /sbin/start-stop-daemon.REAL ]; then
        mv /sbin/start-stop-daemon /sbin/start-stop-daemon.REAL
        echo "#!/bin/sh" > /sbin/start-stop-daemon
        echo "exit 0" >> /sbin/start-stop-daemon
        chmod +x /sbin/start-stop-daemon
    fi
    # 2026-05-12 hotfix: DRBL repo (shipped inside Clonezilla rootfs) is
    # signed with an OpenPGP key that uses SHA1 binding signatures.
    # SHA1 has been banned by Sequoia/sqv (the apt verifier) since
    # 2026-02-01, so `apt-get update` rejects DRBL with:
    #   "Sub-process /usr/bin/sqv returned an error code (1)"
    #   "Signing key ... is not bound: SHA1 is not considered secure"
    # Our Phase 2 runtime deps all come from Debian main — DRBL is not
    # required. Disable DRBL sources for the duration of this apt run,
    # then restore them so the live ISO is unchanged for any downstream
    # Clonezilla-internal needs.
    for f in /etc/apt/sources.list.d/drbl*.list /etc/apt/sources.list.d/*drbl*; do
        [ -e "$f" ] && mv "$f" "$f.disabled"
    done
    # Debian moved hardware firmware into the non-free-firmware component.
    # The Clonezilla source only enables main+contrib, which leaves modern Intel
    # SOF audio controllers present in PCI but without speaker/mic devices.
    printf "%s\n" \
        "deb http://deb.debian.org/debian trixie main contrib non-free-firmware" \
        "deb-src http://deb.debian.org/debian trixie main contrib non-free-firmware" \
        > /etc/apt/sources.list
    printf "Acquire::ForceIPv4 \"true\";\n" > /etc/apt/apt.conf.d/99vstl-force-ipv4
    sed -i -E "/^[[:space:]]*deb(-src)?[[:space:]].*debian/ {
        /non-free-firmware/! s/[[:space:]]*$/ non-free-firmware/
    }" /etc/apt/sources.list /etc/apt/sources.list.d/*.list 2>/dev/null || true
    apt-get update -qq
    apt-get install -y --no-install-recommends -qq \
        -o Dpkg::Options::="--force-confdef" \
        -o Dpkg::Options::="--force-confold" \
        python3-evdev ffmpeg fswebcam fbi v4l-utils hdparm alsa-utils udev ethtool \
        kbd console-setup-linux fonts-terminus \
        network-manager wpasupplicant rfkill iw isc-dhcp-client \
        firmware-sof-signed firmware-intel-sound firmware-iwlwifi wireless-tools \
        evtest libdrm-tests fprintd libfprint-2-2 smartmontools usbutils pciutils
    apt-get install -y --no-install-recommends -qq \
        libpam-fprintd libfprint-2-tod1 || true
    apt-get clean
    rm -rf /var/lib/apt/lists/*
    # Restore start-stop-daemon + remove policy-rc.d so the live ISO
    # behaves normally at runtime.
    rm -f /usr/sbin/policy-rc.d
    if [ -f /sbin/start-stop-daemon.REAL ]; then
        mv /sbin/start-stop-daemon.REAL /sbin/start-stop-daemon
    fi
    for f in /etc/apt/sources.list.d/drbl*.list.disabled /etc/apt/sources.list.d/*drbl*.disabled; do
        [ -e "$f" ] && mv "$f" "${f%.disabled}"
    done
    true
' || die "Required live runtime or firmware packages could not be installed"
set -e

# Clean up chroot mounts (reverse order)
cleanup_chroot_mounts
trap - EXIT

# --- 5b. Patch boot-loader menus to skip Clonezilla's interactive wizard ------
# Clonezilla's `ocs_live_run` kernel param normally points at the interactive
# "ocs-live-general" wizard (Language → Keyboard → Start Clonezilla prompts).
# We override it across every boot entry — BIOS (isolinux) + UEFI (grub) — so
# the live system runs OUR bench client directly with no operator interaction.
#
# Params we inject/replace on every kernel cmdline that contains `boot=live`:
#   ocs_live_run="/opt/vstl/vstl-bench-entry.sh"     -> Phase-1 entrypoint (TUI + network)
#   ocs_live_batch="yes"                              -> non-interactive
#   ocs_lang="en_US.UTF-8"                            -> skips Language prompt
#   ocs_live_keymap="NONE"                            -> skips Keyboard prompt
log "Patching boot-loader menus to auto-run VSTL imaging client ..."

python3 - "$WORK_DIR/extract" <<'PYTHON'
import re, sys, pathlib

root = pathlib.Path(sys.argv[1])

# Every Clonezilla cfg file we might need to touch — globbing rather than
# hard-coding because the layout shifts slightly between Clonezilla releases
# (e.g. 3.2.x uses syslinux/, older builds use isolinux/).
cfg_paths = []
for pattern in ("syslinux/*.cfg", "isolinux/*.cfg",
                "boot/grub/*.cfg", "boot/grub/**/*.cfg",
                "EFI/**/*.cfg"):
    cfg_paths.extend(root.glob(pattern))

# 4 params we want to enforce. Order matters for the "append if missing" pass.
PARAMS = [
    ('ocs_live_run',    '"/opt/vstl/vstl-bench-entry.sh"'),
    ('ocs_live_batch',  '"yes"'),
    ('ocs_lang',        '"en_US.UTF-8"'),
    ('ocs_live_keymap', '"NONE"'),
]

# Match either an isolinux APPEND line or a grub `linux /live/vmlinuz ...`
# line that carries the `boot=live` token (which is the unmistakable marker
# of a live-boot entry, present in every Clonezilla menu entry).
LIVE_LINE_RE = re.compile(r'^(\s*)(append|APPEND|linux|linux16|linuxefi)\b.*\bboot=live\b',
                          re.IGNORECASE)

def patch_line(line):
    new_line = line
    for key, value in PARAMS:
        # Replace any existing `key="..."` or `key=bareword` with our value
        pat = re.compile(rf'\b{re.escape(key)}=("[^"]*"|\S+)')
        if pat.search(new_line):
            new_line = pat.sub(f'{key}={value}', new_line)
        else:
            # Param missing → append before the trailing newline
            new_line = new_line.rstrip('\n') + f' {key}={value}\n'
    return new_line

patched_files = 0
patched_lines = 0
matched_lines = 0
for p in sorted(set(cfg_paths)):
    try:
        text = p.read_text(errors='replace')
    except Exception as e:
        print(f"  skip {p}: {e}")
        continue

    out, file_hits = [], 0
    for ln in text.splitlines(keepends=True):
        if LIVE_LINE_RE.match(ln):
            matched_lines += 1
            new_ln = patch_line(ln)
            if new_ln != ln:
                file_hits += 1
            out.append(new_ln)
        else:
            out.append(ln)

    if file_hits:
        p.write_text(''.join(out))
        patched_files += 1
        patched_lines += file_hits
        print(f"  patched {file_hits} entries in {p.relative_to(root)}")

# Hard-fail if we found NO live boot entries at all (Clonezilla layout shifted).
# A re-run that finds entries but doesn't need to change them (already patched)
# is a benign no-op and should succeed.
if matched_lines == 0:
    print("ERROR: no Clonezilla live boot entries (boot=live) were found — "
          "menu layout changed?", file=sys.stderr)
    sys.exit(1)

print(f"OK — patched {patched_lines}/{matched_lines} live entries "
      f"across {patched_files} cfg files")
PYTHON

# Quick sanity dump so the operator can eyeball one entry post-patch.
# `grep | head -3` triggers SIGPIPE on grep (head closes the pipe early) which
# under `set -euo pipefail` would silently abort the script before mksquashfs.
# `|| true` neutralises that so the build always proceeds to step 6.
log "Sanity check — patched boot entry preview:"
grep -RIh --include='*.cfg' 'ocs_live_run=' "$WORK_DIR/extract/syslinux" \
    "$WORK_DIR/extract/boot" "$WORK_DIR/extract/EFI" 2>/dev/null \
    | head -3 | sed 's/^/    /' || true

# --- 6. Re-compress the root filesystem ----------------------------------------
log "Re-compressing root filesystem into squashfs (~1–2 min) ..."
log "Repairing live rootfs ownership/modes ..."
repair_live_rootfs_permissions "$WORK_DIR/rootfs"

rm -f "$SQUASH_IN"
mksquashfs "$WORK_DIR/rootfs" "$SQUASH_IN" \
    -comp xz -b 1048576 -Xbcj x86 -noappend -no-progress \
    || die "mksquashfs failed"

# Update size file if Clonezilla's ISO has one (some releases do, some don't)
SIZE_FILE="$WORK_DIR/extract/live/filesystem.size"
if [[ -f "$SIZE_FILE" ]]; then
    du -sxB1 "$WORK_DIR/rootfs" | awk '{print $1}' > "$SIZE_FILE"
fi

# Clean up the unpacked rootfs (saves disk)
rm -rf "$WORK_DIR/rootfs"

# --- 7. Re-build the ISO -------------------------------------------------------
log "Re-assembling the hybrid ISO with xorriso (clone-from-source mode) ..."
rm -f "$ISO_OUT"

# CLONE mode: xorriso boots _from_ the original Clonezilla ISO, then we
# overwrite just the files we changed. This preserves Clonezilla's exact
# hybrid BIOS + UEFI boot structure (isolinux path, grub efi.img location,
# MBR signatures). Our previous explicit -eltorito-boot path was wrong and
# triggered "isolinux.bin missing or corrupt" on legacy-BIOS boot.
#
# We push these trees back into the ISO:
#   1. /live/filesystem.squashfs (injected bench client + service + config)
#   2. /syslinux                 (BIOS menus patched with ocs_live_run override)
#   3. /boot                     (UEFI grub menus patched with same override)
#   4. /EFI                      (UEFI bootloader-side menus, when present)

# Build the -update_r argument list dynamically — only include paths that
# actually exist in the extracted tree (Clonezilla layout shifts between releases).
XORRISO_UPDATES=( -update_r "$WORK_DIR/extract/live/filesystem.squashfs" "/live/filesystem.squashfs" )
for sub in syslinux isolinux boot EFI; do
    if [[ -d "$WORK_DIR/extract/$sub" ]]; then
        XORRISO_UPDATES+=( -update_r "$WORK_DIR/extract/$sub" "/$sub" )
        log "  will update /$sub in output ISO"
    fi
done

xorriso \
    -indev "$CZ_ISO_CACHED" \
    -outdev "$ISO_OUT" \
    -boot_image any replay \
    -volid "VSTL_LIVE" \
    "${XORRISO_UPDATES[@]}" \
    -commit \
    || die "xorriso clone+update failed"

# Update filesystem.size too (optional — some releases have this sibling file)
if [[ -f "$WORK_DIR/extract/live/filesystem.size" ]]; then
    xorriso \
        -indev "$ISO_OUT" \
        -outdev "$ISO_OUT" \
        -boot_image any replay \
        -update "$WORK_DIR/extract/live/filesystem.size" "/live/filesystem.size" \
        -commit \
        || true
fi

# --- 8. Report -----------------------------------------------------------------
SIZE_H=$(du -h "$ISO_OUT" | awk '{print $1}')
SHA256=$(sha256sum "$ISO_OUT" | awk '{print $1}')

cat <<BANNER

╔══════════════════════════════════════════════════════════════════════════╗
║                    ✓  LIVE ISO BUILT SUCCESSFULLY                        ║
╠══════════════════════════════════════════════════════════════════════════╣
║ Path    : $ISO_OUT
║ Size    : $SIZE_H
║ SHA-256 : $SHA256
║ Base    : Clonezilla Live $CZ_VERSION (Debian stable)
╚══════════════════════════════════════════════════════════════════════════╝

Next steps:
  1. Test on a bench laptop with:
     sudo dd if=$ISO_OUT of=/dev/sdX bs=4M status=progress oflag=sync
     (replace /dev/sdX with your USB device)
  2. Or register it with FOG (see 05_register_iso_with_fog.md).
  3. The VSTL bench client will auto-run on boot and POST to your API.

BANNER
