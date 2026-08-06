import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QC_PATH = ROOT / "bench-client" / "vstl_qc_tests.py"
TUI_PATH = ROOT / "bench-client" / "vstl-imaging-tui.py"


def _load_qc():
    spec = importlib.util.spec_from_file_location("vstl_qc_tests_keyboard_profile", QC_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


qc = _load_qc()
TUI_TEXT = TUI_PATH.read_text(encoding="utf-8")


def _profile(**overrides):
    values = {
        "has_numpad": False,
        "physical_layout": "US ANSI",
        "arrangement": "QWERTY",
        "print_format": "US",
        "has_pointing_stick": False,
        "has_backlight": False,
        "custom_format": "",
    }
    values.update(overrides)
    return qc.build_keyboard_profile(**values)


def test_profile_name_us_qwerty_without_backlight():
    profile = _profile()
    assert profile["name"] == "US QWERTY WITHOUT BACK LIGHT"
    assert profile["numeric"] == "WITHOUT NUMERIC KEYPAD"


def test_profile_name_uk_qwerty_with_backlight_and_numpad():
    profile = _profile(
        has_numpad=True,
        physical_layout="UK ISO",
        print_format="UK",
        has_backlight=True,
    )
    assert profile["name"] == "UK QWERTY WITH BACK LIGHT"
    assert profile["physical_layout"] == "UK ISO"
    assert profile["numeric"] == "WITH NUMERIC KEYPAD"


def test_profile_name_french_azerty_with_backlight():
    profile = _profile(
        arrangement="AZERTY",
        print_format="FRENCH",
        has_backlight=True,
    )
    assert profile["name"] == "FRENCH AZERTY WITH BACK LIGHT"


def test_profile_name_qwertz_and_pointing_stick():
    profile = _profile(
        arrangement="QWERTZ",
        print_format="GERMAN",
        has_pointing_stick=True,
    )
    assert profile["name"] == "GERMAN QWERTZ WITHOUT BACK LIGHT"
    assert profile["pointing_stick"] == "WITH POINTING STICK"


def test_arabic_print_is_preserved_in_name():
    profile = _profile(
        print_format="US WITH ARABIC PRINT",
        has_backlight=True,
    )
    assert profile["name"] == "US QWERTY WITH ARABIC PRINT WITH BACK LIGHT"
    assert profile["has_arabic_print"] is True


def test_other_format_is_normalized_and_limited():
    profile = _profile(
        print_format="OTHER",
        custom_format="  canadian   french!!!  ",
    )
    assert profile["print_format"] == "CANADIAN FRENCH"
    assert profile["name"] == "CANADIAN FRENCH QWERTY WITHOUT BACK LIGHT"


def test_keyboard_profile_evidence_contains_structured_choices():
    evidence = qc.keyboard_profile_evidence(_profile(has_numpad=True))
    assert "profile=US QWERTY WITHOUT BACK LIGHT" in evidence
    assert "physical_layout=US ANSI" in evidence
    assert "numeric=WITH NUMERIC KEYPAD" in evidence


def test_tui_uses_guided_profile_for_keyboard_map_and_payload():
    assert "def screen_keyboard_profile_wizard" in TUI_TEXT
    assert "Choose keyboard backlight availability:" in TUI_TEXT
    assert "Other - type manually" in TUI_TEXT
    assert 'show_numpad = bool(profile.get("has_numpad"))' in TUI_TEXT
    assert 'rows.insert(-2, ("ISO key", [ecodes.KEY_102ND]))' in TUI_TEXT
    assert 'labels[ecodes.KEY_SEMICOLON] = "M"' in TUI_TEXT
    assert 'labels[ecodes.KEY_Y] = "Z"' in TUI_TEXT
    assert 'payload["keyboard_profile"] = keyboard_profile' in TUI_TEXT
    assert 'payload["keyboard_language"] = keyboard_profile.get("print_format", "")' in TUI_TEXT
