import ast
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TUI_PATH = ROOT / "bench-client" / "vstl-imaging-tui.py"
EXPORTER_PATH = ROOT / "reporting" / "export_reports.py"
TUI_TEXT = TUI_PATH.read_text(encoding="utf-8")


def _load_tui_functions(*names):
    tree = ast.parse(TUI_TEXT)
    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(TUI_PATH), "exec"), namespace)
    return namespace


def _load_exporter():
    spec = importlib.util.spec_from_file_location("vstl_export_reports_post_qc", EXPORTER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_post_qc_list_normalizer_ignores_empty_lines_and_dedupes():
    ns = _load_tui_functions("_post_qc_csv_from_lines")

    assert ns["_post_qc_csv_from_lines"]([
        " keyboard ",
        "",
        "touchpad",
        "Keyboard",
        "LCD",
        "  minor   scratches  ",
    ]) == "keyboard, touchpad, LCD, minor scratches"


def test_post_qc_screens_are_after_qc_summary_before_burn_and_payload_saved():
    qc_summary_pos = TUI_TEXT.index("qc_summary = qc.summarize(results)")
    qc_decision_pos = TUI_TEXT.index("qc_decision = screen_qc_summary", qc_summary_pos)
    post_qc_pos = TUI_TEXT.index("post_qc_details = screen_post_qc_details(", qc_summary_pos)
    burn_pos = TUI_TEXT.index("# 5. Phase 2C", qc_summary_pos)

    assert qc_summary_pos < qc_decision_pos < post_qc_pos < burn_pos
    assert 'qc_summary["post_qc"] = dict(post_qc_details)' in TUI_TEXT
    assert 'payload["cosmetic_grade"] = post_qc_details.get("cosmetic_grade", "")' in TUI_TEXT
    assert 'payload["parts_required"] = post_qc_details.get("parts_required", "")' in TUI_TEXT
    assert 'payload["additional_remarks"] = post_qc_details.get("additional_remarks", "")' in TUI_TEXT
    assert 'COSMETIC_GRADES = ("A+", "A", "B", "C", "D")' in TUI_TEXT


def test_post_qc_navigation_exposes_back_and_reentry_actions():
    assert "def screen_post_qc_step_complete(" in TUI_TEXT
    active = TUI_TEXT[TUI_TEXT.index("def screen_post_qc_step_complete("):]
    active = active[:active.index("def _qc_pass_fail_choice(")]

    assert "Go back to previous post-QC step" in active
    assert "Go back to previous QC test" in active
    assert 'return "back_to_qc"' in active
    assert 'return "back"' in active
    assert 'return "retest"' in active
    assert 'return "next"' in active
    assert "POST_QC_BACK_TO_QC" in active


def test_main_qc_flow_can_return_from_post_qc_to_last_qc_test():
    active = TUI_TEXT[TUI_TEXT.index("test_keys = qc.applicable_tests_for(tech)"):]
    active = active[:active.index("# 5. Phase 2C")]

    assert "while True:" in active
    assert "screen_post_qc_details(" in active
    assert "allow_back_to_qc=bool(test_keys)" in active
    assert 'post_qc_details.get("_nav") == "back_to_qc"' in active
    assert "qc_index = max(0, len(test_keys) - 1)" in active
    assert "post_qc_details = None" in active


def test_exporter_surfaces_post_qc_fields_from_nested_payload():
    exporter = _load_exporter()
    payload = {
        "session_started_at": "2026-07-07T08:00:00+00:00",
        "technician_level": "L1",
        "serial_no": "POSTQC1",
        "model": "Latitude Test",
        "qc_tests": {
            "failed": [],
            "tests": {},
            "post_qc": {
                "cosmetic_grade": "B",
                "parts_required": "keyboard, touchpad",
                "additional_remarks": "hinge loose, speaker distortion",
            },
        },
    }

    row = exporter.flatten({"payload": payload})

    assert "Cosmetic Grade" in exporter.HEADERS
    assert "Additional Remarks" in exporter.HEADERS
    assert "Cosmetic Grade" in exporter._sheet_headers("QC")
    assert "Additional Remarks" in exporter._sheet_headers("QC")
    assert "Cosmetic Grade" not in exporter._sheet_headers("Capture")
    assert row["Cosmetic Grade"] == "B"
    assert row["Parts Required"] == "keyboard, touchpad"
    assert row["Additional Remarks"] == "hinge loose, speaker distortion"
