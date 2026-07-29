import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NETWORK_PATH = ROOT / "bench-client" / "vstl_network_setup.py"
USB_BUILD = (ROOT / "04_build_usb_iso.sh").read_text(encoding="utf-8")
ISO_BUILD = (ROOT / "04_build_live_iso_clonezilla.sh").read_text(encoding="utf-8")
PERMS_HELPER = (ROOT / "tools" / "live_rootfs_permissions.sh").read_text(encoding="utf-8")

spec = importlib.util.spec_from_file_location("vstl_network_setup", NETWORK_PATH)
network = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(network)


def test_server_host_prefers_explicit_ip_and_falls_back_to_fog_url():
    assert network.server_host({"VSTL_SERVER_IP": "10.1.2.3"}) == "10.1.2.3"
    assert network.server_host({"FOG_SERVER": "http://10.255.0.75/fog"}) == "10.255.0.75"
    assert network.server_host({}) == "10.255.0.75"


def test_nmcli_parser_handles_escaped_colon_and_keeps_strongest_bss():
    raw = (
        r"Bench\:WiFi:45:WPA2" "\n"
        r"Bench\:WiFi:82:WPA2" "\n"
        "Open Network:61:--\n"
        ":99:WPA2\n"
    )
    assert network.parse_nmcli_networks(raw) == [
        ("Bench:WiFi", 82, "WPA2"),
        ("Open Network", 61, "--"),
    ]


def test_interface_detection_separates_usb_ethernet_and_wifi(tmp_path):
    sys_net = tmp_path / "net"
    (sys_net / "lo").mkdir(parents=True)
    (sys_net / "eth0" / "device").mkdir(parents=True)
    (sys_net / "enx001122" / "device").mkdir(parents=True)
    (sys_net / "wlan0" / "device").mkdir(parents=True)
    (sys_net / "wlan0" / "wireless").mkdir()

    assert network.wired_interfaces(sys_net) == ["enx001122", "eth0"]
    assert network.wifi_interfaces(sys_net) == ["wlan0"]


def test_pxe_boot_mac_accepts_live_netdev_and_bootif_formats(tmp_path):
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(
        "BOOTIF=01-aa-bb-cc-dd-ee-ff live-netdev=11:22:33:44:55:66",
        encoding="utf-8",
    )
    assert network.pxe_boot_mac(cmdline) == "11:22:33:44:55:66"

    cmdline.write_text("BOOTIF=01-aa-bb-cc-dd-ee-ff", encoding="utf-8")
    assert network.pxe_boot_mac(cmdline) == "aa:bb:cc:dd:ee:ff"


def test_wired_order_prefers_typec_pxe_adapter_mac(tmp_path):
    sys_net = tmp_path / "net"
    for name, mac, carrier in (
        ("eth0", "aa:aa:aa:aa:aa:aa", "1"),
        ("enx112233445566", "11:22:33:44:55:66", "0"),
    ):
        iface = sys_net / name
        (iface / "device").mkdir(parents=True)
        (iface / "address").write_text(mac, encoding="utf-8")
        (iface / "carrier").write_text(carrier, encoding="utf-8")
        (iface / "operstate").write_text("down", encoding="utf-8")

    ordered = network.ordered_wired_interfaces(
        ["eth0", "enx112233445566"],
        target_mac="11:22:33:44:55:66",
        sys_class_net=sys_net,
    )
    assert ordered[0] == "enx112233445566"


def test_full_iso_build_includes_usb_network_dependencies():
    assert "vstl_network_setup.py" in ISO_BUILD
    for package in (
        "network-manager",
        "wpasupplicant",
        "rfkill",
        "iw",
        "isc-dhcp-client",
        "firmware-iwlwifi",
        "wireless-tools",
        "fonts-terminus",
    ):
        assert package in ISO_BUILD


def test_wifi_interface_detection_falls_back_to_iw_and_nmcli(tmp_path, monkeypatch):
    sys_net = tmp_path / "net"
    sys_net.mkdir()
    calls = []

    def fake_run(argv, timeout=30):
        calls.append(argv)
        class Result:
            returncode = 0
            stdout = ""
        result = Result()
        if argv[:2] == ["iw", "dev"]:
            result.stdout = "phy#0\n\tInterface wlan0\n\t\ttype managed\n"
        return result

    monkeypatch.setattr(network, "run", fake_run)
    assert network.wifi_interfaces(sys_net) == ["wlan0"]
    assert ["iw", "dev"] in calls


def test_prompt_wifi_starts_network_manager_before_wifi_detection(monkeypatch):
    order = []
    monkeypatch.setattr(network, "start_network_manager", lambda: order.append("start") or True)
    monkeypatch.setattr(network, "wifi_interfaces", lambda: order.append("wifi") or [])
    monkeypatch.setattr(network, "hold_status", lambda message, seconds=3: None)

    assert network.prompt_wifi("10.255.0.75") is False
    assert order == ["start", "wifi"]


def test_iw_parser_exposes_named_networks():
    raw = """
BSS 11:22:33:44:55:66(on wlan0)
    signal: -48.00 dBm
    SSID: Bench WiFi
    RSN:
BSS aa:bb:cc:dd:ee:ff(on wlan0)
    signal: -71.00 dBm
    SSID: Guest
"""
    networks = network.parse_iw_networks(raw)
    assert networks[0] == ("Bench WiFi", 100, "WPA")
    assert networks[1][0] == "Guest"


def test_network_status_is_held_for_technician_visibility():
    source = NETWORK_PATH.read_text(encoding="utf-8")
    assert "STATUS_HOLD_SECONDS = 3.0" in source
    assert "Wired connection successful" in source
    assert "Wi-Fi connected successfully" in source


def test_usb_builder_preserves_hybrid_boot_and_verifies_runtime():
    assert '-boot_image any replay' in USB_BUILD
    assert 'vstl-usb-live-amd64.iso' in USB_BUILD
    assert 'vstl_network_setup.py' in USB_BUILD
    assert "report_el_torito" in USB_BUILD
    assert "USB ISO contains a stale network helper" in USB_BUILD
    assert "VSTL_WIFI_PROMPT=1" in USB_BUILD
    assert "Source ISO lacks Intel iwlwifi firmware" in USB_BUILD
    assert "USB ISO is missing mandatory QC test" in USB_BUILD


def test_wifi_start_loads_intel_ax201_driver_stack():
    source = NETWORK_PATH.read_text(encoding="utf-8")
    for module in ("cfg80211", "mac80211", "iwlwifi", "iwlmvm"):
        assert f'"{module}"' in source


def test_wired_start_primes_common_typec_ethernet_driver_stack():
    source = NETWORK_PATH.read_text(encoding="utf-8")
    for module in (
        "r8152",
        "r8153_ecm",
        "cdc_ether",
        "cdc_ncm",
        "ax88179_178a",
        "aqc111",
        "thunderbolt_net",
    ):
        assert f'"{module}"' in source
    assert "wait_for_wired_interfaces" in source
    assert "PXE boot adapter MAC" in source


def test_usb_builder_injects_large_console_fonts():
    assert "usr/share/consolefonts" in USB_BUILD
    assert "TerminusBold32x16" in USB_BUILD
    assert "Lat2-VGA32x16" in USB_BUILD


def test_usb_builder_replaces_clonezilla_menus_with_direct_vstl_boot():
    assert 'menuentry "VSTL 360 Bench Imaging"' in USB_BUILD
    assert "set timeout=0" in USB_BUILD
    assert "DEFAULT vstl" in USB_BUILD
    assert "PROMPT 0" in USB_BUILD
    assert '"/boot/grub/grub.cfg"' in USB_BUILD
    assert '"/syslinux/isolinux.cfg"' in USB_BUILD
    assert '"/syslinux/syslinux.cfg"' in USB_BUILD
    assert "Clonezilla boot menu remains in the USB ISO" in USB_BUILD


def test_live_rootfs_repair_preserves_sudo_and_user_home():
    assert "repair_live_rootfs_permissions()" in PERMS_HELPER
    assert 'chmod 4755 "$rootfs/usr/bin/sudo"' in PERMS_HELPER
    assert 'chown root:root "$rootfs/etc/sudo.conf"' in PERMS_HELPER
    assert 'chmod 0440 "$rootfs/etc/sudoers"' in PERMS_HELPER
    assert 'chown -R 1000:1000 "$rootfs/home/user"' in PERMS_HELPER


def test_iso_builders_repair_live_rootfs_before_squashfs():
    assert '. "$SCRIPT_DIR/tools/live_rootfs_permissions.sh"' in USB_BUILD
    assert 'repair_live_rootfs_permissions "$WORK_DIR/rootfs"' in USB_BUILD
    assert '. "$SCRIPT_DIR/tools/live_rootfs_permissions.sh"' in ISO_BUILD
    assert 'repair_live_rootfs_permissions "$WORK_DIR/rootfs"' in ISO_BUILD
