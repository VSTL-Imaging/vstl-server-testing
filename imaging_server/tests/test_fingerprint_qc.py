import importlib.util
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
QC_PATH = ROOT / "bench-client" / "vstl_qc_tests.py"
TUI_TEXT = (ROOT / "bench-client" / "vstl-imaging-tui.py").read_text(encoding="utf-8")
LIVE_ISO_TEXT = (ROOT / "04_build_live_iso.sh").read_text(encoding="utf-8")
CLONEZILLA_ISO_TEXT = (ROOT / "04_build_live_iso_clonezilla.sh").read_text(encoding="utf-8")


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


qc = _load_module("vstl_qc_tests_fingerprint", QC_PATH)


def test_fingerprint_probe_detects_known_usb_reader(monkeypatch):
    monkeypatch.setattr(
        qc,
        "_run",
        lambda argv, timeout=5: "Bus 001 Device 004: ID 06cb:00bd Synaptics, Inc. Fingerprint Reader"
        if argv == ["lsusb"] else "",
    )
    monkeypatch.setattr(qc, "fingerprint_event_devices", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_sysfs_matches", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_fprintd_probe", lambda: [])

    probe = qc.probe_fingerprint()

    assert probe["applicable"] is True
    assert "06cb:00bd" in probe["evidence"]


def test_fingerprint_probe_detects_hp_elan_usb_reader_without_fingerprint_name(monkeypatch):
    monkeypatch.setattr(
        qc,
        "_run",
        lambda argv, timeout=5: "Bus 001 Device 003: ID 04f3:0c4c ELAN Microelectronics Corp."
        if argv == ["lsusb"] else "",
    )
    monkeypatch.setattr(qc, "fingerprint_event_devices", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_sysfs_matches", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_fprintd_probe", lambda: [])

    probe = qc.probe_fingerprint()

    assert probe["applicable"] is True
    assert "04f3:0c4c" in probe["evidence"]


def test_fingerprint_probe_detects_dell_controlvault_reader(monkeypatch):
    monkeypatch.setattr(
        qc,
        "_run",
        lambda argv, timeout=5: "Bus 001 Device 004: ID 0a5c:5834 Broadcom Corp. 5880"
        if argv == ["lsusb"] else "",
    )
    monkeypatch.setattr(qc, "fingerprint_event_devices", lambda: [])
    monkeypatch.setattr(qc, "fingerprint_hidraw_devices", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_udev_database_matches", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_sysfs_matches", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_fprintd_probe", lambda: [])

    probe = qc.probe_fingerprint()

    assert probe["applicable"] is False
    assert probe["possible"] is True
    assert "ignored until Linux confirms fingerprint" in probe["evidence"]
    assert "0a5c:5834" in probe["evidence"]


def test_fingerprint_probe_detects_fprintd_only_reader(monkeypatch):
    monkeypatch.setattr(qc, "_run", lambda argv, timeout=5: "")
    monkeypatch.setattr(qc, "fingerprint_event_devices", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_sysfs_matches", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_fprintd_probe", lambda: ["fprintd Using device Goodix MOC"])

    probe = qc.probe_fingerprint()

    assert probe["applicable"] is True
    assert "fprintd Using device Goodix MOC" in probe["evidence"]
    assert probe["touch_capable"] is True


def test_fingerprint_probe_keeps_acpi_reader_visible_without_live_driver(monkeypatch):
    monkeypatch.setattr(qc, "_run", lambda argv, timeout=5: "")
    monkeypatch.setattr(qc, "fingerprint_event_devices", lambda: [])
    monkeypatch.setattr(qc, "fingerprint_hidraw_devices", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_udev_database_matches", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_sysfs_matches", lambda: ["sysfs GDIX0000:00"])
    monkeypatch.setattr(qc, "_fingerprint_kernel_matches", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_fprintd_probe", lambda: [])

    probe = qc.probe_fingerprint()

    assert probe["applicable"] is True
    assert probe["touch_capable"] is False
    assert probe["driver_available"] is False
    assert "GDIX0000" in probe["evidence"]


def test_embedded_fingerprint_hids_avoid_generic_syna_elan_false_positives():
    assert qc._fingerprint_bus_id_matches("ACPI\\FPC0001")
    assert qc._fingerprint_bus_id_matches("GDIX0000:00")
    assert not qc._fingerprint_bus_id_matches("GDIX1001 Touchscreen")
    assert qc._fingerprint_bus_id_matches("SYNA1234 WBDI fingerprint")
    assert not qc._fingerprint_bus_id_matches("SYNA1234 TouchPad")
    assert not qc._fingerprint_bus_id_matches("ELAN1200 Touchscreen")


def test_fingerprint_hidraw_detects_hp_elan_reader(monkeypatch):
    monkeypatch.setattr(qc.os, "listdir", lambda path: ["hidraw0"] if path == "/dev" else [])
    monkeypatch.setattr(
        qc,
        "_udev_input_properties",
        lambda path: {
            "ID_VENDOR_ID": "04f3",
            "ID_MODEL_ID": "0c4c",
            "ID_VENDOR": "ELAN",
            "ID_MODEL": "MOC_Fingerprint",
        },
    )
    monkeypatch.setattr(qc, "_read_sysfs_text", lambda path: "")

    devices = qc.fingerprint_hidraw_devices()

    assert devices
    assert devices[0].startswith("/dev/hidraw0")


def test_fingerprint_probe_absent_for_regular_usb_devices(monkeypatch):
    monkeypatch.setattr(
        qc,
        "_run",
        lambda argv, timeout=5: "Bus 001 Device 002: ID 8087:0026 Intel Corp. AX201 Bluetooth"
        if argv == ["lsusb"] else "",
    )
    monkeypatch.setattr(qc, "fingerprint_event_devices", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_sysfs_matches", lambda: [])
    monkeypatch.setattr(qc, "_fingerprint_fprintd_probe", lambda: [])

    probe = qc.probe_fingerprint()

    assert probe["applicable"] is False
    assert probe["evidence"] == "no fingerprint reader detected"


def test_applicable_qc_tests_hide_fingerprint_when_sensor_is_absent(monkeypatch):
    monkeypatch.setitem(
        qc.PROBES,
        "fingerprint",
        lambda: {"applicable": False, "evidence": "no fingerprint reader detected"},
    )

    assert "fingerprint" in qc.tests_for("L1")
    assert "fingerprint" in qc.tests_for("L2")
    assert "fingerprint" not in qc.applicable_tests_for("L1")
    assert "fingerprint" not in qc.applicable_tests_for("L2")


def test_applicable_qc_tests_include_fingerprint_when_sensor_is_present(monkeypatch):
    monkeypatch.setitem(
        qc.PROBES,
        "fingerprint",
        lambda: {"applicable": True, "evidence": "06cb:00bd Synaptics Fingerprint"},
    )

    assert "fingerprint" in qc.applicable_tests_for("L1")
    assert "fingerprint" in qc.applicable_tests_for("L2")


def test_applicable_qc_tests_hide_possible_security_device_without_linux_fingerprint(monkeypatch):
    monkeypatch.setitem(
        qc.PROBES,
        "fingerprint",
        lambda: {
            "applicable": False,
            "possible": True,
            "evidence": "possible biometric/security device ignored until Linux confirms fingerprint: 0a5c:5834 Broadcom 5880",
        },
    )

    assert "fingerprint" not in qc.applicable_tests_for("L1")
    assert "fingerprint" not in qc.applicable_tests_for("L2")


def test_tui_uses_hardware_filtered_qc_test_list():
    assert "test_keys = qc.applicable_tests_for(tech)" in TUI_TEXT
    assert "driver_preflight = qc.driver_preflight_result()" in TUI_TEXT
    assert "results: list[dict] = [driver_preflight]" in TUI_TEXT
    assert 'for key in ("driver_preflight", *qc.TEST_ORDER):' in TUI_TEXT


def test_fingerprint_name_matching_ignores_touchpads():
    assert qc._fingerprint_name_matches("Synaptics WBDI Fingerprint Reader")
    assert qc._fingerprint_name_matches("Goodix fingerprint SPI device")
    assert not qc._fingerprint_name_matches("Synaptics TouchPad")


def test_fingerprint_screen_auto_passes_or_k_fails():
    fingerprint_screen = TUI_TEXT[TUI_TEXT.index("def screen_qc_fingerprint("):]
    fingerprint_screen = fingerprint_screen[:fingerprint_screen.index("def screen_qc_speaker(")]

    assert "return _run_fingerprint_auto_test(stdscr, probe[\"evidence\"])" in fingerprint_screen
    assert "Touch the fingerprint reader to auto PASS." in fingerprint_screen
    assert "ch in (ord(\"k\"), ord(\"K\"))" in fingerprint_screen
    assert "result=\"PASS\"" in fingerprint_screen
    assert "result=\"FAIL\"" in fingerprint_screen
    assert "fprintd-enroll" in fingerprint_screen
    assert '"right-index-finger"' in fingerprint_screen
    assert '"systemctl", "start", "fprintd"' in fingerprint_screen
    assert "_fingerprint_fprintd_touch_line(line)" in fingerprint_screen
    assert "enroll-retry-scan" in fingerprint_screen
    assert "fingerprint captured" in fingerprint_screen
    assert "_open_fingerprint_hidraw_devices()" in fingerprint_screen
    assert "auto-pass via hidraw" in fingerprint_screen
    assert 'if not probe.get("touch_capable"):' in fingerprint_screen
    assert 'result="NA"' in fingerprint_screen
    assert "no usable libfprint/event driver" in fingerprint_screen


def test_driver_preflight_records_linux_support_without_vendor_download(monkeypatch):
    monkeypatch.setattr(qc, "ensure_audio_ready", lambda timeout_sec=6: "audio ready: playback=1 capture=1 cards=1")
    monkeypatch.setattr(qc, "probe_speaker", lambda: {"applicable": True, "evidence": "speaker"})
    monkeypatch.setattr(qc, "probe_microphone", lambda: {"applicable": True, "evidence": "mic"})
    monkeypatch.setattr(qc, "probe_fingerprint", lambda: {"applicable": True, "evidence": "fprintd Using device Goodix MOC"})
    monkeypatch.setattr(qc, "typec_ports", lambda: ["port0"])
    monkeypatch.setattr(qc, "current_typec_partners", lambda: set())
    monkeypatch.setattr(qc, "_driver_preflight_identity", lambda: "Dell Latitude 5530")
    monkeypatch.setattr(qc, "_driver_preflight_lspci_audio", lambda: ["00:1f.3 Audio device [8086:51c8]"])
    monkeypatch.setattr(qc, "_driver_preflight_lsusb_biometrics", lambda: ["27c6:63bc Goodix"])

    result = qc.driver_preflight_result()

    assert result["key"] == "driver_preflight"
    assert result["result"] == "PASS"
    assert "vendor Windows driver download not required" in result["remarks"]
    assert "model=Dell Latitude 5530" in result["evidence"]
    assert "typec_ports=1" in result["evidence"]


def test_driver_preflight_warns_when_fingerprint_lacks_libfprint_path(monkeypatch):
    monkeypatch.setattr(qc, "ensure_audio_ready", lambda timeout_sec=6: "audio ready: playback=1 capture=1 cards=1")
    monkeypatch.setattr(qc, "probe_speaker", lambda: {"applicable": True, "evidence": "speaker"})
    monkeypatch.setattr(qc, "probe_microphone", lambda: {"applicable": True, "evidence": "mic"})
    monkeypatch.setattr(
        qc,
        "probe_fingerprint",
        lambda: {
            "applicable": False,
            "possible": True,
            "evidence": "possible biometric/security device ignored until Linux confirms fingerprint: 0a5c:5834 Broadcom 5880",
        },
    )
    monkeypatch.setattr(qc, "typec_ports", lambda: [])
    monkeypatch.setattr(qc, "current_typec_partners", lambda: set())
    monkeypatch.setattr(qc, "_driver_preflight_identity", lambda: "HP EliteBook 640 G10")
    monkeypatch.setattr(qc, "_driver_preflight_lspci_audio", lambda: [])
    monkeypatch.setattr(qc, "_driver_preflight_lsusb_biometrics", lambda: ["27c6:63bc Goodix"])

    result = qc.driver_preflight_result()

    assert result["result"] == "PASS"
    assert "possible fingerprint/security hardware hidden from QC until Linux confirms it" in result["remarks"]
    assert "fingerprint_test_shown=no" in result["evidence"]
    assert "fingerprint_possible=yes" in result["evidence"]


def test_lcd_color_cycle_suppresses_touch_input_while_showing_colors():
    active_fb_path = TUI_TEXT[TUI_TEXT.index("if fb_ok:"):]
    active_fb_path = active_fb_path[:active_fb_path.index('return "fb0 color cycle: R/G/B/W/B full-screen; touch input suppressed"')]

    assert "_physical_blank_tty(stdscr)" in active_fb_path
    assert "_set_console_graphics_mode(True)" in active_fb_path
    assert "_restore_curses_after_framebuffer(stdscr, \"qc_display_color_done\")" in active_fb_path
    assert "stdscr.getch()" in active_fb_path
    assert "sys.stdin.readline()" not in active_fb_path


def test_lcd_verdict_prompt_rearms_curses_after_framebuffer_path():
    run_loop = TUI_TEXT[TUI_TEXT.index("def _qc_run_with_retest("):]
    run_loop = run_loop[:run_loop.index("def _qc_color_cycle(")]

    assert 'if test_name == "display":' in run_loop
    assert '_restore_curses_after_framebuffer(stdscr, "qc_display_verdict")' in run_loop


def test_fingerprint_runtime_packages_are_in_future_iso_builds():
    assert "fprintd" in LIVE_ISO_TEXT
    assert "libfprint-2-2" in LIVE_ISO_TEXT
    assert "fprintd" in CLONEZILLA_ISO_TEXT
    assert "libfprint-2-2" in CLONEZILLA_ISO_TEXT
