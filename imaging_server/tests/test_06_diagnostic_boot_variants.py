"""
Regression — 06_setup_pxe_netboot.sh must publish two diagnostic boot
variants alongside boot.ipxe, plus the uncompressed merged cpio artefact.

Context: 2026-05-20 the founder verified server-side that the merged
initrd.img is structurally valid (1 cpio TRAILER, eof_delta=0, /init
present, byte-identical HTTP delivery) yet the Dell Latitude 5500 STILL
kernel-panics with "Initramfs unpacking failed: invalid magic at start
of compressed archive". The only remaining failure planes are:
  (a) iPXE -> kernel handoff truncation / padding
  (b) gzip decompressor on the kernel side
  (c) cpio content the kernel parser rejects

To isolate (a) from (b)+(c) and (b) from (c), the script publishes:
  - boot-diag-imgstat.ipxe  -> normal boot + `imgstat` + 30 s prompt so
    the operator can read the loaded initrd size from iPXE memory
  - boot-diag-cpio.ipxe     -> loads initrd.cpio (uncompressed merged
    cpio) instead of initrd.img, bypassing the gzip decompressor
  - initrd.cpio             -> the uncompressed cpio artefact, written
    alongside initrd.img inside the MERGE python heredoc

This test pins all three contracts at static-source level so a future
contributor who deletes the diagnostic block is caught immediately.
"""
import hashlib
import lzma
from pathlib import Path

SCRIPT = (Path(__file__).parent.parent / "06_setup_pxe_netboot.sh").read_text(encoding="utf-8")
E1000E_OVERRIDE = (
    Path(__file__).parent.parent
    / "bench-client"
    / "kmods"
    / "6.12.32-amd64"
    / "e1000e.ko.xz"
)
E1000E_OVERRIDE_SHA256 = "214b2285026f16ece07e568d0d589fff298b3bedb1db801900c7fbb9b7fa36bd"


def test_merge_block_also_publishes_uncompressed_initrd_cpio():
    """The MERGE python heredoc must write initrd.cpio alongside initrd.img."""
    # Must reference the cpio_path variable
    assert 'cpio_path = os.path.join(os.path.dirname(src), "initrd.cpio")' in SCRIPT
    # Must write merged_raw (the uncompressed cpio bytes) to it
    assert 'open(cpio_path, "wb").write(merged_raw)' in SCRIPT
    # Must chmod read-only like initrd.img
    assert 'os.chmod(cpio_path, 0o444)' in SCRIPT


def test_imgstat_diagnostic_boot_script_is_generated():
    """boot-diag-imgstat.ipxe heredoc must exist and run imgstat + prompt."""
    assert 'DIAG_IMGSTAT="$PXE_ROOT/boot-diag-imgstat.ipxe"' in SCRIPT
    assert "cat > \"$DIAG_IMGSTAT\" <<DIAGIMG" in SCRIPT
    # Must include imgstat before boot
    assert "imgstat" in SCRIPT
    # Must include a prompt with timeout so operator can read the table
    assert "prompt --timeout 30000" in SCRIPT
    # Must load the gzip directly, with no trailing argument that would make
    # iPXE prepend a generated CPIO wrapper.
    assert "initrd http://${SERVER_IP}/vstl-pxe/initrd.img" in SCRIPT


def test_cpio_diagnostic_boot_script_is_generated():
    """boot-diag-cpio.ipxe must load uncompressed initrd.cpio."""
    assert 'DIAG_CPIO="$PXE_ROOT/boot-diag-cpio.ipxe"' in SCRIPT
    assert "cat > \"$DIAG_CPIO\" <<DIAGCPIO" in SCRIPT
    # Kernel cmdline must point at initrd.cpio, not initrd.img
    assert "initrd=initrd.cpio" in SCRIPT
    assert "initrd http://${SERVER_IP}/vstl-pxe/initrd.cpio" in SCRIPT


def test_raw_cpio_initrd_is_default_production_boot_style():
    """Production must mirror the main server raw CPIO path by default.
    The same Lenovo/USB-C adapter that hangs on the testing server boots from
    the main server with this classic raw CPIO route, so keep it as the default
    and reserve dynamic/gzip handoff for diagnostics or explicit override."""
    assert 'PXE_BOOT_STYLE="${VSTL_PXE_BOOT_STYLE:-classic-raw-cpio}"' in SCRIPT
    assert 'open(stage2_gz_path, "wb").write(gzip.compress(stage2_raw, compresslevel=6, mtime=0))' in SCRIPT
    assert 'runtime_raw_path = os.path.join(os.path.dirname(src), "initrd-vstl-fixed.cpio")' in SCRIPT
    assert '["find", ".", "-print0"]' in SCRIPT
    assert 'open(runtime_raw_path, "wb").write(merged_raw)' in SCRIPT
    assert "for ext in gz xz zst cpio; do" in SCRIPT
    assert 'STAGE2_INITRD="initrd-stage2.${ext}"' in SCRIPT
    assert 'PRODUCTION_INITRD_NAME="initrd.img"' in SCRIPT
    assert "kernel http://${SERVER_IP}/vstl-pxe/vmlinuz initrd=${PRODUCTION_INITRD_NAME} ${KCMD_IPXE_BODY}" in SCRIPT
    assert "${PRODUCTION_INITRD_LINES}" in SCRIPT
    assert "imgargs vmlinuz initrd=initrd.img boot=live union=overlay" not in SCRIPT
    assert "initrd http://${SERVER_IP}/vstl-pxe/initrd.img" in SCRIPT
    assert "initrd --name initrd.img http://${SERVER_IP}/vstl-pxe/initrd.img initrd.img" not in SCRIPT


def test_raw_cpio_production_override_uses_classic_handoff():
    """Raw CPIO production override must not pass an initrd= kernel argument."""
    assert 'if [[ "$PXE_BOOT_STYLE" == "classic-raw-cpio" && -f "$PXE_ROOT/initrd-vstl-fixed.cpio" ]]; then' in SCRIPT
    assert 'RAW_CPIO="$PXE_ROOT/initrd-vstl-fixed.cpio"' in SCRIPT
    assert "VSTL VERIFIED RAW CPIO BOOT" in SCRIPT
    assert 'isset \\${product} || set product UNKNOWN' in SCRIPT
    assert 'iseq "\\${product}" "Latitude 5490" && chain --autofree http://${SERVER_IP}/vstl-pxe/boot-old-dell.ipxe' in SCRIPT
    assert "${IPXE_SELECT_PXE_NIC}" in SCRIPT
    assert "kernel http://${SERVER_IP}/vstl-pxe/vmlinuz ${KCMD_IPXE_BODY}" in SCRIPT
    assert "initrd http://${SERVER_IP}/vstl-pxe/initrd-vstl-fixed.cpio" in SCRIPT
    assert "initrd-vstl-fixed.cpio" in SCRIPT
    raw_start = SCRIPT.index('if [[ "$PXE_BOOT_STYLE" == "classic-raw-cpio" && -f "$PXE_ROOT/initrd-vstl-fixed.cpio" ]]; then')
    raw_end = SCRIPT.index('elif [[ "$PXE_BOOT_STYLE" == "classic-raw-cpio" ]]; then', raw_start)
    assert "initrd --name" not in SCRIPT[raw_start:raw_end]


def test_universal_typec_mode_keeps_old_dell_on_same_boot_script():
    """Testing-server Type-C mode must not let old-Dell MAC tags bypass boot.ipxe."""
    assert 'PXE_BOOT_STYLE" == "universal-typec-gzip"' in SCRIPT
    assert 'install -m 0644 "$BOOT_IPXE" "$OLD_DELL_BOOT_IPXE"' in SCRIPT


def test_autoexec_reuses_firmware_lease_without_static_or_second_dhcp():
    """Generated iPXE shims must not force a fixed bench IP or DHCP again.
    The firmware PXE stage already has a working VSTL lease; a second iPXE DHCP
    pass can be won by the upstream network DHCP server and lose the boot path."""
    assert "VSTL_PXE_STATIC_IP" not in SCRIPT
    assert "set net0/ip" not in SCRIPT
    assert "set net0/netmask" not in SCRIPT
    assert "set net0/gateway" not in SCRIPT
    assert "Reusing firmware PXE network lease when available." in SCRIPT
    assert "chain --autofree http://${SERVER_IP}/vstl-pxe/boot.ipxe || goto retry" in SCRIPT
    assert "dhcp net0 || goto retry" not in SCRIPT
    assert "dhcp net0 || shell" not in SCRIPT


def test_pxelinux_fallback_uses_selected_production_initrd():
    """Legacy BIOS/PXELINUX fallback must match the selected production initrd."""
    assert 'install -m 0444 "$PRODUCTION_INITRD_SOURCE" "$TFTP_ROOT/vstl-pxe/$PRODUCTION_INITRD_NAME"' in SCRIPT
    assert "VSTL 360 - Legacy PXELINUX ${PRODUCTION_INITRD_NAME} boot" in SCRIPT
    assert "INITRD /vstl-pxe/${PRODUCTION_INITRD_NAME}" in SCRIPT
    assert "APPEND initrd=${PRODUCTION_INITRD_NAME} ${KCMD_BODY}" in SCRIPT


def test_kernel_panic_auto_reboots_for_kvm_recovery():
    """If the live kernel panics before the TUI starts, the bench must reboot
    by itself so operators are not stuck at an uncontrollable KVM panic screen."""
    assert "panic=15" in SCRIPT
    assert "quiet loglevel=3 panic=15 noswap" in SCRIPT


def test_kernel_cmdline_disables_console_blanking():
    """Long erase operations must not let the Linux console blank the display."""
    assert "consoleblank=0" in SCRIPT
    assert "panic=15 noswap consoleblank=0 nomodeset" in SCRIPT


def test_cpio_variant_is_stripped_when_merge_did_not_run():
    """If MERGE mode was skipped (SINGLE_CPIO/GZIP/XZ/ZSTD/SPLIT), the cpio
    variant must be deleted to prevent the operator from chaining to a 404."""
    assert 'if [[ ! -f "$PXE_ROOT/initrd.cpio" ]]; then' in SCRIPT
    assert "rm -f \"$DIAG_CPIO\"" in SCRIPT


def test_http_smoke_test_covers_the_diagnostic_files():
    """The HTTP self-test loop must reach the diagnostic files too, so a
    silent 403/404 from Apache misconfiguration is caught at INSTALL time."""
    assert 'DIAG_FILES="boot-old-dell.ipxe boot-diag-imgstat.ipxe"' in SCRIPT
    assert '[[ -f "$PXE_ROOT/initrd.cpio" ]] && DIAG_FILES="$DIAG_FILES initrd.cpio boot-diag-cpio.ipxe"' in SCRIPT


def test_old_dell_uses_raw_cpio_initrd_when_available():
    assert 'OLD_DELL_BOOT_IPXE="$PXE_ROOT/boot-old-dell.ipxe"' in SCRIPT
    assert 'OLD_DELL_AUDIO_PARAMS="${VSTL_OLD_DELL_AUDIO_PARAMS:-snd_intel_dspcfg.dsp_driver=1}"' in SCRIPT
    assert 'OLD_DELL_KCMD_IPXE_BODY="BOOTIF=01-\\${vstl_bootif} live-netdev=\\${vstl_live_netdev} ${OLD_DELL_AUDIO_PARAMS} ${KCMD_BODY}"' in SCRIPT
    assert 'OLD_DELL_KCMD_IPXE_BODY="${KCMD_IPXE_BODY}"' in SCRIPT
    assert "snd_intel_dspcfg.dsp_driver=3" not in SCRIPT
    assert "VSTL OLD-DELL RAW CPIO BOOT" in SCRIPT
    assert 'OLD_DELL_KERNEL_LINE="kernel http://${SERVER_IP}/vstl-pxe/vmlinuz ${OLD_DELL_KCMD_IPXE_BODY}"' in SCRIPT
    assert 'OLD_DELL_INITRD_LINE="initrd http://${SERVER_IP}/vstl-pxe/initrd-vstl-fixed.cpio"' in SCRIPT
    assert "Runtime-proven raw cpio handoff without an initrd kernel argument." in SCRIPT
    assert "VSTL OLD-DELL FALLBACK GZIP BOOT" in SCRIPT
    assert 'OLD_DELL_UEFI_LOADER="${VSTL_OLD_DELL_SNPNAME:-vstl-old-dell-snponly.efi}"' in SCRIPT
    assert "vstl-old-dell-snponly.efi" in SCRIPT
    assert "boot-old-dell.ipxe" in SCRIPT


def test_operator_banner_documents_swap_and_revert_one_liners():
    """The completion banner must include the cp-swap-revert flow so the
    operator can run the diagnostic without root-cause-analysing the script."""
    # imgstat variant
    assert "boot-diag-imgstat.ipxe ${PXE_ROOT}/boot.ipxe" in SCRIPT
    # cpio variant
    assert "boot-diag-cpio.ipxe ${PXE_ROOT}/boot.ipxe" in SCRIPT
    # revert path
    assert "${PXE_ROOT}/boot.ipxe.bak ${PXE_ROOT}/boot.ipxe" in SCRIPT


def test_kcmd_body_shared_between_diagnostic_variants():
    """Both diagnostic variants must use the SAME kernel cmdline body as the
    production boot.ipxe — only the initrd=NAME differs. Sharing via a single
    KCMD_BODY shell var prevents drift."""
    assert 'KCMD_BODY="boot=live union=overlay' in SCRIPT
    # The shared body must include the squashfs fetch (else boot fails post-init)
    assert "fetch=http://${SERVER_IP}/vstl-pxe/filesystem.squashfs" in SCRIPT
    # And the Clonezilla ocs_live entry point
    assert "ocs_live_run=/opt/vstl/vstl-bench-entry.sh" in SCRIPT
    assert "ip=dhcp" not in SCRIPT
    assert "ethdevice-timeout=120" in SCRIPT
    assert "ethdev-dhcp-max-loop=40" in SCRIPT
    assert "usbcore.autosuspend=-1" in SCRIPT
    assert "e1000e.SmartPowerDownEnable=0" in SCRIPT
    assert 'KCMD_IPXE_BODY="BOOTIF=01-\\${vstl_bootif} live-netdev=\\${vstl_live_netdev} ${KCMD_BODY}"' in SCRIPT
    assert "VSTL main-compatible PXE NIC handoff" in SCRIPT
    assert "Do not probe alternate iPXE NICs here" in SCRIPT
    assert "VSTL Linux handoff NIC MAC: ${vstl_live_netdev}" in SCRIPT


def test_old_dell_initrd_primes_common_nic_modules_before_live_boot_scan():
    """Old Dell iPXE can use firmware NII while Linux has not loaded the NIC."""
    assert "VSTL network module priming" in SCRIPT
    assert "Dell Latitude firmware can PXE" in SCRIPT
    assert "modprobe e1000e allow_bad_nvm=1" in SCRIPT
    assert "for module in igb igc r8169 r8152" in SCRIPT
    for usb_nic_module in ("r8153_ecm", "cdc_eem", "aqc111", "thunderbolt_net"):
        assert usb_nic_module in SCRIPT
    assert "udevadm trigger --subsystem-match=net --action=add" in SCRIPT


def test_typec_ethernet_pxe_resolves_boot_mac_after_usb_module_settle():
    """USB-C Ethernet can PXE in firmware but appear late inside live-boot."""
    assert "2026-06-26 Lenovo X13 / Type-C Ethernet fix" in SCRIPT
    assert "VSTL BOOTIF/live-netdev resolver" in SCRIPT
    assert "VSTL_LIVENETDEV" in SCRIPT
    assert "set vstl_bootif ${net0/mac:hexhyp}" in SCRIPT
    assert "set vstl_live_netdev ${net0/mac}" in SCRIPT
    assert "Linux-side recovery still handles late USB NICs" in SCRIPT
    assert 'VSTL_TARGET_MAC=$(echo \\"$VSTL_LIVENETDEV\\" | tr \'A-F\' \'a-f\')' in SCRIPT
    assert 'modprobe -q r8152' in SCRIPT
    assert 'modprobe -q ax88179_178a' in SCRIPT
    assert 'VSTL: selected PXE NIC $DEVICE from live-netdev=$VSTL_LIVENETDEV' in SCRIPT


def test_typec_ethernet_resolver_rebinds_usb_nics_before_fallback():
    """HP/Lenovo USB-C PXE can fetch initrd, then Linux sees no IPv4 NIC."""
    assert "Vstl_rebind_known_usb_network_drivers" in SCRIPT
    assert '5|15|30|60)' in SCRIPT
    assert 'DEVICE=$(Vstl_connected_interface \\"$VSTL_TARGET_MAC\\"' in SCRIPT
    assert "selected connected wired NIC $DEVICE while live-netdev=$VSTL_LIVENETDEV was delayed" in SCRIPT


def test_typec_ethernet_driver_stack_covers_common_usb_adapters():
    for module in ("lan78xx", "smsc75xx", "smsc95xx", "rtl8150", "dm9601", "mcs7830", "cdc_subset"):
        assert module in SCRIPT


def test_typec_ethernet_carrier_wait_uses_configured_timeout_not_fixed_15s():
    assert "VSTL carrier timeout extension" in SCRIPT
    assert 'carrier_wait="${ETHDEV_TIMEOUT:-120}"' in SCRIPT
    assert 'for step in $(seq 1 "$carrier_wait")' in SCRIPT


def test_typec_ethernet_carrier_wait_recovers_stuck_usb_driver():
    """A USB-C NIC can exist as eth0 while carrier remains stuck after UEFI PXE."""
    assert "VSTL USB-C NIC recovery" in SCRIPT
    assert 'readlink -f "/sys/class/net/$interface/device"' in SCRIPT
    assert "driver_path/unbind" in SCRIPT
    assert "driver_path/bind" in SCRIPT
    assert 'Vstl_interface_for_mac "$target_mac"' in SCRIPT
    assert "VSTL: network interface returned as" in SCRIPT
    assert "5|30|60|90)" in SCRIPT


def test_typec_ethernet_carrier_wait_tracks_reenumerated_wired_interfaces():
    assert "Vstl_connected_interface" in SCRIPT
    assert 'case "$candidate" in lo|wl*|ww*|p2p*) continue' in SCRIPT
    assert 'live-netdev=*:*) target_mac=' in SCRIPT
    assert 'connected_interface=$(Vstl_connected_interface "$target_mac"' in SCRIPT
    assert "Vstl_rebind_known_usb_network_drivers" in SCRIPT
    assert "VSTL: Linux network interfaces during PXE handoff" in SCRIPT
    assert "VSTL: USB network recovery completed" in SCRIPT


def test_typec_ethernet_raises_runtime_nic_despite_firmware_mac_passthrough():
    assert 'Vstl_nic_power_on "$candidate"' in SCRIPT
    assert "Carrier is reported as \"unknown\"" in SCRIPT
    assert "Lenovo MAC pass-through" in SCRIPT
    assert "Always reset" in SCRIPT


def test_i219_lm4_bad_nvm_override_is_abi_pinned_and_integrity_guarded():
    compressed = E1000E_OVERRIDE.read_bytes()
    assert hashlib.sha256(compressed).hexdigest() == E1000E_OVERRIDE_SHA256
    module = lzma.decompress(compressed)
    assert b"allow_bad_nvm" in module
    assert b"8086:15d7" in module
    assert b"vermagic=6.12.32-amd64 SMP preempt mod_unload modversions " in module
    assert E1000E_OVERRIDE_SHA256 in SCRIPT
    assert "e1000e_override_sha256_mismatch" in SCRIPT


def test_old_dell_initrd_recovers_firmware_owned_pci_nic_and_emits_diagnostics():
    assert "starting PCI recovery" in SCRIPT
    assert 'echo 1 > /sys/bus/pci/rescan' in SCRIPT
    assert 'modprobe -R \\"$modalias\\"' in SCRIPT
    assert "VSTL NIC DIAGNOSTIC" in SCRIPT


def test_no_ipxe_echo_line_starts_with_dash():
    """iPXE's `echo` parses any first argument starting with `-` as an
    option, so lines like `echo --- foo ---` get rejected with
        Unrecognised option "---"
        Usage: echo [-n|--n] [...]
        Could not boot: Invalid argument
    Founder hit this LIVE on the bench (2026-05-20 23:46 IST) — iPXE
    1.21.1+ on Dell Latitude 5500 bailed before reaching `imgstat`.
    The contract is: no `echo` line in ANY iPXE heredoc body may start
    its first printed token with `-`. Use `=`, `.`, or descriptive text
    instead. This static-source guard parses every `echo ...` line in
    the diagnostic heredoc blocks and asserts the rule.
    """
    bad = []
    in_ipxe_heredoc = False
    current_delim = None
    for ln in SCRIPT.splitlines():
        # Detect start of an iPXE heredoc (DIAGIMG, DIAGCPIO, IPXE, AUTO).
        # We scan ALL of them — production boot.ipxe must obey the rule too.
        stripped = ln.strip()
        if not in_ipxe_heredoc:
            for tag in ("DIAGIMG", "DIAGCPIO", "IPXE", "AUTO"):
                if stripped.endswith(f"<<{tag}") or stripped.endswith(f"<<'{tag}'"):
                    in_ipxe_heredoc = True
                    current_delim = tag
                    break
            continue
        if stripped == current_delim:
            in_ipxe_heredoc = False
            current_delim = None
            continue
        # Inside an iPXE heredoc — check echo lines.
        if stripped.startswith("echo "):
            arg = stripped[len("echo "):].lstrip()
            if arg.startswith("-"):
                bad.append(ln)
    assert not bad, (
        "iPXE `echo` first-arg starts with `-` (will be rejected as an "
        "option, bench will bail). Use `=` or descriptive text instead. "
        f"Offending lines: {bad!r}"
    )


def test_prompt_command_in_diagnostic_blocks():
    """Both diagnostic variants must use `prompt --timeout 30000` BEFORE
    `boot` so the operator has time to read imgstat output. If a future
    contributor removes the prompt, the kernel takes over immediately
    and the diagnostic value is lost."""
    # Two occurrences — one per heredoc
    assert SCRIPT.count("prompt --timeout 30000") >= 2
