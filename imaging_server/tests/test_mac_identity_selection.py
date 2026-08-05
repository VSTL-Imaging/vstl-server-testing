import importlib.util
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HW_PATH = ROOT / "bench-client" / "vstl_hw_detect.py"


def _load_hw():
    spec = importlib.util.spec_from_file_location("vstl_hw_detect_mac_identity", HW_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


hw = _load_hw()


def _iface(sys_net: Path, name: str, mac: str) -> None:
    path = sys_net / name
    path.mkdir(parents=True)
    (path / "address").write_text(mac, encoding="utf-8")


def test_detect_mac_prefers_builtin_lom_over_typec_adapter(tmp_path):
    sys_net = tmp_path / "net"
    _iface(sys_net, "aaa_typec", "11:22:33:44:55:66")
    _iface(sys_net, "eno1", "AA:BB:CC:DD:EE:10")

    with (
        mock.patch.object(hw, "_is_external_usb_or_typec_nic", side_effect=lambda iface, base: iface == "aaa_typec"),
        mock.patch.object(hw, "_dmidecode_text", return_value=""),
    ):
        assert hw.detect_mac(str(sys_net)) == "AA:BB:CC:DD:EE:10"


def test_detect_mac_never_uses_typec_adapter_mac_without_lom_or_passthrough(tmp_path):
    sys_net = tmp_path / "net"
    _iface(sys_net, "enx112233445566", "11:22:33:44:55:66")

    with mock.patch.object(hw, "_dmidecode_text", return_value=""):
        assert hw.detect_mac(str(sys_net)) == hw.UNKNOWN


def test_detect_mac_uses_bios_passthrough_when_no_lom_is_available(tmp_path):
    sys_net = tmp_path / "net"
    _iface(sys_net, "enx112233445566", "11:22:33:44:55:66")
    dmi = "OEM String: Pass Through MAC Address: AA-BB-CC-DD-EE-F1"

    with mock.patch.object(hw, "_dmidecode_text", return_value=dmi):
        assert hw.detect_mac(str(sys_net)) == "AA:BB:CC:DD:EE:F1"


def test_detect_mac_prefers_bios_lom_before_passthrough(tmp_path):
    sys_net = tmp_path / "net"
    dmi = "\n".join([
        "OEM String: Pass Through MAC Address: AA-BB-CC-DD-EE-F1",
        "OEM String: LOM MAC Address: AA-BB-CC-DD-EE-20",
    ])

    with mock.patch.object(hw, "_dmidecode_text", return_value=dmi):
        assert hw.detect_mac(str(sys_net)) == "AA:BB:CC:DD:EE:20"
