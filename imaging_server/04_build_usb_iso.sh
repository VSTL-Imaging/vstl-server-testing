#!/usr/bin/env bash
# Build a USB-oriented VSTL hybrid ISO from the latest existing VSTL ISO.
#
# This is intentionally faster than rebuilding Clonezilla and all packages.
# It preserves the source ISO's BIOS/UEFI hybrid boot records, replaces the
# root filesystem with the current bench runtime, and verifies the result.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
. "$SCRIPT_DIR/tools/live_rootfs_permissions.sh"

BUILD_DIR="$SCRIPT_DIR/build"
SOURCE_ISO="${VSTL_USB_SOURCE_ISO:-$BUILD_DIR/vstl-live-amd64.iso}"
OUTPUT_ISO="${VSTL_USB_OUTPUT_ISO:-$BUILD_DIR/vstl-usb-live-amd64.iso}"
WORK_DIR="$BUILD_DIR/usb-build"
BENCH_DIR="$SCRIPT_DIR/bench-client"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }

[[ "${EUID:-$(id -u)}" -eq 0 ]] || die "Run as root (sudo)."
[[ -f "$SOURCE_ISO" ]] || die "Source ISO not found: $SOURCE_ISO"
[[ -f "$SCRIPT_DIR/.env" ]] || die "Missing $SCRIPT_DIR/.env"

for command in mount umount rsync unsquashfs mksquashfs xorriso sha256sum; do
    command -v "$command" >/dev/null 2>&1 || die "Missing dependency: $command"
done

RUNTIME_FILES=(
    vstl-imaging-client.sh
    vstl-bench-entry.sh
    vstl_network_setup.py
    vstl-imaging-tui.py
    vstl_hw_detect.py
    vstl_lock_audit.py
    vstl_qc_tests.py
    vstl_burn_stress.py
    vstl_secure_erase.py
    vstl_image_capture.py
    vstl_image_restore.py
)

rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR/rootfs" "$WORK_DIR/extract"
MOUNT_POINT="$(mktemp -d -t vstl-usb-iso-XXXX)"
cleanup() {
    umount "$MOUNT_POINT" 2>/dev/null || true
    rmdir "$MOUNT_POINT" 2>/dev/null || true
}
trap cleanup EXIT

log "Mounting source hybrid ISO..."
mount -o loop,ro "$SOURCE_ISO" "$MOUNT_POINT"
[[ -f "$MOUNT_POINT/live/filesystem.squashfs" ]] \
    || die "Source ISO has no /live/filesystem.squashfs"

log "Unpacking source root filesystem..."
unsquashfs -d "$WORK_DIR/rootfs" -no-progress \
    "$MOUNT_POINT/live/filesystem.squashfs" >/dev/null

log "Injecting current VSTL runtime..."
mkdir -p "$WORK_DIR/rootfs/opt/vstl"
for file in "${RUNTIME_FILES[@]}"; do
    [[ -f "$BENCH_DIR/$file" ]] || die "Missing runtime file: $BENCH_DIR/$file"
    install -m 0755 "$BENCH_DIR/$file" "$WORK_DIR/rootfs/opt/vstl/$file"
done
if [[ -d "$BENCH_DIR/sounds" ]]; then
    rm -rf "$WORK_DIR/rootfs/opt/vstl/sounds"
    cp -a "$BENCH_DIR/sounds" "$WORK_DIR/rootfs/opt/vstl/sounds"
fi

install -m 0600 "$SCRIPT_DIR/.env" "$WORK_DIR/rootfs/opt/vstl/config.env"
SERVER_IP="$(
    sed -n 's/^SERVER_IP=["'\'']\?\([^"'\'']*\)["'\'']\?$/\1/p' \
        "$SCRIPT_DIR/.env" | tail -1
)"
SERVER_IP="${SERVER_IP:-10.255.0.75}"

if ! grep -q '^VSTL_SERVER_IP=' "$WORK_DIR/rootfs/opt/vstl/config.env"; then
    printf '\nVSTL_SERVER_IP="%s"\n' "$SERVER_IP" \
        >>"$WORK_DIR/rootfs/opt/vstl/config.env"
fi
if ! grep -q '^VSTL_WIFI_PROMPT=' "$WORK_DIR/rootfs/opt/vstl/config.env"; then
    printf 'VSTL_WIFI_PROMPT=1\n' >>"$WORK_DIR/rootfs/opt/vstl/config.env"
fi
if ! grep -q '^FOG_SERVER=' "$WORK_DIR/rootfs/opt/vstl/config.env"; then
    printf 'FOG_SERVER="http://%s/fog"\n' "$SERVER_IP" \
        >>"$WORK_DIR/rootfs/opt/vstl/config.env"
fi

# The console launcher is already started by ocs_live_run in the boot menu.
# A second systemd launcher would create two TUI owners.
rm -f \
    "$WORK_DIR/rootfs/etc/systemd/system/multi-user.target.wants/vstl-imaging.service"

# Keep USB boot visually consistent with PXE: the bench entrypoint selects a
# 32x16 console font when present so the technician UI is roughly 2x larger.
mkdir -p "$WORK_DIR/rootfs/usr/share/consolefonts"
for font in \
    Lat15-TerminusBold32x16.psf.gz \
    Lat2-TerminusBold32x16.psf.gz \
    Uni3-TerminusBold32x16.psf.gz \
    Lat15-Terminus32x16.psf.gz \
    Lat2-Terminus32x16.psf.gz \
    Lat2-VGA32x16.psf.gz \
    Lat15-Terminus16.psf.gz \
    Lat2-Terminus16.psf.gz \
    Uni3-Terminus16.psf.gz \
    Lat15-Terminus20x10.psf.gz \
    Lat2-Terminus20x10.psf.gz; do
    if [[ -f "/usr/share/consolefonts/$font" ]]; then
        install -m 0644 "/usr/share/consolefonts/$font" \
            "$WORK_DIR/rootfs/usr/share/consolefonts/$font"
    fi
done

[[ -x "$WORK_DIR/rootfs/usr/bin/nmcli" ]] \
    || die "Source ISO lacks nmcli/NetworkManager. Run 04_build_live_iso_clonezilla.sh first."
[[ -x "$WORK_DIR/rootfs/usr/sbin/wpa_supplicant" ]] \
    || die "Source ISO lacks wpa_supplicant. Run 04_build_live_iso_clonezilla.sh first."
find "$WORK_DIR/rootfs/usr/lib/firmware" "$WORK_DIR/rootfs/lib/firmware" \
    -type f -name 'iwlwifi-*' -print -quit 2>/dev/null | grep -q . \
    || die "Source ISO lacks Intel iwlwifi firmware. Run 04_build_live_iso_clonezilla.sh first."

log "Rebuilding USB root filesystem..."
log "Repairing USB live rootfs ownership/modes..."
repair_live_rootfs_permissions "$WORK_DIR/rootfs"

rm -f "$WORK_DIR/filesystem.squashfs"
mksquashfs "$WORK_DIR/rootfs" "$WORK_DIR/filesystem.squashfs" \
    -comp xz -b 1048576 -Xbcj x86 -noappend -no-progress \
    || die "mksquashfs failed"

# Replace Clonezilla's visible boot menus with one immediate VSTL entry.
# UEFI loads /boot/grub/grub.cfg. Legacy BIOS loads one of the two Syslinux
# files below, depending on whether the image was written as ISO or DD mode.
BOOT_ARGS="boot=live union=overlay username=user config components quiet loglevel=3 panic=15 ocs_1_cpu_udev noswap edd=on nomodeset enforcing=0 noeject net.ifnames=0 ocs_live_run=/opt/vstl/vstl-bench-entry.sh ocs_live_batch=yes ocs_live_extra_param= ocs_lang=en_US.UTF-8 ocs_live_keymap=NONE keyboard-layouts=NONE locales=en_US.UTF-8 ocs_live_run_tty=/dev/tty1 noprompt"

mkdir -p "$WORK_DIR/boot/grub" "$WORK_DIR/syslinux"
cat >"$WORK_DIR/boot/grub/grub.cfg" <<EOF
set default=0
set timeout=0
set timeout_style=hidden

menuentry "VSTL 360 Bench Imaging" --id vstl-bench {
    search --no-floppy --set=root -f /live/vmlinuz
    linux /live/vmlinuz $BOOT_ARGS
    initrd /live/initrd.img
}
EOF

cat >"$WORK_DIR/syslinux/isolinux.cfg" <<EOF
DEFAULT vstl
PROMPT 0
TIMEOUT 1
NOESCAPE 1

LABEL vstl
  KERNEL /live/vmlinuz
  APPEND initrd=/live/initrd.img $BOOT_ARGS
EOF
cp "$WORK_DIR/syslinux/isolinux.cfg" "$WORK_DIR/syslinux/syslinux.cfg"

umount "$MOUNT_POINT"
rmdir "$MOUNT_POINT"
trap - EXIT

log "Replaying BIOS/UEFI hybrid boot records into USB ISO..."
rm -f "$OUTPUT_ISO"
xorriso \
    -indev "$SOURCE_ISO" \
    -outdev "$OUTPUT_ISO" \
    -boot_image any replay \
    -volid "VSTL_USB" \
    -update "$WORK_DIR/filesystem.squashfs" "/live/filesystem.squashfs" \
    -update "$WORK_DIR/boot/grub/grub.cfg" "/boot/grub/grub.cfg" \
    -update "$WORK_DIR/syslinux/isolinux.cfg" "/syslinux/isolinux.cfg" \
    -update "$WORK_DIR/syslinux/syslinux.cfg" "/syslinux/syslinux.cfg" \
    -commit \
    || die "xorriso hybrid ISO rebuild failed"

log "Verifying embedded VSTL network helper..."
VERIFY_DIR="$WORK_DIR/verify"
mkdir -p "$VERIFY_DIR"
xorriso -osirrox on -indev "$OUTPUT_ISO" \
    -extract /live/filesystem.squashfs "$VERIFY_DIR/filesystem.squashfs" \
    >/dev/null 2>&1 \
    || die "Could not extract the rebuilt squashfs"

SOURCE_SHA="$(sha256sum "$BENCH_DIR/vstl_network_setup.py" | awk '{print $1}')"
EMBEDDED_SHA="$(
    unsquashfs -cat "$VERIFY_DIR/filesystem.squashfs" \
        opt/vstl/vstl_network_setup.py 2>/dev/null | sha256sum | awk '{print $1}'
)"
[[ "$SOURCE_SHA" == "$EMBEDDED_SHA" ]] \
    || die "USB ISO contains a stale network helper"

EMBEDDED_QC="$VERIFY_DIR/vstl_qc_tests.py"
unsquashfs -cat "$VERIFY_DIR/filesystem.squashfs" \
    opt/vstl/vstl_qc_tests.py > "$EMBEDDED_QC" 2>/dev/null \
    || die "USB ISO is missing the embedded QC test runner"
for test_name in speaker microphone wireless ports; do
    grep -q "\"$test_name\"" "$EMBEDDED_QC" \
        || die "USB ISO is missing mandatory QC test: $test_name"
done

log "Verifying direct VSTL boot configuration..."
xorriso -osirrox on -indev "$OUTPUT_ISO" \
    -extract /boot/grub/grub.cfg "$VERIFY_DIR/grub.cfg" \
    -extract /syslinux/isolinux.cfg "$VERIFY_DIR/isolinux.cfg" \
    -extract /syslinux/syslinux.cfg "$VERIFY_DIR/syslinux.cfg" \
    >/dev/null 2>&1 \
    || die "Could not extract the rebuilt boot configuration"

grep -q 'set timeout=0' "$VERIFY_DIR/grub.cfg" \
    || die "UEFI boot configuration is not immediate"
grep -q 'menuentry "VSTL 360 Bench Imaging"' "$VERIFY_DIR/grub.cfg" \
    || die "UEFI VSTL boot entry is missing"
grep -q 'ocs_live_run=/opt/vstl/vstl-bench-entry.sh' "$VERIFY_DIR/grub.cfg" \
    || die "UEFI VSTL entry does not start the bench workflow"
grep -q '^DEFAULT vstl$' "$VERIFY_DIR/isolinux.cfg" \
    || die "BIOS VSTL boot entry is not the default"
grep -q '^PROMPT 0$' "$VERIFY_DIR/isolinux.cfg" \
    || die "BIOS boot configuration is still interactive"
if grep -qi 'Clonezilla live' \
    "$VERIFY_DIR/grub.cfg" "$VERIFY_DIR/isolinux.cfg" "$VERIFY_DIR/syslinux.cfg"; then
    die "Clonezilla boot menu remains in the USB ISO"
fi

BOOT_REPORT="$WORK_DIR/boot-report.txt"
xorriso -indev "$OUTPUT_ISO" -report_el_torito plain >"$BOOT_REPORT" 2>&1 || true
grep -qi 'UEFI' "$BOOT_REPORT" || grep -qi 'EFI' "$BOOT_REPORT" \
    || die "UEFI boot entry was not preserved"
grep -qi 'BIOS' "$BOOT_REPORT" \
    || die "BIOS boot entry was not preserved"

ISO_SHA="$(sha256sum "$OUTPUT_ISO" | awk '{print $1}')"
printf '%s  %s\n' "$ISO_SHA" "$(basename "$OUTPUT_ISO")" \
    >"${OUTPUT_ISO}.sha256"

cat <<EOF

VSTL USB ISO built successfully
Path:    $OUTPUT_ISO
Size:    $(du -h "$OUTPUT_ISO" | awk '{print $1}')
SHA256:  $ISO_SHA
Server:  $SERVER_IP
Network: wired/USB Ethernet first, interactive Wi-Fi fallback

EOF
