#!/usr/bin/env bash
# =============================================================================
# 06_setup_pxe_netboot.sh — Publish VSTL Live as a direct PXE boot target
# =============================================================================
# This is the PRODUCTION-grade way to PXE-boot VSTL Live — Clonezilla's own
# "Live as PXE" pattern. We do NOT use memdisk (which breaks on UEFI). Instead:
#
#   1. Extract /live/vmlinuz, /live/initrd.img, /live/filesystem.squashfs from
#      the already-built vstl-live-amd64.iso.
#   2. Drop them under FOG's Apache web root at /var/www/html/vstl-pxe/ so
#      bench laptops can HTTP-fetch them over FOG's built-in port 80.
#   3. Generate a small iPXE boot script (boot.ipxe) embedding our
#      ocs_live_run/ocs_live_batch kernel params so the VSTL client auto-runs.
#   4. Also copy the script to /tftpboot/vstl.ipxe for FOG menu chain-booting.
#
# After this, you add ONE menu entry in the FOG webUI (doc 06_pxe_netboot.md)
# and every bench laptop booted with F12 → PXE lands in VSTL imaging in ~15 sec.
#
# Re-run this script after each rebuild of the ISO — it's idempotent.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
. "$SCRIPT_DIR/tools/live_rootfs_permissions.sh"

ISO="$SCRIPT_DIR/build/vstl-live-amd64.iso"
PXE_ROOT="/var/www/html/vstl-pxe"
TFTP_ROOT="/tftpboot"
E1000E_OVERRIDE="$SCRIPT_DIR/bench-client/kmods/6.12.32-amd64/e1000e.ko.xz"
E1000E_OVERRIDE_SHA256="214b2285026f16ece07e568d0d589fff298b3bedb1db801900c7fbb9b7fa36bd"

# Resolve the imaging-server IP the same way our other scripts do: prefer .env,
# fall back to the first routable IPv4 on this host. We bake the IP into the
# iPXE script so bench laptops can reach the kernel/initrd/squashfs over HTTP.
ENV_SERVER_IP="${SERVER_IP:-}"
ENV_PXE_SERVER_IP="${VSTL_PXE_SERVER_IP:-}"
[[ -f "$SCRIPT_DIR/.env" ]] && source "$SCRIPT_DIR/.env"
[[ -n "$ENV_SERVER_IP" ]] && SERVER_IP="$ENV_SERVER_IP"
[[ -n "$ENV_PXE_SERVER_IP" ]] && VSTL_PXE_SERVER_IP="$ENV_PXE_SERVER_IP"
SERVER_IP="${VSTL_PXE_SERVER_IP:-${SERVER_IP:-$(ip -4 -o addr show scope global 2>/dev/null \
    | awk 'NR==1 {print $4}' | cut -d/ -f1)}}"
SERVER_IP="${SERVER_IP:-10.255.0.75}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn() { echo -e "${YELLOW}[$(date +%H:%M:%S)] WARN:${NC} $*"; }
die()  { echo -e "${RED}[$(date +%H:%M:%S)] FATAL:${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (use sudo)."
[[ -f "$ISO"   ]] || die "ISO not found at $ISO. Build it first: sudo ./04_build_live_iso_clonezilla.sh"
[[ -f "$E1000E_OVERRIDE" ]] || die "Missing guarded Intel I219-LM4 recovery module: $E1000E_OVERRIDE"
[[ "$(sha256sum "$E1000E_OVERRIDE" | awk '{print $1}')" == "$E1000E_OVERRIDE_SHA256" ]] \
    || die "Intel I219-LM4 recovery module failed SHA256 verification"

# --- 1. Prerequisites -------------------------------------------------------
for cmd in mount umount rsync install unsquashfs mksquashfs sha256sum; do
    command -v "$cmd" >/dev/null || die "Missing dep: $cmd"
done

# Apache is provided by FOG's installer. If it's missing, FOG isn't set up.
if ! systemctl is-active --quiet apache2 && ! systemctl is-active --quiet httpd; then
    warn "apache2 is not running. FOG installs Apache — did you run 01_install_fog.sh?"
    warn "Continuing anyway; files will still be placed under $PXE_ROOT."
fi

if [[ ! -d "$TFTP_ROOT" ]]; then
    warn "$TFTP_ROOT doesn't exist — tftpd-hpa may not be installed."
    mkdir -p "$TFTP_ROOT"
fi

# --- 2. Extract vmlinuz / initrd / squashfs from the ISO --------------------
log "Publishing VSTL Live PXE assets to $PXE_ROOT (server IP = $SERVER_IP)..."

MOUNT_POINT=$(mktemp -d -t vstl-pxe-XXXX)
mount -o loop,ro "$ISO" "$MOUNT_POINT" || { rmdir "$MOUNT_POINT"; die "Failed to loop-mount $ISO"; }
trap 'umount "$MOUNT_POINT" 2>/dev/null || true; rmdir "$MOUNT_POINT" 2>/dev/null || true' EXIT

mkdir -p "$PXE_ROOT"

# Copy each asset. mksquashfs produces new inode numbers each build so we
# always overwrite — rsync --inplace keeps the file open so Apache can still
# serve an in-progress fetch if a bench happened to PXE boot during a rebuild.
for f in vmlinuz initrd.img filesystem.squashfs; do
    src="$MOUNT_POINT/live/$f"
    dst="$PXE_ROOT/$f"
    [[ -f "$src" ]] || die "Expected $src inside ISO — layout changed?"
    log "  $f ($(du -h "$src" | awk '{print $1}'))"
    rsync -a --inplace "$src" "$dst"
done

umount "$MOUNT_POINT"
rmdir  "$MOUNT_POINT"
trap - EXIT

# The ISO may be older than the currently installed bench-client source. A
# plain asset copy would then silently put stale TUI/entry scripts back into
# production. That exact mismatch makes the main-menu arrow key crash the TUI
# and exposes Clonezilla's blue final-action screen. Re-inject the current
# runtime files into the copied squashfs before publishing it.
BENCH_DIR="$SCRIPT_DIR/bench-client"
BENCH_RUNTIME_FILES=(
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

PATCH_ROOT=$(mktemp -d -t vstl-pxe-rootfs-XXXX)
PATCHED_SQUASHFS="$PXE_ROOT/filesystem.squashfs.new"
cleanup_patch_root() {
    rm -rf "$PATCH_ROOT"
    rm -f "$PATCHED_SQUASHFS"
}
trap cleanup_patch_root EXIT

log "Injecting current bench-client into PXE filesystem.squashfs ..."
unsquashfs -d "$PATCH_ROOT/rootfs" -no-progress "$PXE_ROOT/filesystem.squashfs" >/dev/null \
    || die "Could not unpack PXE filesystem.squashfs"
mkdir -p "$PATCH_ROOT/rootfs/opt/vstl"
for f in "${BENCH_RUNTIME_FILES[@]}"; do
    src="$BENCH_DIR/$f"
    [[ -f "$src" ]] || die "Bench client file not found: $src"
    install -m 0755 "$src" "$PATCH_ROOT/rootfs/opt/vstl/$f"
done

rm -f "$PATCHED_SQUASHFS"
log "Repairing PXE live rootfs ownership/modes ..."
repair_live_rootfs_permissions "$PATCH_ROOT/rootfs"

mksquashfs "$PATCH_ROOT/rootfs" "$PATCHED_SQUASHFS" \
    -comp xz -b 1048576 -Xbcj x86 -noappend -no-progress \
    || die "Could not rebuild PXE filesystem.squashfs"
mv "$PATCHED_SQUASHFS" "$PXE_ROOT/filesystem.squashfs"

for f in vstl-imaging-tui.py vstl-bench-entry.sh; do
    source_sha=$(sha256sum "$BENCH_DIR/$f" | awk '{print $1}')
    embedded_sha=$(unsquashfs -cat "$PXE_ROOT/filesystem.squashfs" "opt/vstl/$f" 2>/dev/null \
        | sha256sum | awk '{print $1}')
    [[ "$embedded_sha" == "$source_sha" ]] \
        || die "PXE squashfs contains stale $f after rebuild"
    log "  verified embedded $f ($embedded_sha)"
done

cleanup_patch_root
trap - EXIT

# Apache / FOG user must be able to read the files.
chown -R root:root "$PXE_ROOT"
chmod -R a+r "$PXE_ROOT"

# 2026-05-13 Phase-2 v3 hard guard — catch the "Initramfs unpacking failed:
# invalid magic" kernel panic class of issue at INSTALL time instead of at
# bench-laptop boot time. Validates that:
#   • initrd.img has a recognized compression magic byte sequence at offset 0
#     (gzip 1f8b / xz fd377a / zstd 28b52ffd / lzma 5d / lz4 04224d18)
#   • vmlinuz starts with a valid Linux bzImage / EFI header
#   • Apache HTTP delivery on localhost returns the SAME bytes (catches
#     truncation, captive-portal HTML, HTTP-401 / 301-to-HTTPS, mod_cache
#     stale entries, etc.)
log "Sanity-checking PXE artifacts ..."

# 2026-05-14 Phase-2 v3.3 — Dell Latitude 5500 IPv4 PXE compatibility fix
# (founder confirmed on 10.255.0.75 / Debian 6.12.32 kernel after
# 06_setup_pxe_netboot.sh shipped the split-initrd profile in v3.2):
# even though iPXE successfully fetches both stages and the kernel
# recognises the dual `initrd` directives, the on-disk concatenation of
# `cpio_newc + zero_pad + xz_blob` STILL triggers
#     Initramfs unpacking failed: invalid magic at start of compressed archive
#     Kernel panic - not syncing: VFS: Unable to mount root fs on
# on this BIOS/firmware combination.
#
# The empirically-stable layout is a SINGLE clean gzip-compressed cpio:
# decompress stage2, concatenate raw_cpio_1 + raw_cpio_2, gzip-compress
# the whole thing, ship it as `initrd.img` with one `initrd` directive.
# This is exactly the hotfix the operator was applying by hand after
# every INSTALL.sh run — now baked into the installer so the next
# `INSTALL.sh --pxe-only` produces a Dell-safe single-archive initrd.
#
# Override knobs:
#   VSTL_PXE_INITRD_MODE=merged  (default — publish both gzip and raw CPIO)
#   VSTL_PXE_INITRD_MODE=split   (legacy v3.2 dual-initrd path; keep as
#                                  fallback for stacks that prefer it)
INITRD_MODE="${VSTL_PXE_INITRD_MODE:-merged}"
# Production default: raw CPIO handoff. Keep the iPXE handoff main-compatible:
# the PXE firmware path that fetched boot.ipxe is net0 in the working .45/.75
# flow. USB-C recovery is handled later inside live-boot after Linux drivers
# load; doing extra iPXE NIC probing before kernel handoff regressed Type-C
# adapters by stopping before the Linux ipconfig/filesystem.squashfs request.
PXE_BOOT_STYLE="${VSTL_PXE_BOOT_STYLE:-classic-raw-cpio}"

INITRD_PROFILE=$(PXE_ROOT="$PXE_ROOT" \
    VSTL_PXE_INITRD_MODE="$INITRD_MODE" \
    VSTL_E1000E_OVERRIDE="$E1000E_OVERRIDE" \
    VSTL_E1000E_OVERRIDE_SHA256="$E1000E_OVERRIDE_SHA256" \
    python3 - <<'PYEOF'
import gzip, hashlib, lzma, os, sys

mode = os.environ.get("VSTL_PXE_INITRD_MODE", "merged")
src = os.environ["PXE_ROOT"] + "/initrd.img"
data = open(src, "rb").read()
if len(data) < 8:
    print("EMPTY"); sys.exit(0)
m6 = data[:6]


def _split_cpio_plus_compressed(buf: bytes):
    """Return (stage1_cpio_bytes, stage2_compressed_bytes, stage2_ext) for a
    Debian-Live-style concatenated cpio_newc + compressed archive. Returns
    None if the buffer is not in that layout."""
    if buf[:6] != b"070701":
        return None
    trailer = buf.find(b"TRAILER!!!\x00")
    if trailer < 0:
        return None
    end_record = trailer + len(b"TRAILER!!!\x00")
    end_record = (end_record + 3) & ~3  # cpio_newc 4-byte alignment
    nxt = end_record
    while nxt < len(buf) and buf[nxt] == 0:
        nxt += 1
    if nxt >= len(buf):
        return None
    nm = buf[nxt:nxt+6].hex()
    ext = ("xz" if nm.startswith("fd377a") else
           "gz" if nm.startswith("1f8b") else
           "zst" if nm.startswith("28b52f") else
           "cpio" if nm.startswith("070701") else "bin")
    return buf[:end_record], buf[nxt:], ext


parts = _split_cpio_plus_compressed(data)
if parts is not None:
    stage1, stage2_compressed, ext = parts
    base = os.path.dirname(src)
    # Always drop the split files alongside as diagnostic artifacts so the
    # operator can verify / fall back at any time without re-running this.
    open(os.path.join(base, "initrd-stage1.cpio"), "wb").write(stage1)
    open(os.path.join(base, f"initrd-stage2.{ext}"), "wb").write(stage2_compressed)
    os.chmod(os.path.join(base, "initrd-stage1.cpio"), 0o444)
    os.chmod(os.path.join(base, f"initrd-stage2.{ext}"), 0o444)

    if mode == "split":
        print(f"SPLIT {ext} {len(stage1)} {len(stage2_compressed)}"); sys.exit(0)

    # Default: MERGE — decompress stage2 (xz/gz/zst → raw cpio), concatenate
    # raw_cpio_1 + raw_cpio_2, gzip-compress the whole thing back to one
    # single archive at maximum-compatibility compression level 6.
    try:
        if ext == "xz":
            stage2_raw = lzma.decompress(stage2_compressed)
        elif ext == "gz":
            stage2_raw = gzip.decompress(stage2_compressed)
        elif ext == "zst":
            import zstandard  # type: ignore
            stage2_raw = zstandard.ZstdDecompressor().decompress(stage2_compressed)
        elif ext == "cpio":
            stage2_raw = stage2_compressed  # already a raw cpio
        else:
            print(f"MERGE_FAIL unsupported_stage2_ext:{ext}"); sys.exit(0)
    except (OSError, lzma.LZMAError, EOFError) as e:
        print(f"MERGE_FAIL decompress:{type(e).__name__}"); sys.exit(0)

    # Also publish a gzip-compressed copy of the live stage-2 archive. The
    # original Clonezilla stage-2 is often xz, but gzip is the most conservative
    # initramfs compression for older Dell/HP UEFI handoff paths while remaining
    # much smaller than the full merged initrd.img.
    if ext != "gz":
        stage2_gz_path = os.path.join(os.path.dirname(src), "initrd-stage2.gz")
        open(stage2_gz_path, "wb").write(gzip.compress(stage2_raw, compresslevel=6, mtime=0))
        os.chmod(stage2_gz_path, 0o444)

    # 2026-05-20 P0 BUGFIX (founder runtime-proved): the legacy
    # byte-concat merge produced ONE gzip wrapping TWO consecutive newc
    # cpio archives. Legacy BIOS kernels tolerated this; modern UEFI iPXE
    # handoff + newer kernels (HP ProBook 640 G8 BIOS T74, Dell Latitude
    # 5500 BIOS 1.43.1) reject it with the exact panic:
    #
    #   Initramfs unpacking failed: invalid magic at start of compressed archive
    #   Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)
    #
    # Founder proved on the live FOG that the working merge is:
    #   (a) extract BOTH stages into a temp tree (stage2 overlays stage1)
    #   (b) repack the unified tree as ONE real newc cpio archive
    #   (c) trim every trailing zero byte after the final TRAILER!!! +
    #       4-byte alignment so the archive ends EXACTLY at the cpio trailer
    #   (d) gzip-compress that single clean archive
    #
    # Working initrd from founder's runtime fix:
    #   size=48,835,560  sha256=d549f3b8060d…  gzip OK / cpio OK / EOF delta 0
    import subprocess, tempfile, shutil
    workdir = tempfile.mkdtemp(prefix="vstl-initrd-merge-")
    try:
        rootdir = os.path.join(workdir, "root")
        os.makedirs(rootdir, exist_ok=True)
        # (a) extract stage1 (newc cpio) into rootdir
        r1 = subprocess.run(
            ["cpio", "-i", "-d", "-m", "-u", "--no-absolute-filenames", "--quiet"],
            input=stage1, cwd=rootdir, capture_output=True,
        )
        if r1.returncode != 0:
            print(f"MERGE_FAIL cpio_extract_stage1:{r1.returncode}"); sys.exit(0)
        # (a) extract stage2 (raw cpio, decompressed above) into rootdir — overlays stage1.
        # `-u` lets stage2 OVERWRITE colliding paths from stage1, matching the
        # founder's intuitive "stage2 is the amended overlay" semantics.
        r2 = subprocess.run(
            ["cpio", "-i", "-d", "-m", "-u", "--no-absolute-filenames", "--quiet"],
            input=stage2_raw, cwd=rootdir, capture_output=True,
        )
        if r2.returncode != 0:
            print(f"MERGE_FAIL cpio_extract_stage2:{r2.returncode}"); sys.exit(0)
        # Dell Latitude 5490 recovery for Intel I219-LM4 device 8086:15d7.
        # A small number of units have an invalid onboard NVM checksum. UEFI
        # NII can still PXE with the NIC, but stock e1000e aborts probe with
        # error -5 before Linux creates an interface. Replace only this exact
        # kernel module with a guarded build whose allow_bad_nvm parameter
        # bypasses the checksum gate only for 8086:15d7 and never writes NVM.
        override_path = os.environ.get("VSTL_E1000E_OVERRIDE", "")
        override_sha = os.environ.get("VSTL_E1000E_OVERRIDE_SHA256", "")
        stock_e1000e = os.path.join(
            rootdir,
            "usr/lib/modules/6.12.32-amd64/kernel/drivers/net/ethernet/intel/e1000e/e1000e.ko.xz",
        )
        if not os.path.isfile(override_path) or not os.path.isfile(stock_e1000e):
            print("MERGE_FAIL missing_e1000e_override_or_stock_module"); sys.exit(0)
        with open(override_path, "rb") as f:
            override_data = f.read()
        if hashlib.sha256(override_data).hexdigest() != override_sha:
            print("MERGE_FAIL e1000e_override_sha256_mismatch"); sys.exit(0)
        with open(stock_e1000e, "wb") as f:
            f.write(override_data)
        os.chmod(stock_e1000e, 0o644)
        # 2026-06-21 Dell Latitude 5490 PXE fix: some units can download the
        # kernel/initrd through firmware NII iPXE, then live-boot sees no Linux
        # NIC yet and loops at "Waiting for ethernet card(s) up".  The network
        # modules are present in the initrd; force-load the common Dell/bench
        # wired drivers immediately before live-boot scans /sys/class/net.
        #
        # 2026-06-26 Lenovo X13 / Type-C Ethernet fix: firmware PXE can fetch
        # the kernel through a USB-C dongle, but live-boot may expose the dongle
        # later or under a non-eth0 name. Resolve the actual PXE MAC from
        # live-netdev after udev/module settling, and make carrier wait honor
        # ethdevice-timeout instead of the fixed 15-second upstream loop.
        select_eth_path = os.path.join(rootdir, "usr/lib/live/boot/9990-select-eth-device.sh")
        if os.path.exists(select_eth_path):
            with open(select_eth_path, "r", encoding="utf-8", errors="ignore") as f:
                select_eth = f.read()
            marker = "# VSTL network module priming"
            if marker not in select_eth:
                needle = "\t# Ensure all our net modules get loaded so we can actually compare MAC addresses...\n"
                prime = (
                    "\t# VSTL network module priming: Dell Latitude firmware can PXE\n"
                    "\t# through NII while Linux has not autoloaded the real NIC yet.\n"
                    "\t# Reload e1000e with the checksum override. The custom module\n"
                    "\t# itself limits bypass to Intel 8086:15d7 and does not write NVM.\n"
                    "\tmodprobe -r e1000e 2>/dev/null || true\n"
                    "\tmodprobe e1000e allow_bad_nvm=1 2>/dev/null || modprobe e1000e 2>/dev/null || true\n"
                    "\tfor module in igb igc r8169 r8152 r8153_ecm usbnet cdc_ether cdc_eem cdc_ncm cdc_mbim ax88179_178a asix aqc111 lan78xx smsc75xx smsc95xx rtl8150 dm9601 sr9700 mcs7830 cdc_subset thunderbolt_net tg3 bnx2 bnx2x alx atl1c sky2 forcedeth\n"
                    "\tdo\n"
                    "\t\tmodprobe -q \"$module\" 2>/dev/null || true\n"
                    "\tdone\n"
                    "\tudevadm trigger --subsystem-match=net --action=add 2>/dev/null || udevadm trigger\n"
                    "\tudevadm settle --timeout=8 2>/dev/null || udevadm settle\n"
                    "\n"
                    "\t# Some Dell UEFI NII implementations leave the Intel NIC in a\n"
                    "\t# firmware-owned PCI power state. If the ordinary module load did\n"
                    "\t# not create a netdev, reset the PCI function and bind the driver\n"
                    "\t# selected by its modalias. This runs only when no NIC exists.\n"
                    "\tif ! ls /sys/class/net 2>/dev/null | grep -qv '^lo$'\n"
                    "\tthen\n"
                    "\t\techo 'VSTL: Linux NIC missing after UEFI handoff; starting PCI recovery.'\n"
                    "\t\techo 1 > /sys/bus/pci/rescan 2>/dev/null || true\n"
                    "\t\tfor attempt in 1 2 3\n"
                    "\t\tdo\n"
                    "\t\t\tfor pci_device in /sys/bus/pci/devices/*\n"
                    "\t\t\tdo\n"
                    "\t\t\t\t[ -r \"$pci_device/class\" ] || continue\n"
                    "\t\t\t\tcase \"$(cat \"$pci_device/class\" 2>/dev/null)\" in\n"
                    "\t\t\t\t\t0x0200*) ;;\n"
                    "\t\t\t\t\t*) continue ;;\n"
                    "\t\t\t\tesac\n"
                    "\t\t\t\techo on > \"$pci_device/power/control\" 2>/dev/null || true\n"
                    "\t\t\t\techo 0 > \"$pci_device/d3cold_allowed\" 2>/dev/null || true\n"
                    "\t\t\t\tif [ \"$attempt\" -gt 1 ] && [ -w \"$pci_device/reset\" ]\n"
                    "\t\t\t\tthen\n"
                    "\t\t\t\t\techo 1 > \"$pci_device/reset\" 2>/dev/null || true\n"
                    "\t\t\t\tfi\n"
                    "\t\t\t\tmodalias=$(cat \"$pci_device/modalias\" 2>/dev/null || true)\n"
                    "\t\t\t\tif [ -n \"$modalias\" ]\n"
                    "\t\t\t\tthen\n"
                    "\t\t\t\t\tfor driver in $(modprobe -R \"$modalias\" 2>/dev/null | head -n 3)\n"
                    "\t\t\t\t\tdo\n"
                    "\t\t\t\t\t\tmodprobe \"$driver\" 2>/dev/null || true\n"
                    "\t\t\t\t\tdone\n"
                    "\t\t\t\tfi\n"
                    "\t\t\tdone\n"
                    "\t\t\tudevadm trigger --subsystem-match=pci --action=add 2>/dev/null || true\n"
                    "\t\t\tudevadm trigger --subsystem-match=net --action=add 2>/dev/null || true\n"
                    "\t\t\tudevadm settle --timeout=8 2>/dev/null || true\n"
                    "\t\t\tsleep 2\n"
                    "\t\t\tls /sys/class/net 2>/dev/null | grep -qv '^lo$' && break\n"
                    "\t\tdone\n"
                    "\tfi\n"
                    "\n"
                    "\tif ! ls /sys/class/net 2>/dev/null | grep -qv '^lo$'\n"
                    "\tthen\n"
                    "\t\techo 'VSTL NIC DIAGNOSTIC: PCI Ethernet device did not create a Linux interface.'\n"
                    "\t\tfor pci_device in /sys/bus/pci/devices/*\n"
                    "\t\tdo\n"
                    "\t\t\t[ -r \"$pci_device/class\" ] || continue\n"
                    "\t\t\tcase \"$(cat \"$pci_device/class\" 2>/dev/null)\" in 0x0200*) ;; *) continue ;; esac\n"
                    "\t\t\tdriver=UNBOUND\n"
                    "\t\t\t[ -L \"$pci_device/driver\" ] && driver=$(basename \"$(readlink \"$pci_device/driver\")\")\n"
                    "\t\t\techo \"VSTL NIC PCI $(basename \"$pci_device\") vendor=$(cat \"$pci_device/vendor\" 2>/dev/null) device=$(cat \"$pci_device/device\" 2>/dev/null) driver=$driver\"\n"
                    "\t\tdone\n"
                    "\t\tdmesg 2>/dev/null | grep -Ei 'e1000e|igc|igb|r8169|ethernet|firmware|NVM|probe.*(fail|error)' | tail -n 20\n"
                    "\tfi\n"
                    "\n"
                )
                if needle in select_eth:
                    select_eth = select_eth.replace(needle, prime + needle, 1)
                else:
                    select_eth = select_eth.replace(
                        "\tmodprobe -q af_packet\n",
                        "\tmodprobe -q af_packet\n" + prime,
                        1,
                    )
            carrier_marker = "# VSTL carrier timeout extension"
            usb_recovery_marker = "# VSTL USB-C NIC recovery"
            if usb_recovery_marker not in select_eth:
                wait_start = select_eth.find("Wait_for_carrier ()\n")
                wait_end = select_eth.find("\nSelect_eth_device ()\n", wait_start)
                if wait_start >= 0 and wait_end > wait_start:
                    recovered_wait = r'''Vstl_interface_for_mac ()
{
	target_mac=$(echo "$1" | tr 'A-F' 'a-f')
	[ -n "$target_mac" ] || return 1
	for candidate_path in /sys/class/net/*
	do
		[ -r "$candidate_path/address" ] || continue
		candidate_mac=$(cat "$candidate_path/address" 2>/dev/null | tr 'A-F' 'a-f')
		if [ "$candidate_mac" = "$target_mac" ]
		then
			echo "${candidate_path##*/}"
			return 0
		fi
	done
	return 1
}

Vstl_connected_interface ()
{
	preferred_mac=$(echo "$1" | tr 'A-F' 'a-f')
	# Prefer the firmware-PXE interface, but accept any physical wired
	# interface with carrier. USB-C adapters can return as eth1 or expose a
	# different runtime MAC after ExitBootServices.
	for pass in preferred fallback
	do
		for candidate_path in /sys/class/net/*
		do
			candidate=${candidate_path##*/}
			case "$candidate" in lo|wl*|ww*|p2p*) continue ;; esac
			[ -e "$candidate_path/device" ] || continue
			[ "$(cat "$candidate_path/type" 2>/dev/null)" = 1 ] || continue
			# Carrier is reported as "unknown" while a newly enumerated USB NIC
			# is administratively down. Raise every wired candidate before
			# deciding that the Type-C adapter has no link.
			Vstl_nic_power_on "$candidate" >/dev/null 2>&1 || true
			candidate_mac=$(cat "$candidate_path/address" 2>/dev/null | tr 'A-F' 'a-f')
			if [ "$pass" = preferred ] && [ -n "$preferred_mac" ] && [ "$candidate_mac" != "$preferred_mac" ]
			then
				continue
			fi
			[ "$(cat "$candidate_path/carrier" 2>/dev/null)" = 1 ] || continue
			echo "$candidate"
			return 0
		done
	done
	return 1
}

Vstl_print_network_state ()
{
	echo ""
	echo "VSTL: Linux network interfaces during PXE handoff:"
	for candidate_path in /sys/class/net/*
	do
		candidate=${candidate_path##*/}
		[ "$candidate" = lo ] && continue
		candidate_mac=$(cat "$candidate_path/address" 2>/dev/null || echo unknown)
		candidate_carrier=$(cat "$candidate_path/carrier" 2>/dev/null || echo unknown)
		candidate_device=$(readlink -f "$candidate_path/device" 2>/dev/null || echo none)
		candidate_driver=$(basename "$(readlink -f "$candidate_path/device/driver" 2>/dev/null || echo none)")
		echo "VSTL: $candidate mac=$candidate_mac carrier=$candidate_carrier driver=$candidate_driver device=$candidate_device"
	done
	for usb_interface in /sys/bus/usb/devices/*:*
	do
		[ -r "$usb_interface/bInterfaceClass" ] || continue
		usb_class=$(cat "$usb_interface/bInterfaceClass" 2>/dev/null || true)
		usb_driver=$(basename "$(readlink -f "$usb_interface/driver" 2>/dev/null || echo none)")
		case "$usb_class:$usb_driver" in
			02:*|0a:*|e0:*|ff:r8152|ff:ax88179_178a|ff:asix|ff:aqc111)
				echo "VSTL: USB interface ${usb_interface##*/} class=$usb_class driver=$usb_driver"
				;;
		esac
	done
}

Vstl_nic_power_on ()
{
	interface="$1"
	[ -e "/sys/class/net/$interface" ] || return 1
	device_path=$(readlink -f "/sys/class/net/$interface/device" 2>/dev/null || true)
	power_path="$device_path"
	while [ -n "$power_path" ] && [ "$power_path" != "/" ]
	do
		[ -w "$power_path/power/control" ] && echo on > "$power_path/power/control" 2>/dev/null || true
		case "$power_path" in
			/sys/devices/*) power_path=${power_path%/*} ;;
			*) break ;;
		esac
	done
	ip link set "$interface" up 2>/dev/null || true
	if command -v ethtool >/dev/null 2>&1
	then
		ethtool -s "$interface" autoneg on 2>/dev/null || true
	fi
}

Vstl_rebind_nic ()
{
	# VSTL USB-C NIC recovery: UEFI PXE can leave a USB Ethernet driver
	# registered but with carrier permanently down. Rebind the selected
	# interface driver whether Linux exposes it through USB, PCI, or a dock.
	interface="$1"
	target_mac="$2"
	VSTL_RECOVERED_INTERFACE=""
	device_path=$(readlink -f "/sys/class/net/$interface/device" 2>/dev/null || true)
	driver_path=$(readlink -f "$device_path/driver" 2>/dev/null || true)
	device_id=${device_path##*/}
	echo ""
	echo "VSTL: recovery probe interface=$interface device=$device_path driver=$driver_path"
	[ -n "$driver_path" ] || return 1
	[ -w "$driver_path/unbind" ] || return 1
	[ -w "$driver_path/bind" ] || return 1

	echo "VSTL: carrier is still down on $interface; rebinding its Linux network driver."
	ip link set "$interface" down 2>/dev/null || true
	printf '%s' "$device_id" > "$driver_path/unbind" 2>/dev/null || return 1
	sleep 2
	printf '%s' "$device_id" > "$driver_path/bind" 2>/dev/null || return 1
	udevadm trigger --subsystem-match=net --action=add 2>/dev/null || true
	udevadm settle --timeout=8 2>/dev/null || true

	for rediscovery_step in $(seq 1 15)
	do
		VSTL_RECOVERED_INTERFACE=$(Vstl_interface_for_mac "$target_mac" 2>/dev/null || true)
		if [ -n "$VSTL_RECOVERED_INTERFACE" ]
		then
			Vstl_nic_power_on "$VSTL_RECOVERED_INTERFACE"
			echo "VSTL: network interface returned as $VSTL_RECOVERED_INTERFACE."
			return 0
		fi
		sleep 1
	done
	return 1
}

Vstl_rebind_known_usb_network_drivers ()
{
	# The firmware PXE adapter may disappear before a netdev is created, so
	# there is no /sys/class/net path to reset. Rebind all attached USB-network
	# drivers, then retrigger USB and net uevents.
	reset_count=0
	for driver_name in r8152 r8153_ecm cdc_ether cdc_eem cdc_ncm cdc_mbim ax88179_178a asix aqc111 lan78xx smsc75xx smsc95xx rtl8150 dm9601 sr9700 mcs7830 cdc_subset
	do
		driver_path="/sys/bus/usb/drivers/$driver_name"
		[ -d "$driver_path" ] || continue
		for bound_path in "$driver_path"/*:*
		do
			[ -L "$bound_path" ] || continue
			bound_id=${bound_path##*/}
			echo "VSTL: rebinding USB network driver=$driver_name interface=$bound_id"
			printf '%s' "$bound_id" > "$driver_path/unbind" 2>/dev/null || continue
			sleep 2
			printf '%s' "$bound_id" > "$driver_path/bind" 2>/dev/null || true
			reset_count=$((reset_count + 1))
		done
	done
	for module in r8152 r8153_ecm cdc_ether cdc_eem cdc_ncm cdc_mbim ax88179_178a asix aqc111 lan78xx smsc75xx smsc95xx rtl8150 dm9601 sr9700 mcs7830 cdc_subset usbnet
	do
		modprobe -q "$module" 2>/dev/null || true
	done
	udevadm trigger --subsystem-match=usb --action=add 2>/dev/null || true
	udevadm trigger --subsystem-match=net --action=add 2>/dev/null || true
	udevadm settle --timeout=10 2>/dev/null || true
	echo "VSTL: USB network recovery completed; rebound=$reset_count"
}

Wait_for_carrier ()
{
	# $1 = network device
	original_interface="$1"
	current_interface="$1"
	target_mac=""
	for ARGUMENT in ${LIVE_BOOT_CMDLINE}
	do
		case "$ARGUMENT" in
			live-netdev=*:*) target_mac=$(echo "${ARGUMENT#live-netdev=}" | tr 'A-F' 'a-f') ;;
		esac
	done
	[ -n "$target_mac" ] || target_mac=$(cat "/sys/class/net/$current_interface/address" 2>/dev/null | tr 'A-F' 'a-f')
	carrier_wait="${ETHDEV_TIMEOUT:-120}"
	usb_rebind_attempted=0
	VSTL_CARRIER_DEVICE="$current_interface"

	echo -n "Waiting for link to come up on $current_interface... "
	Vstl_nic_power_on "$current_interface"
	# VSTL carrier timeout extension: Type-C Ethernet dongles can
	# re-enumerate after firmware PXE hands control to Linux.
	for step in $(seq 1 "$carrier_wait")
	do
		connected_interface=$(Vstl_connected_interface "$target_mac" 2>/dev/null || true)
		if [ -n "$connected_interface" ]
		then
			current_interface="$connected_interface"
			VSTL_CARRIER_DEVICE="$current_interface"
			echo -e "\nLink is up on $current_interface"
			if [ "$current_interface" != "$original_interface" ]
			then
				echo "DEVICE=$current_interface" >> /conf/param.conf
			fi
			return 0
		fi
		if [ -n "$target_mac" ]
		then
			rediscovered=$(Vstl_interface_for_mac "$target_mac" 2>/dev/null || true)
			[ -n "$rediscovered" ] && current_interface="$rediscovered"
		fi
		VSTL_CARRIER_DEVICE="$current_interface"
		carrier=$(cat "/sys/class/net/$current_interface/carrier" 2>/dev/null)
		case "${carrier}" in
			1)
				echo -e "\nLink is up on $current_interface"
				if [ "$current_interface" != "$original_interface" ]
				then
					echo "DEVICE=$current_interface" >> /conf/param.conf
				fi
				return 0
				;;
		esac

		case "$step" in
			5|30|60|90)
				# Restart autonegotiation without disturbing an established link.
				ip link set "$current_interface" down 2>/dev/null || true
				sleep 1
				Vstl_nic_power_on "$current_interface"
				;;
			10)
				if [ "$usb_rebind_attempted" -eq 0 ] && [ -n "$target_mac" ]
				then
					usb_rebind_attempted=1
					Vstl_print_network_state
					current_mac=$(cat "/sys/class/net/$current_interface/address" 2>/dev/null | tr 'A-F' 'a-f')
					if [ -n "$current_mac" ] && [ "$current_mac" = "$target_mac" ] && Vstl_rebind_nic "$current_interface" "$target_mac"
					then
						current_interface="$VSTL_RECOVERED_INTERFACE"
						VSTL_CARRIER_DEVICE="$current_interface"
					fi
					# Lenovo MAC pass-through can make BOOTIF identify the internal
					# Intel controller while the cable is physically connected to a
					# Realtek USB-C adapter with a different runtime MAC. Always reset
					# attached USB-network drivers too, then the next loop can select
					# whichever wired interface actually has carrier.
					Vstl_rebind_known_usb_network_drivers
				fi
				;;
			20)
				Vstl_print_network_state
				;;
		esac

		# Counter remains visible on quiet boots, including Dell systems
		# that otherwise appear to stop on a blank screen.
		echo -n "$step "
		sleep 1
	done
	echo -e "\nError - carrier not detected on $current_interface after ${carrier_wait}s."
	Vstl_print_network_state
	return 1
}
'''
                    select_eth = select_eth[:wait_start] + recovered_wait + select_eth[wait_end:]
                else:
                    print("MERGE_FAIL select_eth_wait_function_not_found"); sys.exit(0)
            elif carrier_marker not in select_eth:
                print("MERGE_FAIL carrier_timeout_marker_missing"); sys.exit(0)
            resolver_marker = "# VSTL BOOTIF/live-netdev resolver"
            if resolver_marker not in select_eth:
                resolver = (
                    "\n"
                    "\t# VSTL BOOTIF/live-netdev resolver: iPXE passes the MAC of the\n"
                    "\t# adapter that fetched the kernel. USB-C Ethernet adapters may\n"
                    "\t# appear after the first udev settle, so wait briefly for the\n"
                    "\t# matching Linux netdev before falling back to eth0.\n"
                    "\tVSTL_LIVENETDEV=\"\"\n"
                    "\tfor ARGUMENT in ${LIVE_BOOT_CMDLINE}\n"
                    "\tdo\n"
                    "\t\tcase \"${ARGUMENT}\" in\n"
                    "\t\t\tlive-netdev=*)\n"
                    "\t\t\t\tVSTL_LIVENETDEV=\"${ARGUMENT#live-netdev=}\"\n"
                    "\t\t\t\t;;\n"
                    "\t\tesac\n"
                    "\tdone\n"
                    "\tif [ -z \"$DEVICE\" ] && [ -n \"$VSTL_LIVENETDEV\" ]\n"
                    "\tthen\n"
                    "\t\tcase \"$VSTL_LIVENETDEV\" in\n"
                    "\t\t\t*:*)\n"
                    "\t\t\t\tVSTL_TARGET_MAC=$(echo \"$VSTL_LIVENETDEV\" | tr 'A-F' 'a-f')\n"
                    "\t\t\t\tfor step in $(seq 1 \"${ETHDEV_TIMEOUT:-120}\")\n"
                    "\t\t\t\tdo\n"
                    "\t\t\t\t\tfor device in /sys/class/net/*\n"
                    "\t\t\t\t\tdo\n"
                    "\t\t\t\t\t\t[ -r \"$device/address\" ] || continue\n"
                    "\t\t\t\t\t\tcurrent_mac=$(cat \"$device/address\" | tr 'A-F' 'a-f')\n"
                    "\t\t\t\t\t\tif [ \"$VSTL_TARGET_MAC\" = \"$current_mac\" ]\n"
                    "\t\t\t\t\t\tthen\n"
                    "\t\t\t\t\t\t\tDEVICE=${device##*/}\n"
                    "\t\t\t\t\t\t\tbreak\n"
                    "\t\t\t\t\t\tfi\n"
                    "\t\t\t\t\tdone\n"
                    "\t\t\t\t\t[ -n \"$DEVICE\" ] && break\n"
                    "\t\t\t\t\tmodprobe -q r8152 2>/dev/null || true\n"
                    "\t\t\t\t\tmodprobe -q cdc_ether 2>/dev/null || true\n"
                    "\t\t\t\t\tmodprobe -q cdc_ncm 2>/dev/null || true\n"
                    "\t\t\t\t\tmodprobe -q ax88179_178a 2>/dev/null || true\n"
                    "\t\t\t\t\tudevadm trigger --subsystem-match=net --action=add 2>/dev/null || true\n"
                    "\t\t\t\t\tudevadm settle --timeout=2 2>/dev/null || true\n"
                    "\t\t\t\t\tcase \"$step\" in\n"
                    "\t\t\t\t\t\t5|15|30|60)\n"
                    "\t\t\t\t\t\t\tVstl_rebind_known_usb_network_drivers\n"
                    "\t\t\t\t\t\t\t;;\n"
                    "\t\t\t\t\tesac\n"
                    "\t\t\t\t\tif [ -z \"$DEVICE\" ] && [ \"$step\" -ge 8 ]\n"
                    "\t\t\t\t\tthen\n"
                    "\t\t\t\t\t\tDEVICE=$(Vstl_connected_interface \"$VSTL_TARGET_MAC\" 2>/dev/null || true)\n"
                    "\t\t\t\t\t\t[ -n \"$DEVICE\" ] && echo \"VSTL: selected connected wired NIC $DEVICE while live-netdev=$VSTL_LIVENETDEV was delayed\"\n"
                    "\t\t\t\t\tfi\n"
                    "\t\t\t\t\tsleep 1\n"
                    "\t\t\t\tdone\n"
                    "\t\t\t\t;;\n"
                    "\t\t\t*)\n"
                    "\t\t\t\t[ -e \"/sys/class/net/$VSTL_LIVENETDEV\" ] && DEVICE=\"$VSTL_LIVENETDEV\"\n"
                    "\t\t\t\t;;\n"
                    "\t\tesac\n"
                    "\t\t[ -n \"$DEVICE\" ] && echo \"VSTL: selected PXE NIC $DEVICE from live-netdev=$VSTL_LIVENETDEV\"\n"
                    "\tfi\n"
                )
                select_eth = select_eth.replace(
                    "\t# See if we can derive the boot device\n\tDevice_from_bootif\n\n\tif [ -z \"$DEVICE\" ]\n",
                    "\t# See if we can derive the boot device\n\tDevice_from_bootif\n" + resolver + "\n\tif [ -z \"$DEVICE\" ]\n",
                    1,
                )
            # Persist every initrd network change. This must not depend on the
            # resolver being new because an already patched server may only
            # need the newer USB carrier recovery block.
            with open(select_eth_path, "w", encoding="utf-8") as f:
                f.write(select_eth)
        # (b) repack unified tree as ONE newc cpio archive
        # Match the runtime-proven May 23 archive recipe exactly: include the
        # root "." record and keep normal find traversal order. Omitting "."
        # or using -depth changes the archive shape and has not been proven on
        # the affected Dell/HP UEFI firmware.
        find_p = subprocess.Popen(
            ["find", ".", "-print0"],
            stdout=subprocess.PIPE, cwd=rootdir,
        )
        cpio_p = subprocess.Popen(
            ["cpio", "-o", "-H", "newc", "--null", "--quiet"],
            stdin=find_p.stdout, stdout=subprocess.PIPE, cwd=rootdir,
        )
        find_p.stdout.close()
        merged_raw, _ = cpio_p.communicate()
        find_p.wait()
        if cpio_p.returncode != 0:
            print(f"MERGE_FAIL cpio_repack:{cpio_p.returncode}"); sys.exit(0)
        # (c) trim trailing zero padding so archive ends EXACTLY at the final
        # TRAILER!!!\0 record + 4-byte alignment. Modern UEFI initramfs
        # unpackers are strict; trailing garbage causes them to bail out.
        trailer_token = b"TRAILER!!!\x00"
        last_trailer = merged_raw.rfind(trailer_token)
        if last_trailer < 0:
            print("MERGE_FAIL no_trailer_in_repack"); sys.exit(0)
        end_of_trailer = last_trailer + len(trailer_token)
        aligned_end = (end_of_trailer + 3) & ~3
        merged_raw = merged_raw[:aligned_end]
        # (d) gzip the single clean cpio archive
        merged_gz = gzip.compress(merged_raw, compresslevel=6, mtime=0)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    backup = src + ".pre-merge"
    if not os.path.exists(backup):
        os.replace(src, backup)
    else:
        os.unlink(src)
    open(src, "wb").write(merged_gz)
    os.chmod(src, 0o444)
    # 2026-05-20 — also publish the uncompressed merged cpio as initrd.cpio
    # so the operator can swap boot.ipxe → boot-diag-cpio.ipxe to isolate
    # the gzip-handoff path from the cpio-content path when the kernel
    # still panics with "invalid magic at start of compressed archive"
    # despite a structurally-valid merged cpio (Dell Latitude 5500 case).
    cpio_path = os.path.join(os.path.dirname(src), "initrd.cpio")
    if os.path.exists(cpio_path):
        os.unlink(cpio_path)
    open(cpio_path, "wb").write(merged_raw)
    os.chmod(cpio_path, 0o444)
    runtime_raw_path = os.path.join(os.path.dirname(src), "initrd-vstl-fixed.cpio")
    if os.path.exists(runtime_raw_path):
        os.unlink(runtime_raw_path)
    open(runtime_raw_path, "wb").write(merged_raw)
    os.chmod(runtime_raw_path, 0o444)
    print(f"MERGED gz {len(stage1)} {len(stage2_compressed)} -> {len(merged_gz)}"); sys.exit(0)

if m6[:2] == b"\x1f\x8b":   print("GZIP")
elif m6[:3] == b"\xfd7z":     print("XZ")
elif m6[:4] == b"\x28\xb5\x2f\xfd": print("ZSTD")
else:
    print(f"UNKNOWN:{m6.hex()}")
PYEOF
)

log "  initrd profile: $INITRD_PROFILE"
case "$INITRD_PROFILE" in
    EMPTY)
        die "initrd.img is EMPTY — ISO build is broken. Re-run sudo ./04_build_live_iso_clonezilla.sh."
        ;;
    MERGE_FAIL*)
        warn "Auto-merge fell back to split mode: ${INITRD_PROFILE#MERGE_FAIL }."
        warn "Re-running detector in legacy split mode for safe fallback."
        # Re-invoke detector in split-only mode so we get the SPLIT layout
        INITRD_PROFILE=$(PXE_ROOT="$PXE_ROOT" VSTL_PXE_INITRD_MODE=split python3 - <<'PYEOF'
import os, sys
src = os.environ["PXE_ROOT"] + "/initrd.img"
data = open(src, "rb").read()
if data[:6] != b"070701":
    print(f"UNKNOWN:{data[:6].hex()}"); sys.exit(0)
trailer = data.find(b"TRAILER!!!\x00")
end_record = trailer + len(b"TRAILER!!!\x00")
end_record = (end_record + 3) & ~3
nxt = end_record
while nxt < len(data) and data[nxt] == 0:
    nxt += 1
nm = data[nxt:nxt+6].hex()
ext = ("xz" if nm.startswith("fd377a") else
       "gz" if nm.startswith("1f8b") else
       "zst" if nm.startswith("28b52f") else "bin")
print(f"SPLIT {ext} {end_record} {len(data)-nxt}")
PYEOF
)
        log "  fallback initrd profile: $INITRD_PROFILE"
        ;;
esac

case "$INITRD_PROFILE" in
    MERGED*)
        # Single clean gzip initrd.img — what Dell Latitude 5500 / iPXE
        # combos expect. Diagnostic stage1/stage2 files remain alongside
        # in case the operator needs to fall back manually.
        # Prefer a named initrd so the kernel's initrd= argument matches the
        # iPXE image name. Older iPXE builds that lack --name fall back to the
        # plain initrd command.
        INITRD_LINES="initrd http://${SERVER_IP}/vstl-pxe/initrd.img"
        SIZES=$(awk '{print $3,"+",$4,"->",$5,"bytes"}' <<< "$INITRD_PROFILE")
        log "  initrd merged → single gzip archive ($SIZES) — clean UEFI handoff"
        ;;
    SPLIT*)
        # Legacy v3.2 dual-initrd boot mode (works on most stacks but
        # NOT on Dell Latitude 5500 BIOS 1.42 — see comment above).
        STAGE2_EXT=$(awk '{print $2}' <<< "$INITRD_PROFILE")
        INITRD_LINES=$(cat <<INITRD_EOF
initrd http://${SERVER_IP}/vstl-pxe/initrd-stage1.cpio
initrd http://${SERVER_IP}/vstl-pxe/initrd-stage2.${STAGE2_EXT}
INITRD_EOF
)
        log "  initrd was concatenated multi-stage cpio+${STAGE2_EXT} — using legacy split mode"
        ;;
    SINGLE_CPIO|GZIP|XZ|ZSTD)
        # Single-archive initrd — use the standard one-line directive
        # with a direct initrd URL for UEFI compatibility.
        INITRD_LINES="initrd http://${SERVER_IP}/vstl-pxe/initrd.img"
        log "  initrd is a single-archive — using standard 1-stage UEFI handoff"
        ;;
    *)
        die "initrd.img has unrecognized format ($INITRD_PROFILE). Kernel will panic. Re-run sudo ./04_build_live_iso_clonezilla.sh."
        ;;
esac

PRODUCTION_INITRD_NAME="initrd.img"
PRODUCTION_INITRD_SOURCE="$PXE_ROOT/initrd.img"
PRODUCTION_INITRD_LINES="$INITRD_LINES"
PRODUCTION_INITRD_LABEL="compressed initrd.img"
PRODUCTION_HTTP_FILES="initrd.img"

if [[ "$PXE_BOOT_STYLE" == "classic-raw-cpio" ]]; then
    if [[ -f "$PXE_ROOT/initrd-vstl-fixed.cpio" ]]; then
        PRODUCTION_INITRD_NAME="initrd-vstl-fixed.cpio"
        PRODUCTION_INITRD_SOURCE="$PXE_ROOT/initrd-vstl-fixed.cpio"
        PRODUCTION_INITRD_LINES="initrd http://${SERVER_IP}/vstl-pxe/initrd-vstl-fixed.cpio"
        PRODUCTION_INITRD_LABEL="runtime-proven unified raw CPIO"
        PRODUCTION_HTTP_FILES="initrd-vstl-fixed.cpio"
        log "  production boot style: classic-raw-cpio - runtime-proven raw initrd"
    else
        warn "Requested classic raw-cpio boot style, but initrd-vstl-fixed.cpio was not produced; keeping ${PRODUCTION_INITRD_NAME}."
    fi
elif [[ "$PXE_BOOT_STYLE" == "stage2-xz" ]]; then
    STAGE2_INITRD=""
    for ext in gz xz zst cpio; do
        candidate="$PXE_ROOT/initrd-stage2.${ext}"
        if [[ -f "$candidate" ]]; then
            STAGE2_INITRD="initrd-stage2.${ext}"
            break
        fi
    done
    if [[ -n "$STAGE2_INITRD" ]]; then
        PRODUCTION_INITRD_NAME="$STAGE2_INITRD"
        PRODUCTION_INITRD_SOURCE="$PXE_ROOT/$STAGE2_INITRD"
        PRODUCTION_INITRD_LINES="initrd http://${SERVER_IP}/vstl-pxe/${STAGE2_INITRD}"
        PRODUCTION_INITRD_LABEL="stage-2 live initrd (${STAGE2_INITRD})"
        PRODUCTION_HTTP_FILES="$STAGE2_INITRD"
        log "  production boot style: stage2-xz - old-generation UEFI-safe ${STAGE2_INITRD}"
    else
        warn "Requested stage2-xz boot style, but initrd-stage2.* was not produced; keeping ${PRODUCTION_INITRD_NAME}."
    fi
elif [[ "$PXE_BOOT_STYLE" == "classic-split" ]]; then
    STAGE2_INITRD=""
    for ext in xz gz zst cpio; do
        candidate="$PXE_ROOT/initrd-stage2.${ext}"
        if [[ -f "$candidate" ]]; then
            STAGE2_INITRD="initrd-stage2.${ext}"
            break
        fi
    done
    if [[ -f "$PXE_ROOT/initrd-stage1.cpio" && -n "$STAGE2_INITRD" ]]; then
        PRODUCTION_INITRD_NAME="initrd-stage1.cpio"
        PRODUCTION_INITRD_SOURCE="$PXE_ROOT/initrd-stage1.cpio"
        PRODUCTION_INITRD_LINES=$(cat <<INITRD_EOF
initrd http://${SERVER_IP}/vstl-pxe/initrd-stage1.cpio
initrd http://${SERVER_IP}/vstl-pxe/${STAGE2_INITRD}
INITRD_EOF
)
        PRODUCTION_INITRD_LABEL="split initrd-stage1.cpio + ${STAGE2_INITRD}"
        PRODUCTION_HTTP_FILES="initrd-stage1.cpio ${STAGE2_INITRD}"
        log "  production boot style: classic-split - ${PRODUCTION_INITRD_LABEL}"
    else
        warn "Requested classic-split boot style, but split initrd files are missing; keeping ${PRODUCTION_INITRD_NAME}."
    fi
else
    log "  production boot style: ${PXE_BOOT_STYLE} - ${PRODUCTION_INITRD_LABEL}"
fi

VMLINUZ_SIZE=$(stat -c '%s' "$PXE_ROOT/vmlinuz" 2>/dev/null || echo 0)
[[ "$VMLINUZ_SIZE" -lt 1000000 ]] && die "vmlinuz is too small ($VMLINUZ_SIZE bytes). Re-run sudo ./04_build_live_iso_clonezilla.sh."
log "  vmlinuz: ${VMLINUZ_SIZE} bytes — OK"

# 2026-05-20 — compute initrd SHA prefix so we can emit it in the iPXE
# banner. Operators reading the bench screen at boot time can confirm at
# a glance whether they're loading the FOG's authentic initrd vs a rogue
# server's stale one. Only the first 12 hex chars — easy to read on screen.
INITRD_SHA_PREFIX=$(sha256sum "$PRODUCTION_INITRD_SOURCE" 2>/dev/null | cut -c1-12 || echo "unknown")
PRODUCTION_INITRD_SIZE=$(stat -c '%s' "$PRODUCTION_INITRD_SOURCE" 2>/dev/null || echo 0)
log "  ${PRODUCTION_INITRD_NAME} build SHA prefix: ${INITRD_SHA_PREFIX} (visible at boot banner)"

SQUASHFS_HEX=$(head -c 4 "$PXE_ROOT/filesystem.squashfs" | od -An -tx1 | tr -d ' \n')
[[ "$SQUASHFS_HEX" != "68737173" ]] && die "filesystem.squashfs has wrong magic '$SQUASHFS_HEX' (expected '68737173' = 'hsqs'). Re-run sudo ./04_build_live_iso_clonezilla.sh."
log "  filesystem.squashfs: valid squashfs magic — OK"

# Verify Apache will serve identical bytes for each asset.
APACHE_URL_BASE="http://127.0.0.1/vstl-pxe"
HTTP_FILES=$(printf '%s\n' vmlinuz filesystem.squashfs $PRODUCTION_HTTP_FILES | awk 'NF && !seen[$0]++')
for f in $HTTP_FILES; do
    LOCAL_SIZE=$(stat -c '%s' "$PXE_ROOT/$f" 2>/dev/null || echo 0)
    HTTP_CODE=$(curl -sI -o /dev/null -w '%{http_code}' "$APACHE_URL_BASE/$f" 2>/dev/null || echo "0")
    HTTP_LEN=$(curl -sI "$APACHE_URL_BASE/$f" 2>/dev/null | awk -v IGNORECASE=1 '/^content-length:/{print $2}' | tr -d '\r')
    if [[ "$HTTP_CODE" != "200" ]]; then
        die "Apache returned HTTP $HTTP_CODE for $APACHE_URL_BASE/$f — bench laptops will get garbage. Check 'sudo systemctl status apache2'."
    fi
    if [[ -n "$HTTP_LEN" && "$HTTP_LEN" != "$LOCAL_SIZE" ]]; then
        die "Apache Content-Length ($HTTP_LEN) != local file size ($LOCAL_SIZE) for $f — truncation or rewrite. Check mod_cache / mod_deflate."
    fi
    log "  HTTP $f: ${HTTP_LEN:-$LOCAL_SIZE} bytes, code 200 — OK"
done

# 2026-05-19 final-state guard: in past incidents (founder, Dell Latitude 5500,
# kernel 6.12.32, BIOS 1.43.1) a stale post-INSTALL hook reverted the merged
# initrd back to the original cpio+xz layout AFTER all the publish steps had
# already verified MERGED mode. Catch that class of bug by re-reading the
# active initrd.img one last time and asserting its magic matches the profile
# we just chose. Mode "merged" MUST land on disk as a single gzip archive
# (1f 8b). If something rewrote it back to cpio (070701) or xz (fd 37 7a),
# halt with a loud error instead of letting the bench panic at boot.
if [[ "$INITRD_PROFILE" == MERGED* ]]; then
    ACTIVE_MAGIC=$(head -c 4 "$PXE_ROOT/initrd.img" | od -An -tx1 | tr -d ' \n')
    if [[ "$ACTIVE_MAGIC" != 1f8b* ]]; then
        die "FINAL CHECK FAILED — initrd.img magic is '$ACTIVE_MAGIC' (expected '1f8b...' gzip after MERGE). Something reverted the Dell-safe merged file after we built it. Hunt for stale post-INSTALL hooks: 'find / -name \"*.sh\" -newer /var/log/apache2 -exec grep -l \"hotfix\\|restored original\" {} \\;'. Bench will panic at boot until this is found."
    fi
    log "  initrd.img final magic: $ACTIVE_MAGIC (gzip) — OK ✓"
fi

# --- 3. Generate the iPXE boot script ---------------------------------------
# Keep the kernel cmdline IDENTICAL to what we patch into the on-ISO grub menu
# in step 5b of the build script. That way PXE boot and USB boot behave
# exactly the same — same auto-run, same batch flag, same locale skip.
BOOT_IPXE="$PXE_ROOT/boot.ipxe"
KCMD_BODY="boot=live union=overlay username=user config components quiet loglevel=3 panic=15 noswap consoleblank=0 nomodeset net.ifnames=0 usbcore.autosuspend=-1 ethdevice-timeout=120 ethdev-dhcp-max-loop=40 e1000e.SmartPowerDownEnable=0 pcie_aspm=off fetch=http://${SERVER_IP}/vstl-pxe/filesystem.squashfs ocs_live_run=/opt/vstl/vstl-bench-entry.sh ocs_live_batch=yes ocs_live_extra_param= ocs_lang=en_US.UTF-8 ocs_live_keymap=NONE keyboard-layouts=NONE locales=en_US.UTF-8 ocs_live_run_tty=/dev/tty1 noprompt"
# iPXE tells live-boot which firmware NIC fetched the kernel. Keep this fixed
# to net0, matching the main server route that actually reaches Linux ipconfig
# on the USB-C test adapters. BOOTIF must use PXELINUX's hyphenated MAC form
# (01-aa-bb-cc-dd-ee-ff). Do not pass ip=* here: this live-boot parser treats
# it as static networking and sets NODHCP=true. Let live-boot DHCP the adapter.
IPXE_SELECT_PXE_NIC=$(cat <<'IPXESELECT'
# VSTL main-compatible PXE NIC handoff. Do not probe alternate iPXE NICs here:
# Type-C adapters on .45 froze after initrd when this block tried net1/net2
# before kernel handoff. Linux-side recovery still handles late USB NICs.
set vstl_bootif ${net0/mac:hexhyp}
set vstl_live_netdev ${net0/mac}
echo VSTL Linux handoff NIC MAC: ${vstl_live_netdev}
IPXESELECT
)
KCMD_IPXE_BODY="BOOTIF=01-\${vstl_bootif} live-netdev=\${vstl_live_netdev} ${KCMD_BODY}"
OLD_DELL_AUDIO_PARAMS="${VSTL_OLD_DELL_AUDIO_PARAMS:-snd_intel_dspcfg.dsp_driver=1}"
if [[ -n "$OLD_DELL_AUDIO_PARAMS" ]]; then
    OLD_DELL_KCMD_IPXE_BODY="BOOTIF=01-\${vstl_bootif} live-netdev=\${vstl_live_netdev} ${OLD_DELL_AUDIO_PARAMS} ${KCMD_BODY}"
else
    OLD_DELL_KCMD_IPXE_BODY="${KCMD_IPXE_BODY}"
fi
OLD_DELL_BOOT_IPXE="$PXE_ROOT/boot-old-dell.ipxe"
OLD_DELL_UEFI_LOADER="${VSTL_OLD_DELL_SNPNAME:-vstl-old-dell-snponly.efi}"
if [[ -f "$PXE_ROOT/initrd-vstl-fixed.cpio" ]]; then
    OLD_DELL_BOOT_LABEL="VSTL OLD-DELL RAW CPIO BOOT"
    OLD_DELL_INITRD_NAME="initrd-vstl-fixed.cpio"
    OLD_DELL_INITRD_BYTES=$(stat -c '%s' "$PXE_ROOT/initrd-vstl-fixed.cpio" 2>/dev/null || echo 0)
    OLD_DELL_KERNEL_LINE="kernel http://${SERVER_IP}/vstl-pxe/vmlinuz ${OLD_DELL_KCMD_IPXE_BODY}"
    OLD_DELL_INITRD_LINE="initrd http://${SERVER_IP}/vstl-pxe/initrd-vstl-fixed.cpio"
    OLD_DELL_NOTE="Runtime-proven raw cpio handoff without an initrd kernel argument."
else
    OLD_DELL_BOOT_LABEL="VSTL OLD-DELL FALLBACK GZIP BOOT"
    OLD_DELL_INITRD_NAME="initrd.img"
    OLD_DELL_INITRD_BYTES=$(stat -c '%s' "$PXE_ROOT/initrd.img" 2>/dev/null || echo 0)
    OLD_DELL_KERNEL_LINE="kernel http://${SERVER_IP}/vstl-pxe/vmlinuz initrd=initrd.img ${OLD_DELL_KCMD_IPXE_BODY}"
    OLD_DELL_INITRD_LINE="initrd http://${SERVER_IP}/vstl-pxe/initrd.img"
    OLD_DELL_NOTE="Fallback named gzip initrd because raw cpio is unavailable."
fi

# Latitude 5470/5480/5490/5500 firmware is sensitive to the initrd handoff.
# Keep these systems on the same raw cpio route proven by the production path
# whenever available; fall back to named gzip only if that raw image is absent.
cat > "$OLD_DELL_BOOT_IPXE" <<OLDDELL
#!ipxe
echo
echo ============================================================
echo ${OLD_DELL_BOOT_LABEL}
echo FOG http://${SERVER_IP}/vstl-pxe
echo Initrd: ${OLD_DELL_INITRD_NAME} (${OLD_DELL_INITRD_BYTES} bytes)
echo ${OLD_DELL_NOTE}
echo ============================================================
echo
echo Checking firmware PXE network handle...
ifstat
ifopen net0 || echo net0 unavailable or already open
isset \${net0/ip} || dhcp net0 || echo net0 DHCP retry failed
ifopen net1 || echo net1 unavailable or already open
isset \${net1/ip} || dhcp net1 || echo net1 DHCP retry skipped or failed
echo net0 IP: \${net0/ip}
echo net1 IP: \${net1/ip}
echo
imgfree
${IPXE_SELECT_PXE_NIC}
${OLD_DELL_KERNEL_LINE}
${OLD_DELL_INITRD_LINE}
imgstat
boot || echo Old-Dell boot failed - press any key for shell && shell
OLDDELL
chmod 644 "$OLD_DELL_BOOT_IPXE"

# Build dedicated first-stage EFI loaders for UEFI firmware that works best
# with SNP. Generic FOG snponly.efi can embed
# `chain tftp://${next-server}/default.ipxe`, which may send benches back into
# an upstream default iPXE path before VSTL ever starts. The embedded scripts
# below jump straight to the VSTL HTTP scripts while still reusing the firmware
# NIC driver.
GENERIC_UEFI_LOADER="${VSTL_GENERIC_SNPNAME:-snponly.efi}"
GENERIC_UEFI_ALIAS="${VSTL_GENERIC_SNP_ALIAS:-vstl-snponly.efi}"
GENERIC_EMBED_SCRIPT=$(mktemp -t vstl-generic-embed-XXXX.ipxe)
OLD_DELL_EMBED_SCRIPT=$(mktemp -t vstl-old-dell-embed-XXXX.ipxe)
GENERIC_STAGE1_EFI="$TFTP_ROOT/$GENERIC_UEFI_LOADER"
OLD_DELL_STAGE1_EFI="$TFTP_ROOT/$OLD_DELL_UEFI_LOADER"
IPXE_CLONE_ROOT=""
IPXE_SRC_DIR=""
normalize_ipxe_src_dir() {
    local candidate="$1"
    if [[ -f "$candidate/Makefile" && -d "$candidate/config" ]]; then
        printf '%s\n' "$candidate"
        return 0
    fi
    if [[ -f "$candidate/src/Makefile" && -d "$candidate/src/config" ]]; then
        printf '%s\n' "$candidate/src"
        return 0
    fi
    return 1
}
cleanup_ipxe_embeds() {
    rm -f "$GENERIC_EMBED_SCRIPT" "$OLD_DELL_EMBED_SCRIPT"
    [[ -n "$IPXE_CLONE_ROOT" ]] && rm -rf "$IPXE_CLONE_ROOT"
}
trap cleanup_ipxe_embeds EXIT
for candidate in \
    "${VSTL_IPXE_SRC:-}" \
    /opt/fogproject/src/ipxe \
    /opt/fogproject/src/ipxe/src \
    /opt/fogproject/packages/ipxe \
    /opt/fogproject/packages/ipxe/src \
    /usr/src/ipxe/src \
    /usr/src/ipxe
do
    [[ -n "$candidate" && -d "$candidate" ]] || continue
    IPXE_SRC_DIR="$(normalize_ipxe_src_dir "$candidate" || true)"
    [[ -n "$IPXE_SRC_DIR" ]] || continue
    break
done

cat > "$GENERIC_EMBED_SCRIPT" <<EMBED
#!ipxe
ifopen net0 || echo net0 already open
isset \${net0/ip} || dhcp net0 || echo net0 DHCP retry failed
chain --autofree http://${SERVER_IP}/vstl-pxe/boot.ipxe || shell
EMBED

cat > "$OLD_DELL_EMBED_SCRIPT" <<EMBED
#!ipxe
ifopen net0 || echo net0 already open
isset \${net0/ip} || dhcp net0 || echo net0 DHCP retry failed
chain --autofree http://${SERVER_IP}/vstl-pxe/boot-old-dell.ipxe || shell
EMBED

show_ipxe_build_logs() {
    local log_file
    for log_file in /tmp/vstl-generic-ipxe-build.log /tmp/vstl-old-dell-ipxe-build.log; do
        [[ -s "$log_file" ]] && cat "$log_file" >&2
    done
}

build_embedded_stage1() {
    local src_dir="$1"
    local loader_label="$2"
    local embed_script="$3"
    local stage1_efi="$4"
    local expected_url="$5"
    local log_file="$6"
    local loader_path="$src_dir/bin-x86_64-efi/snponly.efi"
    log "Building dedicated $loader_label EFI first-stage loader ($(basename "$stage1_efi")) from $src_dir ..."
    rm -f "$loader_path"
    if ! make -C "$src_dir" bin-x86_64-efi/snponly.efi EMBED="$embed_script" >"$log_file" 2>&1; then
        return 1
    fi
    install -m 0644 "$loader_path" "$stage1_efi"
    # Do not use `strings | grep -q` under pipefail here. When grep exits
    # early after finding the match, strings can receive SIGPIPE and make the
    # whole pipeline look failed even though the embedded URL is present.
    if ! grep -aFq "$expected_url" "$stage1_efi" 2>/dev/null; then
        die "$stage1_efi was built without the expected embedded VSTL HTTP chain: $expected_url"
    fi
    if grep -aFq 'default.ipxe' "$stage1_efi" 2>/dev/null; then
        die "$stage1_efi still contains a default.ipxe chain; refusing to publish a broken loader"
    fi
    return 0
}

build_all_embedded_stage1() {
    local src_dir="$1"
    build_embedded_stage1 \
        "$src_dir" \
        "generic UEFI" \
        "$GENERIC_EMBED_SCRIPT" \
        "$GENERIC_STAGE1_EFI" \
        "http://${SERVER_IP}/vstl-pxe/boot.ipxe" \
        /tmp/vstl-generic-ipxe-build.log || return 1
    build_embedded_stage1 \
        "$src_dir" \
        "old-Dell" \
        "$OLD_DELL_EMBED_SCRIPT" \
        "$OLD_DELL_STAGE1_EFI" \
        "http://${SERVER_IP}/vstl-pxe/boot-old-dell.ipxe" \
        /tmp/vstl-old-dell-ipxe-build.log || return 1
    if [[ -n "$GENERIC_UEFI_ALIAS" && "$GENERIC_UEFI_ALIAS" != "$GENERIC_UEFI_LOADER" ]]; then
        install -m 0644 "$GENERIC_STAGE1_EFI" "$TFTP_ROOT/$GENERIC_UEFI_ALIAS"
    fi
}

if [[ -n "$IPXE_SRC_DIR" ]] && ! build_all_embedded_stage1 "$IPXE_SRC_DIR"; then
    warn "Existing iPXE source tree at $IPXE_SRC_DIR could not build VSTL embedded EFI loaders."
    show_ipxe_build_logs
    IPXE_SRC_DIR=""
fi

if [[ -z "$IPXE_SRC_DIR" ]]; then
    IPXE_CLONE_ROOT=$(mktemp -d -t vstl-ipxe-XXXX)
    if command -v git >/dev/null; then
        log "Trying a clean upstream iPXE clone for embedded EFI build fallback ..."
        if ! git clone --depth 1 https://github.com/ipxe/ipxe.git "$IPXE_CLONE_ROOT" >/tmp/vstl-old-dell-ipxe-build.log 2>&1; then
            show_ipxe_build_logs
            die "Failed to clone a clean iPXE source tree for VSTL embedded EFI loaders"
        fi
        IPXE_SRC_DIR="$IPXE_CLONE_ROOT/src"
    elif command -v curl >/dev/null && command -v tar >/dev/null; then
        log "Downloading a clean upstream iPXE tarball for embedded EFI build fallback ..."
        if ! curl -fsSL https://github.com/ipxe/ipxe/archive/refs/heads/master.tar.gz -o "$IPXE_CLONE_ROOT/ipxe.tar.gz" >/tmp/vstl-old-dell-ipxe-build.log 2>&1; then
            show_ipxe_build_logs
            die "Failed to download a clean iPXE tarball for VSTL embedded EFI loaders"
        fi
        tar -xzf "$IPXE_CLONE_ROOT/ipxe.tar.gz" -C "$IPXE_CLONE_ROOT"
        IPXE_SRC_DIR="$(normalize_ipxe_src_dir "$IPXE_CLONE_ROOT/ipxe-master" || true)"
    else
        die "Could not find a buildable iPXE source tree and neither git nor curl+tar are available for fallback."
    fi
    [[ -n "$IPXE_SRC_DIR" ]] || die "Clean iPXE fallback tree is missing its src/ build directory."
    if ! build_all_embedded_stage1 "$IPXE_SRC_DIR"; then
        show_ipxe_build_logs
        die "Failed to build VSTL embedded EFI loaders from the clean upstream iPXE fallback tree"
    fi
fi
log "  published $GENERIC_STAGE1_EFI"
if [[ -n "$GENERIC_UEFI_ALIAS" && "$GENERIC_UEFI_ALIAS" != "$GENERIC_UEFI_LOADER" ]]; then
    log "  published $TFTP_ROOT/$GENERIC_UEFI_ALIAS"
fi
log "  published $OLD_DELL_STAGE1_EFI"
rm -f /tmp/vstl-generic-ipxe-build.log /tmp/vstl-old-dell-ipxe-build.log
cleanup_ipxe_embeds
trap - EXIT

# NOTE: iPXE preserves double-quotes when passing kernel cmdline arguments,
# so `ocs_live_keymap="NONE"` lands on the kernel as the literal string
# `"NONE"` (with the quote chars), which Clonezilla's parser does NOT
# recognize — falls back to the interactive keyboard/language wizard.
# We intentionally emit UNQUOTED values here; Clonezilla's 3.2.x docs use
# the same unquoted form for `ocs_live_*` params.
# The USB boot path patches grub.cfg instead, where shell-quoted values
# ARE interpreted correctly by GRUB before reaching the kernel.
cat > "$BOOT_IPXE" <<IPXE
#!ipxe
# ----------------------------------------------------------------------------
# VSTL Imaging — Live PXE Boot
# Auto-generated by 06_setup_pxe_netboot.sh. Re-run that script to refresh.
# ----------------------------------------------------------------------------
# 2026-06-08 - production handoff.
# Pass the selected production initrd by name in the kernel cmdline and load
# the same image in iPXE. The default production image is the gzip initrd.img.
echo
echo ==============================================================
echo   VSTL Imaging — booting from FOG http://${SERVER_IP}
echo   If this address is NOT your authorised VSTL FOG server,
echo   POWER OFF the bench immediately and contact admin.
echo   Production initrd: ${PRODUCTION_INITRD_NAME} (${PRODUCTION_INITRD_SIZE} bytes)
echo   Expected initrd SHA-prefix: ${INITRD_SHA_PREFIX}
echo ==============================================================
echo
echo   Loading VSTL Imaging Live kernel over HTTP ...
${IPXE_SELECT_PXE_NIC}
kernel http://${SERVER_IP}/vstl-pxe/vmlinuz initrd=${PRODUCTION_INITRD_NAME} ${KCMD_IPXE_BODY}
echo   Loading VSTL Imaging Live initrd (${PRODUCTION_INITRD_LABEL}) ...
${PRODUCTION_INITRD_LINES}
echo   Loaded images:
imgstat
boot || echo Boot failed - press any key to return to menu && shell
IPXE
chmod 644 "$BOOT_IPXE"

# Testing-server universal Type-C mode: keep old-Dell MAC-tagged boots on the
# exact same script. This prevents dnsmasq/iPXE product or MAC routing from
# bypassing the Type-C-safe compressed initrd handoff.
if [[ "$PXE_BOOT_STYLE" == "universal-typec-gzip" ]]; then
    install -m 0644 "$BOOT_IPXE" "$OLD_DELL_BOOT_IPXE"
fi

# TFTP copy for FOG iPXE menu `kernel tftp://...` users who prefer TFTP over HTTP.
install -m 0644 "$BOOT_IPXE" "$TFTP_ROOT/vstl.ipxe"

# 2026-05-13 Phase-2 boot-speed fix: drop an autoexec.ipxe shim at both the
# TFTP root AND the HTTP /vstl-pxe/ root so iPXE doesn't waste ~10 seconds
# at startup waiting for the default `autoexec.ipxe` download to time out
# (see https://ipxe.org/2d12618e). The shim immediately chains to our
# real boot script — no operator interaction needed.
AUTOEXEC_IPXE="$PXE_ROOT/autoexec.ipxe"
cat > "$AUTOEXEC_IPXE" <<AUTO
#!ipxe
# VSTL fast-path — auto-generated. Skips the iPXE startup HTTP timeout.
# VSTL-FOG-AUTOEXEC-MARKER fog_ip=${SERVER_IP} initrd_sha_prefix=${INITRD_SHA_PREFIX}
# (2026-05-20 anti-rogue-PXE marker: tftp-get /autoexec.ipxe from any
# PXE server on the LAN; if the marker line doesn't match THIS FOG IP
# it's a rogue responder. See tools/find_rogue_pxe.sh.)
ifopen net0 || echo net0 already open
echo Reusing firmware PXE network lease when available.
isset \${net0/ip} || dhcp net0 || echo net0 DHCP retry failed; continuing with existing firmware state
ifstat
chain --autofree http://${SERVER_IP}/vstl-pxe/boot.ipxe || goto retry
:retry
echo VSTL HTTP chain failed; retrying in 3 seconds...
sleep 3
isset \${net0/ip} || dhcp net0 || echo net0 DHCP retry failed again
chain --autofree http://${SERVER_IP}/vstl-pxe/boot.ipxe || goto retry
AUTO
chmod 644 "$AUTOEXEC_IPXE"
install -m 0644 "$AUTOEXEC_IPXE" "$TFTP_ROOT/autoexec.ipxe"
install -m 0644 "$AUTOEXEC_IPXE" "$TFTP_ROOT/default.ipxe"

# Legacy BIOS PXE fallback. Some Latitude 5490 units report architecture 0
# and are served pxelinux.0 even when the boot menu label says UEFI IPv4.
# Keep this path on the same selected production initrd as iPXE.
install -d -m 0755 "$TFTP_ROOT/vstl-pxe" "$TFTP_ROOT/pxelinux.cfg"
install -m 0444 "$PRODUCTION_INITRD_SOURCE" "$TFTP_ROOT/vstl-pxe/$PRODUCTION_INITRD_NAME"
cat > "$TFTP_ROOT/pxelinux.cfg/default" <<PXELINUX
DEFAULT vstl
PROMPT 0
TIMEOUT 1
ONTIMEOUT vstl

LABEL vstl
  SAY VSTL 360 - Legacy PXELINUX ${PRODUCTION_INITRD_NAME} boot
  LINUX /vstl-pxe/vmlinuz
  INITRD /vstl-pxe/${PRODUCTION_INITRD_NAME}
  APPEND initrd=${PRODUCTION_INITRD_NAME} ${KCMD_BODY}
PXELINUX
chmod 644 "$TFTP_ROOT/pxelinux.cfg/default"

# 2026-05-20 — DIAGNOSTIC boot variants for the persistent Dell Latitude 5500
# `Initramfs unpacking failed: invalid magic` panic class. The merged
# initrd.img passes every server-side structural check (gzip OK, 1 valid
# newc cpio TRAILER, eof_delta=0, /init present, file count > 0) and is
# delivered byte-identical over HTTP, yet kernel 6.12.32-amd64 still
# panics on this hardware. To isolate the failure plane:
#
#   • boot-diag-imgstat.ipxe — identical boot path to boot.ipxe but adds
#     `imgstat` + a 30 s prompt AFTER kernel+initrd are loaded, so the
#     operator can read the iPXE image table on screen and confirm the
#     loaded initrd size in iPXE memory equals the expected file size
#     (rules out iPXE truncation or padding on the EFI handoff).
#
#   • boot-diag-cpio.ipxe — same path but loads the UNCOMPRESSED merged
#     cpio (initrd.cpio) instead of initrd.img (gzip). Kernel auto-detects
#     `070701` magic and unpacks without going through the gzip
#     decompressor. If the bench boots with this variant, the bug is in
#     the gzip/kernel handoff format. If it still panics, the bug is in
#     the cpio content or the kernel's initramfs parser itself — not in
#     compression or transport.
#
# Both variants generate the same kernel cmdline + squashfs fetch as
# boot.ipxe — only the initrd= name and initrd URL differ.
# Operator workflow (run on FOG server, no rebuild needed):
#   sudo cp /var/www/html/vstl-pxe/boot.ipxe /var/www/html/vstl-pxe/boot.ipxe.bak
#   sudo cp /var/www/html/vstl-pxe/boot-diag-imgstat.ipxe /var/www/html/vstl-pxe/boot.ipxe
#   # PXE-boot bench, read imgstat output, photograph screen
#   sudo cp /var/www/html/vstl-pxe/boot-diag-cpio.ipxe /var/www/html/vstl-pxe/boot.ipxe
#   # PXE-boot bench, observe whether uncompressed cpio boots cleanly
#   sudo cp /var/www/html/vstl-pxe/boot.ipxe.bak /var/www/html/vstl-pxe/boot.ipxe
INITRD_IMG_BYTES=$(stat -c '%s' "$PXE_ROOT/initrd.img" 2>/dev/null || echo 0)
INITRD_CPIO_BYTES=$(stat -c '%s' "$PXE_ROOT/initrd.cpio" 2>/dev/null || echo 0)

# Shared kernel cmdline body (sans the leading `initrd=NAME`). Kept on one
# line so iPXE preserves token spacing exactly as the kernel parser expects.
KCMD_BODY="boot=live union=overlay username=user config components quiet loglevel=3 panic=15 noswap consoleblank=0 nomodeset net.ifnames=0 usbcore.autosuspend=-1 ethdevice-timeout=120 ethdev-dhcp-max-loop=40 e1000e.SmartPowerDownEnable=0 pcie_aspm=off fetch=http://${SERVER_IP}/vstl-pxe/filesystem.squashfs ocs_live_run=/opt/vstl/vstl-bench-entry.sh ocs_live_batch=yes ocs_live_extra_param= ocs_lang=en_US.UTF-8 ocs_live_keymap=NONE keyboard-layouts=NONE locales=en_US.UTF-8 ocs_live_run_tty=/dev/tty1 noprompt"
IPXE_SELECT_PXE_NIC=$(cat <<'IPXESELECT'
# VSTL main-compatible PXE NIC handoff. Do not probe alternate iPXE NICs here:
# Type-C adapters on .45 froze after initrd when this block tried net1/net2
# before kernel handoff. Linux-side recovery still handles late USB NICs.
set vstl_bootif ${net0/mac:hexhyp}
set vstl_live_netdev ${net0/mac}
echo VSTL Linux handoff NIC MAC: ${vstl_live_netdev}
IPXESELECT
)
KCMD_IPXE_BODY="BOOTIF=01-\${vstl_bootif} live-netdev=\${vstl_live_netdev} ${KCMD_BODY}"

DIAG_IMGSTAT="$PXE_ROOT/boot-diag-imgstat.ipxe"
cat > "$DIAG_IMGSTAT" <<DIAGIMG
#!ipxe
# ----------------------------------------------------------------------------
# VSTL DIAGNOSTIC — imgstat pause before kernel handoff
# Auto-generated by 06_setup_pxe_netboot.sh.
# ----------------------------------------------------------------------------
echo
echo ==============================================================
echo   VSTL DIAGNOSTIC BOOT  (imgstat pause)
echo   FOG: http://${SERVER_IP}
echo   Expected initrd.img: ${INITRD_IMG_BYTES} bytes (SHA ${INITRD_SHA_PREFIX})
echo ==============================================================
echo
echo   Loading kernel ...
${IPXE_SELECT_PXE_NIC}
kernel http://${SERVER_IP}/vstl-pxe/vmlinuz initrd=initrd.img ${KCMD_IPXE_BODY}
echo   Loading initrd.img (gzip-wrapped merged cpio) ...
initrd http://${SERVER_IP}/vstl-pxe/initrd.img
echo
echo === iPXE image table (imgstat below) ===
imgstat
echo === end imgstat ===
echo
echo   If 'size:' for initrd.img is NOT ${INITRD_IMG_BYTES} bytes, iPXE truncated the transfer.
echo   If 'size:' matches but kernel still panics, the bug is past iPXE in the kernel decoder.
echo
prompt --timeout 30000 Press any key to continue boot (auto-boot in 30s) ... && boot
echo   Boot returned. Dropping to iPXE shell.
shell
DIAGIMG
chmod 644 "$DIAG_IMGSTAT"

DIAG_CPIO="$PXE_ROOT/boot-diag-cpio.ipxe"
cat > "$DIAG_CPIO" <<DIAGCPIO
#!ipxe
# ----------------------------------------------------------------------------
# VSTL DIAGNOSTIC — uncompressed cpio initrd (bypasses gzip handoff)
# Auto-generated by 06_setup_pxe_netboot.sh.
# ----------------------------------------------------------------------------
echo
echo ==============================================================
echo   VSTL DIAGNOSTIC BOOT  (uncompressed cpio)
echo   FOG: http://${SERVER_IP}
echo   initrd.cpio (raw): ${INITRD_CPIO_BYTES} bytes
echo ==============================================================
echo
echo   Loading kernel ...
${IPXE_SELECT_PXE_NIC}
kernel http://${SERVER_IP}/vstl-pxe/vmlinuz initrd=initrd.cpio ${KCMD_IPXE_BODY}
echo   Loading initrd.cpio (uncompressed newc cpio) ...
initrd http://${SERVER_IP}/vstl-pxe/initrd.cpio
echo
echo === iPXE image table (imgstat below) ===
imgstat
echo === end imgstat ===
echo
echo   If bench boots from here, the bug is in the gzip/kernel handoff.
echo   If bench still panics, the bug is in cpio content or the kernel parser.
echo
prompt --timeout 30000 Press any key to continue boot (auto-boot in 30s) ... && boot
echo   Boot returned. Dropping to iPXE shell.
shell
DIAGCPIO
chmod 644 "$DIAG_CPIO"

# Sanity: only the cpio variant requires initrd.cpio to exist. If MERGE was
# skipped (SINGLE_CPIO / GZIP / XZ / ZSTD / SPLIT modes), strip the cpio
# variant so the operator doesn't chain to a 404.
if [[ ! -f "$PXE_ROOT/initrd.cpio" ]]; then
    rm -f "$DIAG_CPIO"
    log "  (diagnostic cpio variant skipped — no merged cpio available in this mode)"
fi

log "  diagnostic boot scripts published:"
log "    http://${SERVER_IP}/vstl-pxe/boot-diag-imgstat.ipxe  (imgstat pause)"
[[ -f "$DIAG_CPIO" ]] && log "    http://${SERVER_IP}/vstl-pxe/boot-diag-cpio.ipxe     (uncompressed cpio)"

# Production raw CPIO override. Keep the first generated boot.ipxe above as a
# compatibility fallback while the selected production style decides the active
# script written here.
if [[ "$PXE_BOOT_STYLE" == "classic-raw-cpio" && -f "$PXE_ROOT/initrd-vstl-fixed.cpio" ]]; then
    RAW_CPIO="$PXE_ROOT/initrd-vstl-fixed.cpio"
    RAW_CPIO_BYTES=$(stat -c '%s' "$RAW_CPIO" 2>/dev/null || echo 0)
    RAW_CPIO_SHA_PREFIX=$(sha256sum "$RAW_CPIO" 2>/dev/null | cut -c1-12 || echo "unknown")

    cat > "$BOOT_IPXE" <<CLASSIC
#!ipxe
isset \${product} || set product UNKNOWN
iseq "\${product}" "Latitude 5470" && chain --autofree http://${SERVER_IP}/vstl-pxe/boot-old-dell.ipxe || echo -n
iseq "\${product}" "Latitude 5480" && chain --autofree http://${SERVER_IP}/vstl-pxe/boot-old-dell.ipxe || echo -n
iseq "\${product}" "Latitude 5490" && chain --autofree http://${SERVER_IP}/vstl-pxe/boot-old-dell.ipxe || echo -n
iseq "\${product}" "Latitude 5500" && chain --autofree http://${SERVER_IP}/vstl-pxe/boot-old-dell.ipxe || echo -n
# VSTL production boot - runtime-proven raw cpio initrd handoff
# Auto-generated by 06_setup_pxe_netboot.sh. Re-run that script to refresh.
echo
echo ============================================================
echo VSTL VERIFIED RAW CPIO BOOT
echo FOG http://${SERVER_IP}/vstl-pxe
echo Expected initrd: ${RAW_CPIO_BYTES} bytes SHA ${RAW_CPIO_SHA_PREFIX}
echo Runtime-proven classic iPXE handoff without an initrd kernel argument.
echo ============================================================
echo
ifstat
echo Cleaning stale autoexec script image before kernel handoff ...
imgfree autoexec.ipxe || echo autoexec.ipxe was not present/freeable
${IPXE_SELECT_PXE_NIC}
echo Loading VSTL kernel ...
kernel http://${SERVER_IP}/vstl-pxe/vmlinuz ${KCMD_IPXE_BODY}
echo Loading runtime-proven unified raw cpio initrd ...
initrd http://${SERVER_IP}/vstl-pxe/initrd-vstl-fixed.cpio
echo Loaded images:
imgstat
boot || echo Boot failed - press any key for shell && shell
CLASSIC
    chmod 644 "$BOOT_IPXE"
    install -m 0644 "$BOOT_IPXE" "$TFTP_ROOT/vstl.ipxe"

cat > "$AUTOEXEC_IPXE" <<AUTO
#!ipxe
echo VSTL PXE HTTP chain to FOG ${SERVER_IP}
# Dell 5490-class iPXE can lose the VSTL answer to the upstream DHCP server on
# its second DHCP pass and stop at "Please enter tftp server". Use the reserved
# PXE lease directly; DHCP remains as a fallback only if static assignment fails.
ifopen net0 || echo net0 already open
echo Reusing firmware PXE network lease when available.
isset \${net0/ip} || dhcp net0 || echo net0 DHCP retry failed; continuing with existing firmware state
ifstat
chain --autofree http://${SERVER_IP}/vstl-pxe/boot.ipxe || goto retry
:retry
echo VSTL HTTP chain failed; retrying in 3 seconds...
sleep 3
isset \${net0/ip} || dhcp net0 || echo net0 DHCP retry failed again
chain --autofree http://${SERVER_IP}/vstl-pxe/boot.ipxe || goto retry
AUTO
    chmod 644 "$AUTOEXEC_IPXE"
    install -m 0644 "$AUTOEXEC_IPXE" "$TFTP_ROOT/autoexec.ipxe"
    install -m 0644 "$AUTOEXEC_IPXE" "$TFTP_ROOT/default.ipxe"
    log "  production boot style: classic raw-cpio handoff (${RAW_CPIO_BYTES} bytes, SHA ${RAW_CPIO_SHA_PREFIX})"
elif [[ "$PXE_BOOT_STYLE" == "classic-raw-cpio" ]]; then
    warn "Requested classic raw-cpio boot style, but initrd-vstl-fixed.cpio was not produced; keeping compressed boot.ipxe."
fi

# --- 4. Quick HTTP self-test ------------------------------------------------
log "Smoke-testing HTTP reachability from this host ..."
DIAG_FILES="boot-old-dell.ipxe boot-diag-imgstat.ipxe"
[[ -f "$PXE_ROOT/initrd.cpio" ]] && DIAG_FILES="$DIAG_FILES initrd.cpio boot-diag-cpio.ipxe"
[[ -f "$PXE_ROOT/initrd-vstl-fixed.cpio" ]] && DIAG_FILES="$DIAG_FILES initrd-vstl-fixed.cpio"
for f in vmlinuz initrd.img filesystem.squashfs boot.ipxe $DIAG_FILES; do
    if curl -fsI "http://${SERVER_IP}/vstl-pxe/$f" -o /dev/null; then
        log "  OK  http://${SERVER_IP}/vstl-pxe/$f"
    else
        warn "  FAIL http://${SERVER_IP}/vstl-pxe/$f (Apache may need to be enabled / restarted)"
    fi
done

# --- 5. Firewall (Apache=80, TFTP=69) ---------------------------------------
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 80/tcp     comment 'FOG Apache / VSTL-PXE' >/dev/null 2>&1 || true
    ufw allow 69/udp     comment 'TFTP'                  >/dev/null 2>&1 || true
fi

# --- 6. Done ----------------------------------------------------------------
SHA=$(sha256sum "$PXE_ROOT/filesystem.squashfs" | awk '{print $1}')
SIZE=$(du -sh "$PXE_ROOT" | awk '{print $1}')

cat <<BANNER

${GREEN}========================================================================
PXE NETBOOT READY
========================================================================${NC}

HTTP assets    : http://${SERVER_IP}/vstl-pxe/
iPXE script    : http://${SERVER_IP}/vstl-pxe/boot.ipxe
TFTP copy      : tftp://${SERVER_IP}/vstl.ipxe
Total size     : ${SIZE}
squashfs SHA   : ${SHA}

${YELLOW}Next step${NC} — one-time FOG webUI configuration (takes 2 minutes):

  1. Open http://${SERVER_IP}/fog/management
  2. Log in (default fog / password — change it if you haven't).
  3. Top menu → iPXE → New Menu Entry
  4. Fill in:
       Menu Item   :  vstl.live.netboot
       Description :  VSTL Imaging — Live Netboot
       Parameters  :
         chain http://\${fog-ip}/vstl-pxe/boot.ipxe
       Menu Show with : All Hosts
       Default Item   : ☑ (if you want F12 → PXE → straight-to-VSTL)
  5. Click Add.

Bench test:
  * Plug a bench laptop into the same LAN as ${SERVER_IP}.
  * Power on → F12 (HP / Dell) or F9 (some HP) → PXE IPv4 / IPv6.
  * FOG iPXE menu appears → pick "VSTL Imaging — Live Netboot"
    (or skip the menu entirely if you ticked Default Item).
  * Laptop fetches vmlinuz + initrd over HTTP → kernel comes up →
    squashfs streams on demand → VSTL client runs → auto-poweroff.

Watch the boot live from this host with:
    sudo journalctl -u dnsmasq -f            # PXE DHCP requests
    sudo tail -f /var/log/apache2/access.log # HTTP asset fetches

Re-run this script any time you rebuild the ISO:
    sudo ./06_setup_pxe_netboot.sh

${YELLOW}Diagnostic boot variants${NC} (use when bench panics with
"Initramfs unpacking failed: invalid magic at start of compressed archive"):

  imgstat variant — shows iPXE image table before kernel handoff:
    sudo cp ${PXE_ROOT}/boot.ipxe ${PXE_ROOT}/boot.ipxe.bak
    sudo cp ${PXE_ROOT}/boot-diag-imgstat.ipxe ${PXE_ROOT}/boot.ipxe
    # PXE-boot the bench; read 'size:' line in imgstat output:
    #   matches expected initrd.img bytes -> iPXE handoff is clean
    #   does NOT match                    -> iPXE truncated the transfer
    sudo cp ${PXE_ROOT}/boot.ipxe.bak ${PXE_ROOT}/boot.ipxe   # revert

  uncompressed cpio variant — bypasses the gzip handoff layer:
    sudo cp ${PXE_ROOT}/boot-diag-cpio.ipxe ${PXE_ROOT}/boot.ipxe
    # PXE-boot the bench:
    #   boots cleanly -> bug is in gzip/kernel decompressor handoff
    #   still panics  -> bug is in cpio CONTENT or kernel initramfs parser
    sudo cp ${PXE_ROOT}/boot.ipxe.bak ${PXE_ROOT}/boot.ipxe   # revert

BANNER
