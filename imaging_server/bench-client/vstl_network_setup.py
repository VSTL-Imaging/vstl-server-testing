#!/usr/bin/env python3
"""Bring up a VSTL bench network before the technician TUI starts.

The USB workflow prefers any connected physical Ethernet adapter, including
USB-A/USB-C dongles. If wired access cannot reach the VSTL server, it offers a
small console Wi-Fi selector. No Wi-Fi password is stored in the ISO.
"""

from __future__ import annotations

import getpass
import ipaddress
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse


DEFAULT_SERVER_IP = "10.255.0.75"
SYS_CLASS_NET = Path("/sys/class/net")
DHCP_RELEASE_IFACE_FILE = Path(
    os.environ.get("VSTL_DHCP_RELEASE_IFACE_FILE", "/run/vstl-dhcp-release-iface")
)
STATUS_HOLD_SECONDS = 3.0
WIRED_DEVICE_WAIT_SECONDS = 60
WIRED_LINK_WAIT_SECONDS = 20
WIRED_DHCP_ATTEMPTS = 3
WIRED_DRIVER_MODULES = (
    "usbnet",
    "r8152",
    "r8153_ecm",
    "cdc_ether",
    "cdc_eem",
    "cdc_ncm",
    "cdc_mbim",
    "ax88179_178a",
    "asix",
    "aqc111",
    "lan78xx",
    "smsc75xx",
    "smsc95xx",
    "rtl8150",
    "dm9601",
    "sr9700",
    "mcs7830",
    "cdc_subset",
    "thunderbolt_net",
    "igb",
    "igc",
    "e1000e",
    "r8169",
)
USB_NETWORK_DRIVERS = (
    "r8152",
    "r8153_ecm",
    "cdc_ether",
    "cdc_eem",
    "cdc_ncm",
    "cdc_mbim",
    "ax88179_178a",
    "asix",
    "aqc111",
    "lan78xx",
    "smsc75xx",
    "smsc95xx",
    "rtl8150",
    "dm9601",
    "sr9700",
    "mcs7830",
    "cdc_subset",
)


def run(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 127, stdout=str(exc))


def hold_status(message: str, seconds: float = STATUS_HOLD_SECONDS) -> None:
    """Keep connection results visible before the technician TUI replaces them."""
    print(f"\n{message}")
    sys.stdout.flush()
    try:
        delay = float(os.environ.get("VSTL_NETWORK_STATUS_SECONDS", str(seconds)))
    except ValueError:
        delay = seconds
    if delay > 0:
        time.sleep(delay)


def server_host(environ: dict[str, str] | None = None) -> str:
    env = environ or os.environ
    explicit = env.get("VSTL_SERVER_IP", "").strip()
    if explicit:
        return explicit

    fog_server = env.get("FOG_SERVER", "").strip()
    if fog_server:
        parsed = urlparse(
            fog_server if "://" in fog_server else f"http://{fog_server}"
        )
        if parsed.hostname:
            return parsed.hostname

    return DEFAULT_SERVER_IP


def server_reachable(host: str, timeout: float = 2.0) -> bool:
    ports = (80, 443)
    for port in ports:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def interface_has_ipv4(interface: str) -> bool:
    result = run(["ip", "-4", "-o", "addr", "show", "dev", interface], timeout=4)
    return result.returncode == 0 and bool(result.stdout.strip())


def env_enabled(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def record_dhcp_release_interface(interface: str) -> None:
    try:
        DHCP_RELEASE_IFACE_FILE.parent.mkdir(parents=True, exist_ok=True)
        DHCP_RELEASE_IFACE_FILE.write_text(f"{interface}\n", encoding="utf-8")
    except OSError:
        pass


def claim_existing_dhcp_lease(interface: str) -> None:
    """Create dhclient lease state when live-boot already configured IPv4."""
    if not env_enabled("VSTL_DHCP_CLAIM_EXISTING_LEASE", True):
        return
    if run(["sh", "-c", "command -v dhclient"], timeout=3).returncode != 0:
        return
    try:
        timeout = int(os.environ.get("VSTL_DHCP_CLAIM_TIMEOUT", "10") or "10")
    except ValueError:
        timeout = 10
    result = run(["dhclient", "-4", "-1", "-v", interface], timeout=max(5, timeout))
    if result.returncode != 0:
        detail = result.stdout.strip().replace("\n", " ")[:120]
        print(f"DHCP lease claim on {interface} skipped: {detail}")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="ignore").strip()
    except OSError:
        return ""


def normalize_mac(value: str) -> str:
    mac = value.strip().lower()
    if mac.startswith("01-") and "-" in mac:
        mac = mac[3:]
    mac = mac.replace("-", ":")
    compact = re.sub(r"[^0-9a-f]", "", mac)
    if len(compact) != 12:
        return ""
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def pxe_boot_mac(cmdline_path: Path = Path("/proc/cmdline")) -> str:
    """Return the MAC of the interface that firmware/iPXE used, if supplied."""
    text = _read_text(cmdline_path)
    live_netdev = ""
    bootif = ""
    for token in text.split():
        if token.startswith("live-netdev="):
            live_netdev = token.split("=", 1)[1]
        elif token.startswith("BOOTIF="):
            bootif = token.split("=", 1)[1]
    return normalize_mac(live_netdev) or normalize_mac(bootif)


def interface_mac(interface: str, sys_class_net: Path = SYS_CLASS_NET) -> str:
    return normalize_mac(_read_text(sys_class_net / interface / "address"))


def is_wireless(interface: str, sys_class_net: Path = SYS_CLASS_NET) -> bool:
    base = sys_class_net / interface
    if (base / "wireless").is_dir():
        return True
    uevent = base / "uevent"
    try:
        return "DEVTYPE=wlan" in uevent.read_text(errors="ignore")
    except OSError:
        return interface.startswith(("wl", "wlan"))


def physical_interfaces(sys_class_net: Path = SYS_CLASS_NET) -> list[str]:
    if not sys_class_net.is_dir():
        return []
    interfaces = []
    for entry in sorted(sys_class_net.iterdir(), key=lambda item: item.name):
        name = entry.name
        if name == "lo" or name.startswith(
            (
                "wwan",
                "docker",
                "veth",
                "virbr",
                "tap",
                "tun",
                "bond",
                "br-",
                "p2p-",
            )
        ):
            continue
        if (entry / "device").exists() or name.startswith(("en", "eth", "usb")):
            interfaces.append(name)
    return interfaces


def wired_interfaces(sys_class_net: Path = SYS_CLASS_NET) -> list[str]:
    return [
        name
        for name in physical_interfaces(sys_class_net)
        if not is_wireless(name, sys_class_net)
    ]


def wifi_interfaces(sys_class_net: Path = SYS_CLASS_NET) -> list[str]:
    interfaces = [
        name
        for name in physical_interfaces(sys_class_net)
        if is_wireless(name, sys_class_net)
    ]
    if interfaces:
        return interfaces

    result = run(["iw", "dev"], timeout=4)
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*Interface\s+(\S+)", line)
        if match and not match.group(1).startswith("p2p-"):
            interfaces.append(match.group(1))
    if interfaces:
        return sorted(set(interfaces))

    result = run(
        ["nmcli", "--terse", "--fields", "DEVICE,TYPE", "device", "status"],
        timeout=6,
    )
    for line in result.stdout.splitlines():
        fields = _split_nmcli_line(line)
        if len(fields) >= 2 and fields[1].strip().lower() == "wifi":
            interfaces.append(fields[0].strip())
    return sorted(set(i for i in interfaces if i))


def carrier(interface: str, sys_class_net: Path = SYS_CLASS_NET) -> int:
    try:
        return int((sys_class_net / interface / "carrier").read_text().strip())
    except (OSError, ValueError):
        return 0


def operstate(interface: str, sys_class_net: Path = SYS_CLASS_NET) -> str:
    return _read_text(sys_class_net / interface / "operstate").lower()


def is_usb_or_typec_interface(interface: str, sys_class_net: Path = SYS_CLASS_NET) -> bool:
    if interface.startswith(("enx", "usb")):
        return True
    try:
        device_path = (sys_class_net / interface / "device").resolve()
    except OSError:
        return False
    text = str(device_path).lower()
    return "/usb" in text or "thunderbolt" in text or "typec" in text


def interface_for_mac(
    mac: str,
    preferred: str = "",
    sys_class_net: Path = SYS_CLASS_NET,
) -> str:
    target = normalize_mac(mac)
    if not target:
        return ""
    candidates = wired_interfaces(sys_class_net)
    if preferred in candidates:
        candidates.remove(preferred)
        candidates.insert(0, preferred)
    for candidate in candidates:
        if interface_mac(candidate, sys_class_net) == target:
            return candidate
    return ""


def wait_for_interface_by_mac(
    mac: str,
    preferred: str = "",
    seconds: int = 18,
    sys_class_net: Path = SYS_CLASS_NET,
) -> str:
    deadline = time.monotonic() + max(1, seconds)
    while time.monotonic() <= deadline:
        match = interface_for_mac(mac, preferred, sys_class_net)
        if match:
            return match
        prime_wired_drivers()
        time.sleep(1)
    return interface_for_mac(mac, preferred, sys_class_net)


def rebind_interface_driver(interface: str, sys_class_net: Path = SYS_CLASS_NET) -> bool:
    try:
        device_path = (sys_class_net / interface / "device").resolve()
        driver_path = (device_path / "driver").resolve()
    except OSError:
        return False
    if not (driver_path / "unbind").exists() or not (driver_path / "bind").exists():
        return False
    device_id = device_path.name
    run(["ip", "link", "set", interface, "down"], timeout=5)
    try:
        (driver_path / "unbind").write_text(device_id, encoding="ascii")
        time.sleep(1)
        (driver_path / "bind").write_text(device_id, encoding="ascii")
    except OSError:
        return False
    run(["udevadm", "trigger", "--subsystem-match=net", "--action=add"], timeout=5)
    run(["udevadm", "settle", "--timeout=10"], timeout=11)
    return True


def rebind_usb_network_drivers() -> int:
    rebound = 0
    for driver in USB_NETWORK_DRIVERS:
        driver_path = Path("/sys/bus/usb/drivers") / driver
        if not driver_path.is_dir():
            continue
        for bound in sorted(driver_path.glob("*:*")):
            if not bound.is_symlink():
                continue
            bound_id = bound.name
            try:
                (driver_path / "unbind").write_text(bound_id, encoding="ascii")
                time.sleep(1)
                (driver_path / "bind").write_text(bound_id, encoding="ascii")
                rebound += 1
            except OSError:
                continue
    if rebound:
        run(["udevadm", "trigger", "--subsystem-match=usb", "--action=add"], timeout=5)
        run(["udevadm", "trigger", "--subsystem-match=net", "--action=add"], timeout=5)
        run(["udevadm", "settle", "--timeout=10"], timeout=11)
    return rebound


def recover_usb_typec_interface(interface: str) -> str:
    """Reset a firmware-owned USB-C NIC and return its current Linux name."""
    mac = interface_mac(interface)
    if not mac:
        rebind_usb_network_drivers()
        return interface
    rebind_interface_driver(interface)
    rebind_usb_network_drivers()
    return wait_for_interface_by_mac(mac, preferred=interface) or interface


def prime_wired_drivers() -> None:
    for module in WIRED_DRIVER_MODULES:
        run(["modprobe", "-q", module], timeout=5)
    run(["udevadm", "trigger", "--subsystem-match=net", "--action=add"], timeout=5)
    run(["udevadm", "settle", "--timeout=12"], timeout=13)


def wait_for_wired_interfaces(
    timeout_seconds: int = WIRED_DEVICE_WAIT_SECONDS,
    sys_class_net: Path = SYS_CLASS_NET,
) -> list[str]:
    deadline = time.monotonic() + max(1, timeout_seconds)
    last_interfaces: list[str] = []
    while time.monotonic() <= deadline:
        prime_wired_drivers()
        last_interfaces = wired_interfaces(sys_class_net)
        if last_interfaces:
            return last_interfaces
        time.sleep(2)
    return last_interfaces


def wait_for_link(interface: str, seconds: int = WIRED_LINK_WAIT_SECONDS) -> bool:
    deadline = time.monotonic() + max(1, seconds)
    while time.monotonic() <= deadline:
        if carrier(interface) == 1 or operstate(interface) in {"up", "unknown"}:
            return True
        time.sleep(1)
    return carrier(interface) == 1 or operstate(interface) in {"up", "unknown"}


def wired_priority(
    interface: str,
    target_mac: str = "",
    sys_class_net: Path = SYS_CLASS_NET,
) -> tuple[int, str]:
    score = 0
    mac = interface_mac(interface, sys_class_net)
    if target_mac and mac == target_mac:
        score += 1000
    if carrier(interface, sys_class_net) == 1:
        score += 100
    if operstate(interface, sys_class_net) == "up":
        score += 25
    if is_usb_or_typec_interface(interface, sys_class_net):
        score += 20
    if interface.startswith(("enx", "usb")):
        score += 10
    return score, interface


def ordered_wired_interfaces(
    interfaces: list[str],
    target_mac: str = "",
    sys_class_net: Path = SYS_CLASS_NET,
) -> list[str]:
    return sorted(
        interfaces,
        key=lambda name: (-wired_priority(name, target_mac, sys_class_net)[0], name),
    )


def request_dhcp(interface: str) -> bool:
    current_interface = interface
    for attempt in range(1, WIRED_DHCP_ATTEMPTS + 1):
        prime_wired_drivers()
        if is_usb_or_typec_interface(current_interface) and attempt in {1, 2}:
            recovered = recover_usb_typec_interface(current_interface)
            if recovered != current_interface:
                print(f"USB-C Ethernet adapter moved from {current_interface} to {recovered}.")
                current_interface = recovered
        run(["ip", "link", "set", current_interface, "up"], timeout=5)
        wait_for_link(current_interface)
        if interface_has_ipv4(current_interface):
            record_dhcp_release_interface(current_interface)
            claim_existing_dhcp_lease(current_interface)
            return True
        run(["ip", "addr", "flush", "dev", current_interface], timeout=5)

        if run(["sh", "-c", "command -v dhclient"], timeout=3).returncode == 0:
            run(["dhclient", "-4", "-r", current_interface], timeout=8)
            result = run(["dhclient", "-4", "-1", "-v", current_interface], timeout=45)
            if result.returncode == 0 and interface_has_ipv4(current_interface):
                record_dhcp_release_interface(current_interface)
                return True

        if run(["sh", "-c", "command -v dhcpcd"], timeout=3).returncode == 0:
            run(["dhcpcd", "-k", current_interface], timeout=8)
            result = run(["dhcpcd", "-4", "-1", current_interface], timeout=45)
            if result.returncode == 0 and interface_has_ipv4(current_interface):
                record_dhcp_release_interface(current_interface)
                return True

        if run(["sh", "-c", "command -v udhcpc"], timeout=3).returncode == 0:
            result = run(["udhcpc", "-q", "-t", "7", "-T", "5", "-i", current_interface], timeout=45)
            if result.returncode == 0 and interface_has_ipv4(current_interface):
                record_dhcp_release_interface(current_interface)
                return True

        if attempt < WIRED_DHCP_ATTEMPTS:
            print(f"Retrying DHCP on {current_interface} ({attempt}/{WIRED_DHCP_ATTEMPTS})...")
            time.sleep(2)

    return False


def try_wired(host: str) -> tuple[bool, list[str]]:
    interfaces = wait_for_wired_interfaces()
    if not interfaces:
        return False, []

    target_mac = pxe_boot_mac()
    if target_mac:
        print(f"PXE boot adapter MAC: {target_mac}")
    ordered = ordered_wired_interfaces(interfaces, target_mac)
    for interface in ordered:
        print(f"Checking wired adapter {interface}...")
        if request_dhcp(interface):
            if server_reachable(host):
                print(f"Connected to VSTL server through {interface}.")
                return True, interfaces
            print(f"{interface} has an IP address but cannot reach {host}.")
        else:
            print(f"No DHCP lease on {interface}.")
    return False, interfaces


def _split_nmcli_line(line: str) -> list[str]:
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in line.rstrip("\n"):
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def parse_nmcli_networks(raw: str) -> list[tuple[str, int, str]]:
    strongest: dict[str, tuple[int, str]] = {}
    for line in raw.splitlines():
        fields = _split_nmcli_line(line)
        if len(fields) < 3:
            continue
        ssid = fields[0].strip()
        if not ssid:
            continue
        try:
            signal = int(fields[1].strip() or "0")
        except ValueError:
            signal = 0
        security = ":".join(fields[2:]).strip() or "--"
        if ssid not in strongest or signal > strongest[ssid][0]:
            strongest[ssid] = (signal, security)
    return sorted(
        ((ssid, signal, security) for ssid, (signal, security) in strongest.items()),
        key=lambda item: (-item[1], item[0].lower()),
    )


def parse_iw_networks(raw: str) -> list[tuple[str, int, str]]:
    """Parse iw/iwlist output when NetworkManager has not populated yet."""
    networks: dict[str, tuple[int, str]] = {}
    blocks = re.split(r"(?=^\s*(?:BSS\s|Cell\s+\d+\s+-))", raw or "", flags=re.M)
    for block in blocks:
        ssid_match = re.search(r"^\s*SSID:\s*(.*?)\s*$", block, re.M)
        if not ssid_match:
            ssid_match = re.search(r'ESSID:"(.*?)"', block)
        if not ssid_match:
            continue
        ssid = ssid_match.group(1).strip()
        if not ssid:
            continue
        signal = 0
        signal_match = re.search(r"signal:\s*(-?\d+(?:\.\d+)?)\s*dBm", block, re.I)
        if signal_match:
            signal = max(0, min(100, int(2 * (float(signal_match.group(1)) + 100))))
        else:
            quality_match = re.search(r"Quality=(\d+)/(\d+)", block, re.I)
            if quality_match and int(quality_match.group(2)):
                signal = int(int(quality_match.group(1)) * 100 / int(quality_match.group(2)))
        security = "OPEN"
        if re.search(r"\b(RSN|WPA|Privacy)\b|Encryption key:on", block, re.I):
            security = "WPA"
        previous = networks.get(ssid)
        if previous is None or signal > previous[0]:
            networks[ssid] = (signal, security)
    return sorted(
        ((ssid, signal, security) for ssid, (signal, security) in networks.items()),
        key=lambda item: (-item[1], item[0].lower()),
    )


def start_network_manager() -> bool:
    if run(["sh", "-c", "command -v nmcli"], timeout=3).returncode != 0:
        return False
    run(["rfkill", "unblock", "all"], timeout=5)
    for module in ("cfg80211", "mac80211", "iwlwifi", "iwlmvm"):
        run(["modprobe", module], timeout=5)
    run(["udevadm", "trigger", "--subsystem-match=net", "--action=add"], timeout=5)
    run(["udevadm", "settle", "--timeout=8"], timeout=9)
    run(["systemctl", "start", "NetworkManager"], timeout=20)
    run(["nmcli", "networking", "on"], timeout=8)
    run(["nmcli", "radio", "wifi", "on"], timeout=8)
    return True


def scan_networks(interface: str) -> list[tuple[str, int, str]]:
    run(["ip", "link", "set", interface, "up"], timeout=5)
    run(["nmcli", "device", "set", interface, "managed", "yes"], timeout=5)
    run(["nmcli", "--wait", "12", "device", "wifi", "rescan", "ifname", interface], timeout=15)
    result = run(
        [
            "nmcli",
            "--terse",
            "--escape",
            "yes",
            "--fields",
            "SSID,SIGNAL,SECURITY",
            "device",
            "wifi",
            "list",
            "ifname",
            interface,
            "--rescan",
            "yes",
        ],
        timeout=20,
    )
    networks = parse_nmcli_networks(result.stdout)
    if networks:
        return networks
    result = run(["iw", "dev", interface, "scan"], timeout=18)
    networks = parse_iw_networks(result.stdout)
    if networks:
        return networks
    result = run(["iwlist", interface, "scanning"], timeout=18)
    return parse_iw_networks(result.stdout)


def connect_wifi(interface: str, ssid: str, security: str) -> bool:
    command = [
        "nmcli",
        "--wait",
        "30",
        "device",
        "wifi",
        "connect",
        ssid,
        "ifname",
        interface,
    ]
    if security not in ("", "--", "NONE"):
        password = getpass.getpass(f"Wi-Fi password for {ssid}: ")
        if not password:
            print("Password was empty; connection cancelled.")
            return False
        command.extend(["password", password])

    result = run(command, timeout=40)
    if result.returncode != 0:
        print(result.stdout.strip() or "Wi-Fi connection failed.")
        return False
    return True


def prompt_wifi(host: str) -> bool:
    if not start_network_manager():
        hold_status("Wi-Fi setup failed: NetworkManager/nmcli is not installed.")
        return False
    interfaces = wifi_interfaces()
    if not interfaces:
        hold_status("Wi-Fi setup failed: no Wi-Fi adapter was detected.")
        return False

    interface = interfaces[0]
    while True:
        print(f"\nScanning Wi-Fi networks with {interface}...")
        networks = scan_networks(interface)
        print("\nVSTL USB Network Setup")
        print("======================")
        print(f"Server: {host}")
        if networks:
            for index, (ssid, signal, security) in enumerate(networks[:20], start=1):
                print(f"{index:2}. {ssid[:44]:44} {signal:3}%  {security}")
        else:
            print("No named Wi-Fi networks were found.")
        print("\nR = rescan, M = hidden/manual SSID, O = continue offline")
        choice = input("Select Wi-Fi: ").strip()
        if choice.lower() == "o":
            return False
        if choice.lower() == "r":
            continue
        if choice.lower() == "m":
            ssid = input("Wi-Fi SSID: ").strip()
            if not ssid:
                continue
            security = "WPA"
        elif choice.isdigit() and 1 <= int(choice) <= min(20, len(networks)):
            ssid, _, security = networks[int(choice) - 1]
        else:
            print("Invalid selection.")
            time.sleep(1)
            continue

        if not connect_wifi(interface, ssid, security):
            time.sleep(2)
            continue
        if server_reachable(host, timeout=3):
            hold_status(f"Wi-Fi connected successfully. VSTL server {host} is reachable.")
            return True

        print(
            f"Wi-Fi connected, but {host} is not reachable. "
            "Use the bench Wi-Fi/VLAN or a USB Ethernet adapter."
        )
        input("Press Enter to choose another network...")


def validate_server_ip(host: str) -> str:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            return DEFAULT_SERVER_IP
    return host


def main() -> int:
    host = validate_server_ip(server_host())
    print("\nPreparing network connection for VSTL...")

    if server_reachable(host):
        hold_status(f"Network ready. VSTL server {host} is already reachable.")
        return 0

    connected, wired = try_wired(host)
    if connected:
        hold_status(f"Wired connection successful. VSTL server {host} is reachable.")
        return 0

    if os.environ.get("VSTL_WIFI_PROMPT", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        hold_status("Network failed. Wi-Fi selection is disabled; continuing offline.")
        return 1

    if wired:
        print("\nNo wired adapter could reach the server.")
    else:
        print("\nNo physical Ethernet adapter was detected.")
    print("A USB-A/USB-C Ethernet adapter is recommended for image transfers.")
    if prompt_wifi(host):
        return 0
    hold_status("No connection to the VSTL server. Continuing in offline QC mode.")
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nNetwork setup cancelled; continuing offline.")
        raise SystemExit(1)
