import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QC_PATH = ROOT / "bench-client" / "vstl_qc_tests.py"
TUI = (ROOT / "bench-client" / "vstl-imaging-tui.py").read_text(encoding="utf-8")
ENTRY = (ROOT / "bench-client" / "vstl-bench-entry.sh").read_text(encoding="utf-8")

spec = importlib.util.spec_from_file_location("vstl_qc_tests_touch", QC_PATH)
qc = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(qc)


def _classify(**overrides):
    values = {
        "name": "Generic HID Device",
        "has_mt_xy": False,
        "has_abs_xy": False,
        "has_touch_key": False,
        "input_prop_direct": False,
        "input_prop_pointer": False,
        "udev_touchscreen": False,
        "udev_touchpad": False,
    }
    values.update(overrides)
    return qc._touch_device_classification(**values)


def test_udev_touchscreen_property_is_authoritative():
    is_touch, reason = _classify(udev_touchscreen=True)
    assert is_touch
    assert "ID_INPUT_TOUCHSCREEN" in reason


def test_evdev_direct_property_detects_generic_i2c_touch_panel():
    is_touch, reason = _classify(input_prop_direct=True, has_mt_xy=True)
    assert is_touch
    assert "INPUT_PROP_DIRECT" in reason


def test_generic_multitouch_controller_with_touch_key_is_direct_touch():
    is_touch, reason = _classify(has_mt_xy=True, has_touch_key=True)
    assert is_touch
    assert "multitouch touch coordinates" in reason


def test_touchpad_is_never_classified_as_touchscreen():
    is_touch, _reason = _classify(
        name="ELAN Touchpad",
        has_mt_xy=True,
        input_prop_direct=True,
        udev_touchscreen=True,
        udev_touchpad=True,
    )
    assert not is_touch


def test_non_touch_screen_offers_force_touch_recovery_key():
    assert "T force touch test" in TUI
    assert "Operator override: TOUCH display" in TUI


def test_touch_screen_has_one_active_handler_and_never_silently_skips_launch_failure():
    assert TUI.count("def screen_qc_touchscreen(") == 1
    assert "discovered_paths.update(" in TUI
    assert "sorted(discovered_paths - preferred_paths)" in TUI
    assert "if preferred_paths and path not in preferred_paths" not in TUI
    assert "qc._touch_device_classification(" in TUI
    assert '["udevadm", "settle", "--timeout=2"]' in TUI
    assert "_curses_draw_touch_grid" in TUI
    assert 'use_text_grid = os.environ.get("VSTL_TOUCH_USE_CURSES", "").strip() == "1"' in TUI
    assert "info[2] in (16, 24, 32)" in TUI
    assert "def _fb_pixel(" in TUI
    assert "framebuffer unavailable for pixel touch map" in TUI
    assert "touch text grid fallback disabled" in TUI
    assert "_set_console_cursor_visible(False)" in TUI
    assert "_set_console_cursor_visible(True)" in TUI
    assert "_set_console_graphics_mode(True)" in TUI
    assert "min(2, min(cell_w, cell_h) // 24)" in TUI
    assert "grid_top = 2" in TUI
    assert "pad_x = 0 if x1 - x0 <= 4 else 1" in TUI
    assert "pad_y = 0 if y1 - y0 <= 2 else 1" in TUI
    assert "previous_cursor = curses.curs_set(0)" in TUI
    assert "Touch display detected, but the touch map could not start." in TUI
    assert "[ R ] Retry touch input initialization" in TUI
    assert "[ F ] Mark touch test as FAIL and continue" in TUI
    assert "def _show_touch_result(" in TUI
    assert "RESULT: {verdict}" in TUI
    assert "_show_touch_result(stdscr, verdict, evidence)" in TUI


def test_qc_intro_uses_qc_test_wording_not_battery():
    assert "ENTER  start the QC test" in TUI
    assert "ENTER  start the QC battery" not in TUI


def test_live_entry_makes_touch_event_nodes_readable_before_tui_launch():
    chmod_pos = ENTRY.index("chmod a+r /dev/input/event*")
    launch_pos = ENTRY.index('TERM=linux python3 "$TUI_PATH"')
    assert chmod_pos < launch_pos
