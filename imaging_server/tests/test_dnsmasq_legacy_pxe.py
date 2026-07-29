from pathlib import Path


ROOT = Path(__file__).parent.parent
DNSMASQ = (ROOT / "03_dnsmasq_proxydhcp.conf").read_text(encoding="utf-8")
INSTALLER = (ROOT / "02_install_dnsmasq.sh").read_text(encoding="utf-8")
PXE_PUBLISH = (ROOT / "06_setup_pxe_netboot.sh").read_text(encoding="utf-8")


def test_pxe_dhcp_avoids_dnsmasq_pxe_service_menu_for_old_dells():
    """Dell Latitude 5490 UEFI PXE accepts DHCP but can stop before TFTP when
    dnsmasq sends PXE service/menu metadata. Use direct bootfile fields only."""
    assert "dhcp-no-override" in DNSMASQ
    assert "dhcp-pxe-vendor=PXEClient,HW-Client" in DNSMASQ
    assert "dhcp-match=set:vstl_pxe,option:vendor-class,MSFT 5.0" not in DNSMASQ
    assert "dhcp-ignore=tag:old_dell_uefi,tag:normal_windows" in DNSMASQ
    assert "pxe-service=tag:" not in DNSMASQ
    assert 'pxe-service=x86PC,"VSTL Imaging Network Boot",undionly.kpxe' not in DNSMASQ


def test_uefi_bootfile_uses_firmware_snp_and_direct_http_reentry():
    """SNP-only iPXE avoids native NIC probing hangs, then receives HTTP boot."""
    assert "dhcp-match=set:ipxe,175" in DNSMASQ
    assert "dhcp-boot=tag:efi64,tag:!old_dell_uefi,snponly.efi,vstl-imaging,10.255.0.75" in DNSMASQ
    assert "dhcp-boot=tag:efix64,tag:!old_dell_uefi,snponly.efi,vstl-imaging,10.255.0.75" in DNSMASQ
    assert "dhcp-boot=tag:ipxe,tag:!old_dell_uefi,http://10.255.0.75/vstl-pxe/boot.ipxe,vstl-imaging,10.255.0.75" in DNSMASQ
    assert "dhcp-boot=tag:ipxe,tag:old_dell_uefi,http://10.255.0.75/vstl-pxe/boot-old-dell.ipxe,vstl-imaging,10.255.0.75" in DNSMASQ
    assert "dhcp-match=set:normal_windows,option:vendor-class,MSFT 5.0" in DNSMASQ
    assert "dhcp-ignore=tag:old_dell_uefi,tag:normal_windows" in DNSMASQ


def test_known_old_dell_prefix_uses_firmware_snp_loader():
    """The bench Latitude 5490 NIC prefix is kept explicit for future tuning.
    Use a dedicated embedded SNP-only first stage so old Dell firmware jumps
    directly to VSTL instead of upstream default.ipxe."""
    assert "dhcp-mac=set:old_dell_uefi,10:65:30:*:*:*" in DNSMASQ
    assert "dhcp-boot=tag:old_dell_uefi,vstl-old-dell-snponly.efi,vstl-imaging,10.255.0.75" in DNSMASQ
    assert "dhcp-option=tag:!ipxe,tag:old_dell_uefi,option:bootfile-name,vstl-old-dell-snponly.efi" in DNSMASQ
    assert "dhcp-host=10:65:30:66:c1:3b" not in DNSMASQ


def test_known_old_dell_ipxe_second_pass_stays_on_vstl_dhcp():
    """Known Latitude NIC families must retain the VSTL tag even when their
    second-stage iPXE DHCP request omits PXEClient/option 175 metadata."""
    for prefix in ("10:65:30", "8c:04:ba", "e4:b9:7a", "98:e7:43", "c8:f7:50", "34:48:ed"):
        assert f"dhcp-mac=set:old_dell_uefi,{prefix}:*:*:*" in DNSMASQ
        assert f"dhcp-mac=set:vstl_pxe,{prefix}:*:*:*" in DNSMASQ
    assert not any(
        line.strip().startswith(("dhcp-boot=", "dhcp-option="))
        and "10.255.0.1/default.ipxe" in line
        for line in DNSMASQ.splitlines()
    )


def test_pxe_only_rescue_leases_are_scoped_to_pxeclient():
    """Older Dell Latitude UEFI PXE may ignore proxyDHCP metadata. Give only
    PXE firmware/iPXE a short real lease; never run general bench DHCP."""
    assert "dhcp-match=set:vstl_pxe,option:vendor-class,PXEClient" in DNSMASQ
    assert "dhcp-match=set:vstl_pxe,option:vendor-class,HW-Client" in DNSMASQ
    assert "dhcp-match=set:vstl_pxe,175" in DNSMASQ
    assert "dhcp-range=tag:vstl_pxe,10.255.0.80,10.255.0.89,255.255.255.0,2m" in DNSMASQ
    assert "dhcp-option=tag:vstl_pxe,option:router,10.255.0.1" in DNSMASQ
    assert "dhcp-ignore=tag:!vstl_pxe" in DNSMASQ
    assert "no-ping" in DNSMASQ
    assert "dhcp-range=10.255.0.0,proxy" not in DNSMASQ
    assert "dhcp-range=10.255.254.251,10.255.254.252" not in DNSMASQ
    assert 'pxe-prompt=' not in DNSMASQ
    assert 'VSTL_PXE_DHCP_START="${VSTL_PXE_DHCP_START:-10.255.0.80}"' in INSTALLER
    assert 'VSTL_PXE_DHCP_END="${VSTL_PXE_DHCP_END:-10.255.0.89}"' in INSTALLER
    assert "PXE DHCP range: $VSTL_PXE_DHCP_START - $VSTL_PXE_DHCP_END" in INSTALLER


def test_installer_can_pin_or_auto_select_uefi_loader():
    """02_install_dnsmasq.sh should let .env pin the loader, otherwise keep
    the VSTL embedded loaders as the safe defaults."""
    assert 'VSTL_UEFI_BOOTFILE=ipxe.efi' in INSTALLER
    assert 'VSTL_UEFI_BOOTFILE=ipxe.efi, intel.efi, vstl-clean-ipxe.efi, or snponly.efi' in INSTALLER
    assert 'UEFI_BOOTFILE="snponly.efi"' in INSTALLER
    assert 'OLD_DELL_UEFI_BOOTFILE="vstl-old-dell-snponly.efi"' in INSTALLER
    assert 'elif [[ -f "$TFTP_ROOT/snponly.efi" ]]; then' not in INSTALLER
    assert 's|^dhcp-boot=tag:efi64,tag:!old_dell_uefi,.*$|dhcp-boot=tag:efi64,tag:!old_dell_uefi,$UEFI_BOOTFILE_ESC,vstl-imaging,$SERVER_IP_ESC|' in INSTALLER
    assert 's|^dhcp-option=tag:!ipxe,tag:efi64,tag:!old_dell_uefi,option:bootfile-name,.*$|dhcp-option=tag:!ipxe,tag:efi64,tag:!old_dell_uefi,option:bootfile-name,$UEFI_BOOTFILE_ESC|' in INSTALLER
    assert 's|^dhcp-boot=tag:old_dell_uefi,.*$|dhcp-boot=tag:old_dell_uefi,$OLD_DELL_UEFI_BOOTFILE_ESC,vstl-imaging,$SERVER_IP_ESC|' in INSTALLER
    assert 's|^dhcp-option=tag:!ipxe,tag:old_dell_uefi,option:bootfile-name,.*$|dhcp-option=tag:!ipxe,tag:old_dell_uefi,option:bootfile-name,$OLD_DELL_UEFI_BOOTFILE_ESC|' in INSTALLER
    assert 'UEFI bootfile:  $UEFI_BOOTFILE' in INSTALLER
    assert 'Old Dell UEFI:  $OLD_DELL_UEFI_BOOTFILE' in INSTALLER
    assert 'TFTP $TFTP_ROOT/snponly.efi is not VSTL-embedded yet.' in INSTALLER


def test_installer_replaces_current_and_legacy_default_server_ip():
    """The bundled config may carry either the old or current default IP.
    Reinstalling must still substitute SERVER_IP from .env."""
    assert 's/10\\\\.255\\\\.254\\\\.75/$SERVER_IP_ESC/g' in INSTALLER
    assert 's/10\\\\.255\\\\.0\\\\.75/$SERVER_IP_ESC/g' in INSTALLER
    assert "contains stale 10.255.254.75 embedded script text" in INSTALLER


def test_old_dell_pxe_gets_explicit_tftp_and_bootfile_options():
    """Latitude 5490 UEFI requests DHCP options 66/67. Include them explicitly
    in addition to BOOTP sname/file so firmware proceeds to TFTP."""
    assert "dhcp-option=tag:!ipxe,tag:vstl_pxe,option:tftp-server,10.255.0.75" in DNSMASQ
    assert "dhcp-option=tag:!ipxe,tag:vstl_pxe,option:tftp-server-address,10.255.0.75" in DNSMASQ
    assert "dhcp-option=tag:!ipxe,tag:bios,option:bootfile-name,pxelinux.0" in DNSMASQ
    assert "dhcp-option=tag:!ipxe,tag:efi64,tag:!old_dell_uefi,option:bootfile-name,snponly.efi" in DNSMASQ
    assert "dhcp-option=tag:!ipxe,tag:efix64,tag:!old_dell_uefi,option:bootfile-name,snponly.efi" in DNSMASQ


def test_ipxe_second_dhcp_pass_gets_direct_http_script():
    """After the EFI loader starts iPXE, advertise the final HTTP script in
    option 67. Keep requested boot-server metadata, but avoid default.ipxe."""
    assert "dhcp-match=set:ipxe,175" in DNSMASQ
    assert "dhcp-match=set:vstl_pxe,175" in DNSMASQ
    assert "dhcp-userclass=set:ipxe,iPXE" in DNSMASQ
    assert "dhcp-userclass=set:vstl_pxe,iPXE" in DNSMASQ
    assert "dhcp-authoritative" in DNSMASQ
    assert "dhcp-boot=tag:ipxe,tag:!old_dell_uefi,http://10.255.0.75/vstl-pxe/boot.ipxe,vstl-imaging,10.255.0.75" in DNSMASQ
    assert "dhcp-boot=tag:ipxe,tag:old_dell_uefi,http://10.255.0.75/vstl-pxe/boot-old-dell.ipxe,vstl-imaging,10.255.0.75" in DNSMASQ
    assert "dhcp-option=tag:ipxe,tag:!old_dell_uefi,option:bootfile-name,http://10.255.0.75/vstl-pxe/boot.ipxe" in DNSMASQ
    assert "dhcp-option=tag:ipxe,tag:old_dell_uefi,option:bootfile-name,http://10.255.0.75/vstl-pxe/boot-old-dell.ipxe" in DNSMASQ
    assert "dhcp-boot=tag:ipxe,default.ipxe" not in DNSMASQ
    assert "dhcp-option=tag:ipxe,option:tftp-server,10.255.0.75" in DNSMASQ
    assert "dhcp-option=tag:ipxe,option:tftp-server-address,10.255.0.75" in DNSMASQ
    assert "snponly.efi,vstl-imaging,10.255.0.75" in DNSMASQ


def test_pxe_publish_builds_embedded_uefi_first_stages():
    """UEFI PXE must receive dedicated SNP-only EFI loaders that embed VSTL
    HTTP chains instead of generic default.ipxe."""
    assert 'GENERIC_UEFI_LOADER="${VSTL_GENERIC_SNPNAME:-snponly.efi}"' in PXE_PUBLISH
    assert 'GENERIC_UEFI_ALIAS="${VSTL_GENERIC_SNP_ALIAS:-vstl-snponly.efi}"' in PXE_PUBLISH
    assert 'OLD_DELL_UEFI_LOADER="${VSTL_OLD_DELL_SNPNAME:-vstl-old-dell-snponly.efi}"' in PXE_PUBLISH
    assert 'chain --autofree http://${SERVER_IP}/vstl-pxe/boot.ipxe || shell' in PXE_PUBLISH
    assert 'chain --autofree http://${SERVER_IP}/vstl-pxe/boot-old-dell.ipxe || shell' in PXE_PUBLISH
    assert 'normalize_ipxe_src_dir()' in PXE_PUBLISH
    assert 'Trying a clean upstream iPXE clone for embedded EFI build fallback' in PXE_PUBLISH
    assert 'git clone --depth 1 https://github.com/ipxe/ipxe.git "$IPXE_CLONE_ROOT"' in PXE_PUBLISH
    assert 'build_embedded_stage1()' in PXE_PUBLISH
    assert 'build_all_embedded_stage1()' in PXE_PUBLISH
    assert 'install -m 0644 "$GENERIC_STAGE1_EFI" "$TFTP_ROOT/$GENERIC_UEFI_ALIAS"' in PXE_PUBLISH
    assert 'still contains a default.ipxe chain; refusing to publish a broken loader' in PXE_PUBLISH


def test_installer_keeps_tftp_option_negotiation_for_old_dell_roms():
    """Latitude 5490 UEFI accepts negotiated blksize/tsize TFTP for EFI loaders
    and aborts when tftpd-hpa refuses those options."""
    assert "TFTPD_COMPAT_OPTIONS='-s'" in INSTALLER
    assert "aborts if tftpd-hpa refuses options" in INSTALLER
    assert 'TFTP_OPTIONS=\\"$TFTPD_COMPAT_OPTIONS\\"' in INSTALLER
    assert 'printf \'TFTP_OPTIONS="%s"\\n\' "$TFTPD_COMPAT_OPTIONS"' in INSTALLER
    assert "systemctl restart tftpd-hpa" in INSTALLER


def test_installer_archives_stale_dnsmasq_backup_configs():
    """dnsmasq reads non-ignored files in /etc/dnsmasq.d, so stale .bak files
    must not remain beside the active VSTL config."""
    assert "vstl-imaging.conf.*" in INSTALLER
    assert "dnsmasq-conf-disabled-" in INSTALLER
    assert "Moving inactive dnsmasq backup out of live config dir" in INSTALLER
