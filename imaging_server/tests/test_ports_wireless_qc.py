import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QC_PATH = ROOT / "bench-client" / "vstl_qc_tests.py"
TUI_TEXT = (ROOT / "bench-client" / "vstl-imaging-tui.py").read_text(encoding="utf-8")
HW_TEXT = (ROOT / "bench-client" / "vstl_hw_detect.py").read_text(encoding="utf-8")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


qc = _load_module("vstl_qc_tests_ports_wireless", QC_PATH)


def _kinds(rows):
    return [row["kind"] for row in rows]


def test_wireless_qc_is_in_order_and_dispatch():
    assert "wireless" in qc.TEST_ORDER
    assert qc.TEST_ORDER.index("wireless") < qc.TEST_ORDER.index("ports")
    assert "wireless" in qc.L1_TESTS
    assert qc.PROBES["wireless"] is qc.probe_wireless
    assert '"wireless":      screen_qc_wireless' in TUI_TEXT


def test_wifi_interface_parser_ignores_p2p_helper(monkeypatch):
    output = """
phy#0
    Interface wlan0
        type managed
    Interface p2p-dev-wlan0
        type P2P-device
"""
    monkeypatch.setattr(qc, "_run", lambda argv, timeout=5: output)
    assert qc.wifi_interfaces() == ["wlan0"]


def test_wifi_scan_retries_and_counts_hidden_bss(monkeypatch):
    calls = []
    scans = iter([
        "",
        "BSS aa:bb:cc:dd:ee:ff(on wlan0)\n\tSSID: ",
    ])

    def fake_run(argv, timeout=5):
        calls.append(argv)
        if argv[:3] == ["iw", "dev", "wlan0"] and argv[-1] == "scan":
            return next(scans)
        if argv[:2] == ["iwlist", "wlan0"]:
            return ""
        return ""

    monkeypatch.setattr(qc, "wifi_interfaces", lambda: ["wlan0"])
    monkeypatch.setattr(qc, "_run", fake_run)
    monkeypatch.setattr(qc.time, "sleep", lambda seconds: None)
    result = qc.scan_wifi_networks(attempts=3)
    assert result["interfaces"] == ["wlan0"]
    assert result["network_count"] == 1
    assert result["hidden_count"] == 1
    assert result["ssids"] == []
    assert result["attempts"] == 2
    assert ["rfkill", "unblock", "wifi"] in calls
    assert ["ip", "link", "set", "wlan0", "up"] in calls


def test_wifi_scan_uses_nmcli_fallback_when_iw_finds_nothing(monkeypatch):
    calls = []

    def fake_run(argv, timeout=5):
        calls.append(argv)
        if argv[:3] == ["iw", "dev", "wlan0"] and argv[-1] == "scan":
            return ""
        if argv[:2] == ["iwlist", "wlan0"]:
            return ""
        if argv[:2] == ["sh", "-c"]:
            return "/usr/bin/nmcli"
        if argv[:3] == ["nmcli", "--terse", "--escape"]:
            return "BenchWiFi:AA\\:BB\\:CC\\:DD\\:EE\\:FF:82\n"
        return ""

    monkeypatch.setattr(qc, "wifi_interfaces", lambda: ["wlan0"])
    monkeypatch.setattr(qc, "_run", fake_run)
    result = qc.scan_wifi_networks(attempts=1)
    assert result["network_count"] == 1
    assert result["ssids"] == ["BenchWiFi"]
    assert any(call[:3] == ["nmcli", "--terse", "--escape"] for call in calls)


def test_wireless_check_passes_when_only_hidden_network_is_seen(monkeypatch):
    monkeypatch.setattr(qc, "scan_wifi_networks", lambda: {
        "interfaces": ["wlan0"],
        "ssids": [],
        "network_count": 1,
        "hidden_count": 1,
        "attempts": 2,
    })
    monkeypatch.setattr(qc, "bluetooth_status", lambda: (True, "hci0"))
    result = qc.run_wireless_check()
    assert result["wifi_ok"] is True
    assert result["ok"] is True
    assert "1 hidden" in result["evidence"]


def test_ports_qc_is_in_l1_and_l2_order():
    assert "ports" in qc.L1_TESTS
    assert "ports" in qc.L2_TESTS
    assert qc.L1_TESTS.index("wireless") < qc.L1_TESTS.index("ports")
    assert qc.TEST_ORDER.index("wireless") < qc.TEST_ORDER.index("ports")
    assert '"ports":         screen_qc_ports' in TUI_TEXT


def test_dmidecode_inventory_uses_external_connector_records(monkeypatch):
    raw = """
Handle 0x0010, DMI type 8, 9 bytes
Port Connector Information
    External Reference Designator: USB Left
    External Connector Type: Access Bus (USB)
    Port Type: USB
Handle 0x0011, DMI type 8, 9 bytes
Port Connector Information
    External Reference Designator: USB Right
    External Connector Type: Access Bus (USB)
    Port Type: USB
Handle 0x0012, DMI type 8, 9 bytes
Port Connector Information
    External Reference Designator: HDMI
    External Connector Type: HDMI
    Port Type: Video Port
Handle 0x0013, DMI type 8, 9 bytes
Port Connector Information
    External Reference Designator: Headset
    External Connector Type: Mini Jack (headphones)
    Port Type: Audio Port
"""
    monkeypatch.setattr(qc, "typec_ports", lambda: [])
    counts = qc.dmidecode_port_counts(raw)
    assert counts == {"usb_a": 2, "hdmi": 1, "audio": 1}


def test_generic_usb_inventory_is_split_using_typec_sysfs(monkeypatch):
    raw = "\n".join(
        f"""Handle 0x00{idx}, DMI type 8, 9 bytes
Port Connector Information
    External Reference Designator: USB {idx}
    External Connector Type: Access Bus (USB)
    Port Type: USB"""
        for idx in range(4)
    )
    monkeypatch.setattr(qc, "typec_ports", lambda: ["port0", "port1"])
    counts = qc.dmidecode_port_counts(raw)
    assert counts["usb_a"] == 2
    assert counts["usb_c"] == 2


def test_dmidecode_inventory_ignores_embedded_usb_peripherals(monkeypatch):
    raw = """
Handle 0x0010, DMI type 8, 9 bytes
Port Connector Information
    External Reference Designator: USB Left
    External Connector Type: Access Bus (USB)
    Port Type: USB
Handle 0x0011, DMI type 8, 9 bytes
Port Connector Information
    External Reference Designator: Internal Webcam
    External Connector Type: Access Bus (USB)
    Port Type: USB
"""
    monkeypatch.setattr(qc, "typec_ports", lambda: [])
    assert qc.dmidecode_port_counts(raw) == {"usb_a": 1}


def test_detect_port_profile_does_not_infer_physical_rj45(monkeypatch):
    monkeypatch.setattr(qc, "dmidecode_port_counts", lambda: {"usb_a": 2})
    monkeypatch.setattr(qc, "external_display_connectors", lambda: [])
    monkeypatch.setattr(qc, "built_in_wired_interfaces", lambda: [])
    monkeypatch.setattr(qc, "audio_jack_present", lambda: False)
    monkeypatch.setattr(qc, "power_supply_mains_nodes", lambda: [])

    rows = qc.detect_port_profile()

    assert [row["kind"] for row in rows] == ["usb_a", "usb_a"]


def test_detect_port_profile_does_not_add_controller_only_rows(monkeypatch):
    monkeypatch.setattr(qc, "dmidecode_port_counts", lambda: {"usb_a": 1})
    monkeypatch.setattr(qc, "external_usb_port_count", lambda: 1)
    monkeypatch.setattr(qc, "typec_ports", lambda: [])
    monkeypatch.setattr(qc, "external_display_connectors", lambda: [])
    monkeypatch.setattr(qc, "built_in_wired_interfaces", lambda: ["eth0"])
    monkeypatch.setattr(qc, "audio_jack_present", lambda: True)
    monkeypatch.setattr(qc, "power_supply_mains_nodes", lambda: ["AC"])
    monkeypatch.setattr(qc, "_power_node_is_usb_c", lambda node: False)

    rows = qc.detect_port_profile()

    assert _kinds(rows) == ["usb_a"]


def test_detect_port_profile_filters_optional_smbios_ports_without_linux_evidence(monkeypatch):
    monkeypatch.setattr(qc, "dmidecode_port_counts", lambda: {
        "usb_a": 2,
        "usb_c": 2,
        "ethernet": 1,
        "audio": 1,
        "hdmi": 1,
        "dc_power": 1,
    })
    monkeypatch.setattr(qc, "typec_ports", lambda: ["port0"])
    monkeypatch.setattr(qc, "external_usb_port_count", lambda: 0)
    monkeypatch.setattr(qc, "external_display_connectors", lambda: [
        {"kind": "hdmi", "name": "card0-HDMI-A-1"},
    ])
    monkeypatch.setattr(qc, "built_in_wired_interfaces", lambda: [])
    monkeypatch.setattr(qc, "audio_jack_present", lambda: False)
    monkeypatch.setattr(qc, "power_supply_mains_nodes", lambda: ["USBC"])
    monkeypatch.setattr(qc, "_power_node_is_usb_c", lambda node: True)

    rows = qc.detect_port_profile()

    assert _kinds(rows) == ["usb_a", "usb_a", "usb_c", "hdmi"]


def test_kernel_usb_topology_overrides_inaccurate_smbios_counts(monkeypatch):
    monkeypatch.setattr(qc, "dmidecode_port_counts", lambda: {"usb_a": 4, "usb_c": 2})
    monkeypatch.setattr(qc, "external_usb_port_count", lambda: 3)
    monkeypatch.setattr(qc, "typec_ports", lambda: ["port0"])
    monkeypatch.setattr(qc, "external_display_connectors", lambda: [])
    monkeypatch.setattr(qc, "built_in_wired_interfaces", lambda: [])
    monkeypatch.setattr(qc, "audio_jack_present", lambda: False)
    monkeypatch.setattr(qc, "power_supply_mains_nodes", lambda: [])

    rows = qc.detect_port_profile()

    assert _kinds(rows) == ["usb_a", "usb_a", "usb_c"]


def test_usb_inventory_uses_conservative_total_when_kernel_overcounts(monkeypatch):
    monkeypatch.setattr(qc, "dmidecode_port_counts", lambda: {"usb_a": 2})
    monkeypatch.setattr(qc, "external_usb_port_count", lambda: 3)
    monkeypatch.setattr(qc, "typec_ports", lambda: [])
    monkeypatch.setattr(qc, "external_display_connectors", lambda: [])
    monkeypatch.setattr(qc, "built_in_wired_interfaces", lambda: [])
    monkeypatch.setattr(qc, "audio_jack_present", lambda: False)
    monkeypatch.setattr(qc, "power_supply_mains_nodes", lambda: [])

    rows = qc.detect_port_profile()

    assert _kinds(rows) == ["usb_a", "usb_a"]


def test_external_usb_count_collapses_usb2_usb3_peer_ports(monkeypatch):
    paths = [
        "/sys/bus/usb/devices/usb1/usb1-port1",
        "/sys/bus/usb/devices/usb2/usb2-port1",
        "/sys/bus/usb/devices/usb1/usb1-port2",
    ]
    monkeypatch.setattr(qc.glob, "glob", lambda pattern: paths if "usb*/" in pattern else [])
    monkeypatch.setattr(qc, "_read_sysfs_text", lambda path: "hotplug")

    peers = {
        paths[0] + "/peer": paths[1],
        paths[1] + "/peer": paths[0],
    }

    def fake_realpath(path):
        path = str(path).replace("\\", "/")
        return peers.get(path, path)

    monkeypatch.setattr(qc.os.path, "realpath", fake_realpath)
    assert qc.external_usb_port_count() == 2


def test_external_usb_count_prefers_hotplug_over_unknown_ports(monkeypatch):
    paths = [
        "/sys/bus/usb/devices/usb1/usb1-port1",
        "/sys/bus/usb/devices/usb1/usb1-port2",
        "/sys/bus/usb/devices/usb1/usb1-port3",
    ]
    monkeypatch.setattr(qc.glob, "glob", lambda pattern: paths if "usb*/" in pattern else [])

    def fake_read(path):
        normalized = str(path).replace("\\", "/")
        return "unknown" if normalized.endswith("port3/connect_type") else "hotplug"

    monkeypatch.setattr(qc, "_read_sysfs_text", fake_read)
    monkeypatch.setattr(qc.os.path, "realpath", lambda path: str(path).replace("\\", "/"))
    assert qc.external_usb_port_count() == 2


def test_ports_screen_uses_auto_matrix_and_s_key_fail():
    latest_ports_screen = TUI_TEXT[TUI_TEXT.rfind("def screen_qc_ports"):]
    assert "_run_ports_matrix(stdscr, slots)" in latest_ports_screen
    assert 'if result.get("_action") == "reselect":' in latest_ports_screen
    assert "S fail" in TUI_TEXT
    assert "R reselect" in TUI_TEXT
    assert "All detected ports completed." in TUI_TEXT
    assert "Each row needs its own plug/replug event" in TUI_TEXT
    assert "last_usb_devices = qc.current_usb_device_keys()" in TUI_TEXT
    assert "usb_added = current_usb_devices - last_usb_devices" in TUI_TEXT
    assert "tested_usb_ports: set[str] = set()" in TUI_TEXT
    assert "tested_usb_fingerprints: set[str] = set()" in TUI_TEXT
    assert "qc.usb_connector_fingerprint(port_id)" in TUI_TEXT
    assert "port_id not in tested_usb_ports" in TUI_TEXT
    assert "USB_CLASSIFY_DELAY = 1.2" in TUI_TEXT
    assert "pending_usb_ports.setdefault(port_id, now)" in TUI_TEXT
    assert "tested_typec_ports: set[str] = set()" in TUI_TEXT
    assert "last_power_online = qc.current_dc_power_online()" in TUI_TEXT
    assert "last_audio_active = qc.current_audio_jack_active()" in TUI_TEXT
    assert "mark_initial(" not in TUI_TEXT
    assert "audio_armed = not last_audio_active" in TUI_TEXT
    assert "wired_armed = not last_wired_link_up" in TUI_TEXT
    assert "Type-C row for that plug event" in TUI_TEXT
    assert "profile = [" in latest_ports_screen
    assert 'if slot.get("kind") != "audio"' in latest_ports_screen
    assert "_confirm_port_inventory(stdscr, profile)" in latest_ports_screen
    assert "QC - Confirm Physical Ports" in TUI_TEXT
    assert "manual_port_key_defs = [" in TUI_TEXT
    assert "selected_manual_kinds = {" in TUI_TEXT
    assert "manual_mapping_text()" in TUI_TEXT
    assert "manual_footer_text()" in TUI_TEXT
    assert "Manual OK keys:" in TUI_TEXT
    assert 'ord("a"): ("usb_a", "USB Type-A")' not in TUI_TEXT
    assert 'ord("c"): ("usb_c", "USB Type-C")' not in TUI_TEXT
    assert '("H", "hdmi", "HDMI")' in TUI_TEXT
    assert '("V", "vga", "VGA")' in TUI_TEXT
    assert '("D", "displayport", "DisplayPort")' in TUI_TEXT
    assert '("I", "dvi", "DVI")' in TUI_TEXT
    assert '("J", "audio", "3.5mm Audio Jack")' in TUI_TEXT
    assert 'ord("p"): ("dc_power", "DC Power Jack")' not in TUI_TEXT
    assert 'ord("e"): ("ethernet", "Ethernet RJ45")' not in TUI_TEXT
    assert "pending_usb_choice: tuple[str, str] | None = None" not in TUI_TEXT
    assert "suppressed_usb_fingerprints: dict[str, float] = {}" in TUI_TEXT
    assert "typec_guard_until = 0.0" in TUI_TEXT
    assert "USB event consume a Type-A row" in TUI_TEXT
    assert "TYPEC_USB_SUPPRESS_SECONDS = 10.0" in TUI_TEXT
    assert "USB detected on {port_id}: press A for Type-A or C for Type-C." not in TUI_TEXT
    assert "Connect a USB device first, then press" not in TUI_TEXT
    assert "Is the VSTL screen visible on the connected {label} display?" in TUI_TEXT
    assert "Are headphones connected and audio confirmed through the 3.5mm jack?" in TUI_TEXT
    assert "def _append_live_port" in TUI_TEXT
    assert "live-detected:" in TUI_TEXT
    assert "allow_live_add=True" in TUI_TEXT
    assert 'note_manual_display_event(display_added, "display hotplug transition")' in TUI_TEXT
    assert 'mark_one("dc_power", "charger online transition",' in TUI_TEXT
    assert 'note_manual_event("audio", "jack sense transition")' in TUI_TEXT
    assert '["alsactl", "monitor"]' in TUI_TEXT
    assert '"ALSA jack control transition"' in TUI_TEXT
    assert 'is not in the selected port list' in TUI_TEXT
    assert "qc.current_connected_display_connectors()" in TUI_TEXT
    assert 'mark_one("usb_c", f"Type-C partner detected on {port_id}",' in TUI_TEXT


def test_usb_device_identity_is_stable_per_physical_socket():
    assert qc._usb_physical_port_key("1-2") == "1-2"
    assert qc._usb_physical_port_key("1-2.3") == "1-2"
    assert qc._usb_physical_port_key("2-4.1.6") == "2-4"


def test_usb_fingerprint_prefers_udev_hardware_path(monkeypatch):
    monkeypatch.setattr(
        qc, "_run",
        lambda argv, timeout=5: "ID_PATH=pci-0000:00:14.0-usb-0:2:1.0\n",
    )
    assert qc.usb_connector_fingerprint("1-2") == "pci-0000:00:14.0-usb-0:2"


def test_usb_physical_port_kind_uses_typec_path_evidence(monkeypatch):
    monkeypatch.setattr(
        qc,
        "_run",
        lambda argv, timeout=5: "ID_PATH=pci-0000:00:0d.0-usb-c-typec-port0\n",
    )
    monkeypatch.setattr(os.path, "lexists", lambda path: False)
    monkeypatch.setattr(
        os.path,
        "realpath",
        lambda path: "/devices/pci0000:00/usb1/1-2",
    )
    assert qc.usb_physical_port_kind("1-2") == "usb_c"


def test_usb_physical_port_kind_defaults_ordinary_usb_path_to_type_a(monkeypatch):
    monkeypatch.setattr(
        qc,
        "_run",
        lambda argv, timeout=5: "ID_PATH=pci-0000:00:14.0-usb-0:2:1.0\n",
    )
    monkeypatch.setattr(os.path, "lexists", lambda path: False)
    monkeypatch.setattr(
        os.path,
        "realpath",
        lambda path: "/devices/pci0000:00/usb1/1-2",
    )
    assert qc.usb_physical_port_kind("1-2") == "usb_a"


def test_ports_matrix_uses_connector_classification_not_first_remaining_row():
    assert "qc.usb_physical_port_kind(port_id)" in TUI_TEXT
    assert (
        '"usb_a" if _has_remaining_port(slots, status, "usb_a")'
        not in TUI_TEXT
    )


def test_typec_partner_discovery_uses_sibling_partner_nodes(monkeypatch):
    original_listdir = os.listdir

    def fake_listdir(path):
        if path == "/sys/class/typec":
            return ["port0", "port0-partner", "port1", "port1-cable"]
        if path == "/sys/class/typec/port0":
            return []
        if path == "/sys/class/typec/port1":
            return []
        if path in ("/sys/class/usb_role", "/sys/class/dual_role_usb"):
            raise OSError
        return original_listdir(path)

    monkeypatch.setattr(os, "listdir", fake_listdir)
    assert qc.typec_ports() == ["port0", "port1"]
    assert qc.current_typec_partners() == {"port0"}


def test_typec_ports_falls_back_to_platform_ucsi_and_thunderbolt_nodes(monkeypatch):
    original_listdir = os.listdir

    def fake_listdir(path):
        if path in ("/sys/class/typec", "/sys/class/usb_role", "/sys/class/dual_role_usb"):
            raise OSError
        return original_listdir(path)

    def fake_glob(pattern):
        if pattern == "/sys/bus/platform/devices/*":
            return ["/sys/bus/platform/devices/USBC000:00"]
        if pattern == "/sys/bus/acpi/devices/*":
            return []
        if pattern == "/sys/bus/thunderbolt/devices/*":
            return ["/sys/bus/thunderbolt/devices/domain0"]
        return []

    monkeypatch.setattr(os, "listdir", fake_listdir)
    monkeypatch.setattr(qc.glob, "glob", fake_glob)
    monkeypatch.setattr(os.path, "realpath", lambda path: str(path))

    assert qc.typec_ports() == ["port0"]


def test_display_kind_parser_handles_common_connector_names():
    assert qc._display_kind_from_name("card0-HDMI-A-1") == "hdmi"
    assert qc._display_kind_from_name("VGA-1") == "vga"
    assert qc._display_kind_from_name("card0-DP-2") == "displayport"


def test_display_connector_discovery_uses_kind_parser(monkeypatch):
    original_listdir = os.listdir
    original_exists = os.path.exists
    original_getsize = os.path.getsize
    original_read = qc._read_sysfs_text

    def fake_listdir(path):
        if path == "/sys/class/drm":
            return ["card0-hdmi-a-1", "card0-DP-2", "card0-eDP-1"]
        return original_listdir(path)

    def fake_exists(path):
        path = str(path).replace("\\", "/")
        if path.startswith("/sys/class/drm/card0-hdmi-a-1/"):
            return True
        if path.startswith("/sys/class/drm/card0-DP-2/"):
            return True
        return original_exists(path)

    def fake_getsize(path):
        path = str(path).replace("\\", "/")
        if path.endswith("/card0-DP-2/edid"):
            return 128
        return original_getsize(path)

    def fake_read(path):
        path = str(path).replace("\\", "/")
        if path.endswith("/card0-hdmi-a-1/status"):
            return "connected"
        if path.endswith("/card0-hdmi-a-1/enabled"):
            return "disabled"
        if path.endswith("/card0-DP-2/status"):
            return "unknown"
        if path.endswith("/card0-DP-2/enabled"):
            return "disabled"
        return original_read(path)

    monkeypatch.setattr(os, "listdir", fake_listdir)
    monkeypatch.setattr(os.path, "exists", fake_exists)
    monkeypatch.setattr(os.path, "getsize", fake_getsize)
    monkeypatch.setattr(qc, "_read_sysfs_text", fake_read)
    rows = qc.external_display_connectors()
    assert {row["kind"] for row in rows} == {"hdmi", "displayport"}
    assert all(row["connected"] for row in rows)


def test_connected_display_inventory_preserves_physical_connector_names(monkeypatch):
    monkeypatch.setattr(qc, "external_display_connectors", lambda: [
        {"name": "card0-HDMI-A-1", "kind": "hdmi", "connected": True},
        {"name": "card0-HDMI-A-2", "kind": "hdmi", "connected": False},
    ])
    monkeypatch.setattr(qc, "_run", lambda argv, timeout=5: "")

    connectors = qc.current_connected_display_connectors()

    assert connectors == {"card0-HDMI-A-1": "hdmi"}
    assert qc.current_connected_display_kinds() == {"hdmi"}


def test_system_info_displays_mac_id():
    assert '"mac_id": detect_mac()' in HW_TEXT
    assert '"MAC ID"' in TUI_TEXT
    assert 'ident.get("mac_id")' in TUI_TEXT
