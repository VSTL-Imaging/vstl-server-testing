import importlib.util
from pathlib import Path


ROOT = Path(__file__).parent.parent
CAPTURE_PATH = ROOT / "bench-client" / "vstl_image_capture.py"
SPEC = importlib.util.spec_from_file_location("vstl_image_capture_speed", CAPTURE_PATH)
capture = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(capture)


def _write_counter(root: Path, interface: str, state: str, rx: int, tx: int) -> None:
    base = root / interface
    (base / "statistics").mkdir(parents=True)
    (base / "operstate").write_text(state, encoding="ascii")
    (base / "statistics" / "rx_bytes").write_text(str(rx), encoding="ascii")
    (base / "statistics" / "tx_bytes").write_text(str(tx), encoding="ascii")


def test_network_totals_include_up_physical_and_exclude_virtual(tmp_path):
    _write_counter(tmp_path, "eth0", "up", 1_000, 2_000)
    _write_counter(tmp_path, "enxusb", "unknown", 3_000, 4_000)
    _write_counter(tmp_path, "tailscale0", "up", 50_000, 60_000)
    _write_counter(tmp_path, "veth123", "up", 70_000, 80_000)
    _write_counter(tmp_path, "eth1", "down", 90_000, 100_000)

    totals = capture._network_byte_totals(str(tmp_path))

    assert totals == {
        "rx": 4_000,
        "tx": 6_000,
        "interfaces": ["enxusb", "eth0"],
    }


def test_restore_uses_receive_rate_and_capture_uses_transmit_rate():
    restore_state = {"operation": "restoring"}
    restore_speed = {}
    capture._update_network_progress(
        restore_state, restore_speed, 10.0,
        {"rx": 1_000, "tx": 2_000, "interfaces": ["eth0"]},
    )
    capture._update_network_progress(
        restore_state, restore_speed, 12.0,
        {"rx": 3_048, "tx": 2_200, "interfaces": ["eth0"]},
    )
    assert restore_state["network_rate"] == "1.0 KB/s"
    assert restore_state["network_direction"] == "download"

    capture_state = {"operation": "capturing"}
    capture_speed = {}
    capture._update_network_progress(
        capture_state, capture_speed, 20.0,
        {"rx": 5_000, "tx": 10_000, "interfaces": ["eth0"]},
    )
    capture._update_network_progress(
        capture_state, capture_speed, 22.0,
        {"rx": 5_100, "tx": 14_096, "interfaces": ["eth0"]},
    )
    assert capture_state["network_rate"] == "2.0 KB/s"
    assert capture_state["network_direction"] == "upload"


def test_network_rate_uses_rolling_window():
    state = {"operation": "restoring"}
    speed = {}
    capture._update_network_progress(
        state, speed, 0.0,
        {"rx": 0, "tx": 0, "interfaces": ["eth0"]},
    )
    capture._update_network_progress(
        state, speed, 3.0,
        {"rx": 3_072, "tx": 0, "interfaces": ["eth0"]},
    )
    capture._update_network_progress(
        state, speed, 6.0,
        {"rx": 9_216, "tx": 0, "interfaces": ["eth0"]},
    )

    assert speed["network_samples"] == [(3.0, 3_072, 0), (6.0, 9_216, 0)]
    assert state["network_rate"] == "2.0 KB/s"
