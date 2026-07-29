from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TUI_TEXT = (ROOT / "bench-client" / "vstl-imaging-tui.py").read_text(encoding="utf-8")


def test_qc_completion_screen_exposes_keyboard_back_navigation():
    assert "def screen_qc_step_complete(" in TUI_TEXT
    active = TUI_TEXT[TUI_TEXT.index("def screen_qc_step_complete("):]
    active = active[:active.index("def _qc_color_cycle(")]

    assert "Go Back to Previous Test" in active
    assert 'return "back"' in active
    assert 'return "retest"' in active
    assert 'return "next"' in active
    assert "not available on first test" in active


def test_qc_loop_replaces_results_when_operator_goes_back():
    active = TUI_TEXT[TUI_TEXT.index("test_keys = qc.applicable_tests_for(tech)"):]
    active = active[:active.index("qc_summary = qc.summarize(results)")]

    assert "completed_qc_results: dict[str, dict] = {}" in active
    assert "while qc_index < len(test_keys):" in active
    assert "completed_qc_results[key] = screen_fn(stdscr)" in active
    assert "nav_action = screen_qc_step_complete(" in active
    assert 'if nav_action == "back":' in active
    assert "qc_index = max(0, qc_index - 1)" in active
    assert 'if nav_action == "retest":' in active
    assert "qc_index += 1" in active
    assert "results.extend(" in active
