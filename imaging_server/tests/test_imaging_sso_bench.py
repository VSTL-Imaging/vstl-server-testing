import ast
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).parent.parent
TUI = (ROOT / "bench-client" / "vstl-imaging-tui.py").read_text(encoding="utf-8")
EXPORTER = (ROOT / "reporting" / "export_reports.py").read_text(encoding="utf-8")
STATE_API = (ROOT / "reporting" / "bench-state.php").read_text(encoding="utf-8")
REPORT_INSTALLER = (ROOT / "07_install_reporting.sh").read_text(encoding="utf-8")


def load_tui_functions(*names):
    tree = ast.parse(TUI)
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    namespace = {"json": json, "hashlib": hashlib}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "vstl-imaging-tui.py", "exec"), namespace)
    return namespace


def test_bench_has_vstl360_login_contract_endpoints():
    assert '"/imaging/auth/login-pin"' in TUI
    assert '"/imaging/auth/me"' in TUI
    assert '"/imaging/auth/logout"' in TUI
    assert '"X-API-Key"' in TUI
    assert '"Authorization": f"Bearer {operator_token}"' in TUI
    assert 'base = f"{base}/api"' in TUI
    assert '"pin": pin' in TUI
    assert 'body["layer"] = layer' in TUI
    assert 'data.get("need_layer")' in TUI
    assert 'session.get("available_layers")' in TUI
    login_fn = TUI[TUI.index("def auth_login_pin("):TUI.index("def auth_me(")]
    assert '"email": email' not in login_fn
    assert '"password": password' not in login_fn


def test_login_controls_layer_before_bench_screen_access():
    login_pos = TUI.index("operator = screen_login(stdscr, cfg)")
    layer_pos = TUI.index("selected_layer = screen_working_layer(stdscr, _operator_name(operator))")
    tech_pos = TUI.index("tech = _operator_bench_mode(operator)")
    menu_pos = TUI.index("choice = screen_main_menu(stdscr, tech, operator)")
    assert login_pos < layer_pos < tech_pos < menu_pos
    assert "Login is required before VSTL Bench access." in TUI
    assert "Enter your 4-digit Imaging PIN:" in TUI
    assert "PIN must be exactly 4 digits" in TUI
    assert "Login is kept on this laptop only" in TUI
    assert "L1 and L2 users may select either L1 or L2." in TUI
    assert "there is no local override" not in TUI
    technician_fn = TUI[TUI.index("def screen_technician("):TUI.index("MENU_OPTIONS =")]
    assert "Select your technician level" not in technician_fn


def test_hidden_testing_mode_restore_only_bypasses_reporting_login_and_box_flow():
    assert "TESTING_MODE_CTRL_T = 20" in TUI
    assert 'TESTING_MODE_HOTKEY_LABEL = "Ctrl+Shift+Alt+T"' in TUI
    assert "def _testing_modifier_chord_active(" in TUI
    assert "active & ctrl_keys and active & shift_keys and active & alt_keys" in TUI
    assert "def screen_testing_mode_menu(" in TUI
    assert "5. {TESTING_RESTORE_ONLY_LABEL}" in TUI
    assert "if _operator_testing_mode(operator):" in TUI
    assert "return run_testing_restore_only(stdscr, cfg)" in TUI

    testing_flow = TUI[
        TUI.index("def run_testing_restore_only("):
        TUI.index("# ---------------------------------------------------------------------------\n# Main controller")
    ]
    assert "screen_login" not in testing_flow
    assert "ensure_operator_box" not in testing_flow
    assert "screen_lock_audit_run" not in testing_flow
    assert "post_ingest" not in testing_flow
    assert "post_local_report" not in testing_flow
    assert "LOCAL_AUDIT_FILE" not in testing_flow
    assert "phase3_secure_erase(" in testing_flow
    assert "phase3_restore(" in testing_flow
    assert "suppress_reporting=True" in testing_flow

    secure_erase_flow = TUI[
        TUI.index("def phase3_secure_erase("):
        TUI.index("def phase3_capture(")
    ]
    assert "suppress_reporting: bool = False" in secure_erase_flow
    assert "and not suppress_reporting" in secure_erase_flow
    assert "_post_secure_erase_local_report(" in secure_erase_flow
    assert "testing_mode_reporting_suppressed" in secure_erase_flow

    restore_flow = TUI[
        TUI.index("def phase3_restore("):
        TUI.index("def screen_completion(")
    ]
    assert "suppress_reporting: bool = False" in restore_flow
    assert "if suppress_reporting:" in restore_flow
    assert '"/imaging/restore/complete"' in restore_flow
    assert "log_suppressed" in restore_flow


def test_restore_approved_requires_verified_secure_erase_before_restore():
    restore_branch = TUI[
        TUI.index("if choice == 0:"):
        TUI.index("elif choice == 1:")
    ]
    assert 'phase3_results["erase"] = erase' in restore_branch
    assert 'erase_result = (erase or {}).get("result") or {}' in restore_branch
    assert 'erase_result.get("ok") and erase_result.get("verified")' in restore_branch
    assert "Restore cancelled because drive wipe did not complete." in restore_branch


def test_capture_option_uses_backend_can_capture_flag_on_bench_menu():
    assert "def _visible_menu_options(operator" in TUI
    assert "if not _operator_has_capture_access(operator):" in TUI
    assert "if idx != 3" in TUI
    assert "Capture is hidden because this operator has no Capture permission (needs Admin or the Imaging role)." in TUI
    ns = load_tui_functions(
        "_operator_user",
        "_operator_roles",
        "_truthy_operator_value",
        "_operator_is_admin",
        "_operator_has_capture_access",
    )
    assert ns["_operator_has_capture_access"]({"user": {"can_capture": True}})
    assert ns["_operator_has_capture_access"]({"user": {"can_capture": "true"}})
    assert not ns["_operator_has_capture_access"]({"user": {"is_admin": True}})
    assert not ns["_operator_has_capture_access"]({"user": {"roles": ["Imaging"]}})
    assert not ns["_operator_has_capture_access"]({"user": {"capture_access": True}})
    assert not ns["_operator_has_capture_access"]({"user": {"roles": ["Layer 2"]}})


def test_imaging_only_operator_skips_layer_and_only_sees_phase3_actions():
    ns = load_tui_functions(
        "_operator_user",
        "_operator_roles",
        "_truthy_operator_value",
        "_operator_has_capture_access",
        "_normalize_layer",
        "_operator_layer",
        "_operator_technician_level",
        "_operator_has_layer_access",
        "_operator_bench_mode",
        "_visible_menu_options",
    )
    ns["MENU_OPTIONS"] = [
        "Restore Approved System Image  (with QC Test and Certified Secure Erase)",
        "QC Test Only  (Certified Secure Erase optional)",
        "Certified Secure Erase",
        "Capture Full System Image",
    ]
    imaging_operator = {"user": {"name": "Image Operator", "roles": ["Imaging"], "layer": None, "can_capture": True}}
    assert ns["_operator_technician_level"](imaging_operator) == ""
    assert ns["_operator_has_layer_access"](imaging_operator) is False
    assert ns["_operator_bench_mode"](imaging_operator) == "IMAGING"
    assert [idx for idx, _label in ns["_visible_menu_options"](imaging_operator)] == [2, 3]

    both_layers = {"user": {"roles": ["Layer 1", "Layer 2"], "can_capture": False}}
    assert ns["_operator_technician_level"](both_layers) == ""
    assert ns["_operator_has_layer_access"](both_layers) is True


def test_logged_in_user_is_attached_to_payload_and_reports():
    assert "def _attach_operator_to_payload" in TUI
    assert 'payload["user"] = name' in TUI
    assert 'payload["technician_user_name"] = name' in TUI
    assert '_attach_operator_to_payload(payload, operator)' in TUI
    assert '"User"' in EXPORTER
    assert 'payload.get("technician_user_name")' in EXPORTER


def test_operator_session_cache_is_local_only_by_default():
    ns = load_tui_functions("_truthy_config", "_shared_operator_session_enabled")
    assert ns["_shared_operator_session_enabled"]({}) is False
    assert ns["_shared_operator_session_enabled"]({"VSTL_SHARE_OPERATOR_SESSION": ""}) is False
    assert ns["_shared_operator_session_enabled"]({"VSTL_SHARE_OPERATOR_SESSION": "0"}) is False
    assert ns["_shared_operator_session_enabled"]({"VSTL_SHARE_OPERATOR_SESSION": "1"}) is True

    load_fn = TUI[TUI.index("def _load_auth_session("):TUI.index("def _save_local_auth_session(")]
    save_fn = TUI[TUI.index("def _save_auth_session("):TUI.index("def _clear_auth_session(")]
    clear_fn = TUI[TUI.index("def _clear_auth_session("):TUI.index("def _operator_user(")]
    assert 'if cfg and _shared_operator_session_enabled(cfg):' in load_fn
    assert 'if cfg and _shared_operator_session_enabled(cfg):' in save_fn
    assert 'if cfg and _shared_operator_session_enabled(cfg):' in clear_fn


def test_shift_session_can_still_be_cached_on_the_imaging_server_when_enabled():
    assert '"GET", "session"' in TUI
    assert '"POST",\n            "session"' in TUI
    assert '"DELETE", "session"' in TUI
    assert "bench-state.php" in TUI
    assert "bench-state.php" in REPORT_INSTALLER
    assert "VSTL_SHARE_OPERATOR_SESSION" in TUI
    assert "VSTL_BENCH_CLIENT_ID" in TUI
    assert "bench_client_id" in TUI
    assert 'params["client_id"] = _bench_client_id()' in TUI
    assert "hash('sha256', $benchId)" in STATE_API
    assert "$clientId = vstl_safe_state_key" in STATE_API
    assert "$sessionDir = $benchDir . '/sessions'" in STATE_API
    assert "hash('sha256', $clientId)" in STATE_API
    assert "'token' => (string)$session['token']" in STATE_API


def test_completed_units_use_operator_owned_durable_retry_queue():
    assert "def _queue_pending_ingest(" in TUI
    assert "def flush_pending_ingests(" in TUI
    assert '"operator_user_id"' in TUI
    assert '"operator_session": _operator_session_snapshot(operator)' in TUI
    assert "def _operator_for_pending_ingest(" in TUI
    assert "clear_session_on_401=bool(current_token and token == current_token)" in TUI
    assert 'payload["bench_submission_id"]' in TUI
    assert '"POST",\n        "queue"' in TUI
    assert '"DELETE",\n        "queue"' in TUI
    assert "post_ingest(payload, cfg, operator)" in TUI
    assert "operator_user_id" in STATE_API
    assert "submission_id" in STATE_API


def test_bench_attaches_audit_submission_status_before_local_report_post():
    assert "def _attach_audit_submission_status(" in TUI
    assert 'payload["audit_submission_status"] = status' in TUI
    assert '"Audit Submitted" if ok else "Submission Failed"' in TUI
    submit_pos = TUI.index("ok, msg = post_ingest(payload, cfg, operator)")
    attach_pos = TUI.index("_attach_audit_submission_status(payload, ok, msg)", submit_pos)
    local_pos = TUI.index("local_ok, local_msg = post_local_report(payload, cfg)", attach_pos)
    completion_pos = TUI.index("screen_completion(stdscr, ident, ok, msg, tech, choice", local_pos)
    assert submit_pos < attach_pos < local_pos < completion_pos


def test_successful_cloud_audit_releases_dhcp_after_operator_restart_prompt():
    assert "def release_successful_audit_dhcp_lease(" in TUI
    assert "not releasing DHCP lease; audit was not submitted" in TUI
    assert 'commands.append(["dhclient", "-r", iface])' in TUI
    assert '"VSTL_RELEASE_DHCP_ON_AUDIT_SUBMITTED"' in TUI
    completion_pos = TUI.index("screen_completion(stdscr, ident, ok, msg, tech, choice")
    release_pos = TUI.index("release_successful_audit_dhcp_lease(cfg, ok)", completion_pos)
    return_pos = TUI.index("return 0", release_pos)
    assert completion_pos < release_pos < return_pos


def test_local_report_uses_api_key_fallback_and_is_non_blocking_when_cloud_saved():
    assert 'token = cfg.get("VSTL_REPORT_TOKEN") or cfg.get("VSTL_API_KEY", "")' in TUI
    assert "local report token unavailable; cloud audit still uses VSTL_API_KEY" in TUI
    completion_fn = TUI[TUI.index("def screen_completion("):TUI.index("# ---------------------------------------------------------------------------\n# Main controller")]
    assert "cloud_saved = bool(ingest_ok)" in completion_fn
    assert "✅  Cloud audit submitted" in completion_fn
    assert 'local_report_status = "saved" if local_report_ok else "warning"' in completion_fn
    assert "Server report save failed" not in completion_fn


def test_ingest_response_surfaces_reconcile_and_l1_status():
    assert "Not in this box - flagged for reconcile" in TUI
    assert "Imaged into" in TUI
    assert "official L1 audit completed" in TUI
    assert "asset matched by" in TUI
    assert "operator session expired; audit queued safely" in TUI


def test_l1_box_picker_is_wired_before_menu_and_payload_ingest():
    assert '"/imaging/my-boxes"' in TUI
    fetch_fn = TUI[TUI.index("def fetch_my_boxes("):TUI.index("def auth_logout(")]
    assert "token=token" in fetch_fn
    assert "include_key=True" not in fetch_fn

    tech_pos = TUI.index("tech = _operator_bench_mode(operator)")
    box_pos = TUI.index('if tech == "L1" and not ensure_operator_box(stdscr, cfg, operator):')
    menu_pos = TUI.index("choice = screen_main_menu(stdscr, tech, operator)")
    assert tech_pos < box_pos < menu_pos
    assert "if choice != BOX_SWITCH_CHOICE:" in TUI
    assert "screen_box_picker(stdscr, cfg, operator)" in TUI
    assert "_attach_box_scope_to_payload(payload, operator)" in TUI
    assert "No boxes assigned to you. Ask your supervisor to allocate a box." in TUI
    assert "Legacy bench operation can continue" not in TUI
    assert "ENTER legacy mode" not in TUI
    assert 'detail = _box_model_label(box)' in TUI
    assert 'f"{box[\'remaining\']} left / {box[\'total\']}"' in TUI


def test_box_scope_attaches_only_to_l1_payloads_and_refreshes_after_ingest():
    ns = load_tui_functions(
        "_operator_user",
        "_operator_roles",
        "_normalize_layer",
        "_operator_layer",
        "_operator_technician_level",
        "_nonnegative_int",
        "_normalize_box_scope",
        "_operator_box",
        "_attach_box_scope_to_payload",
        "_box_model_label",
    )
    box = {
        "lot_no": "LOT-2206",
        "box_no": "BOX NO 01",
        "total": 20,
        "remaining": 13,
        "imaged": 7,
        "brand": "HP",
        "model": "HP EliteBook 640 14 inch G9",
        "model_label": "HP EliteBook 640 14 inch G9",
        "model_count": 1,
    }
    normalized = ns["_normalize_box_scope"](box)
    assert normalized["model_label"] == "HP EliteBook 640 14 inch G9"
    assert normalized["model_count"] == 1
    assert ns["_box_model_label"](normalized) == "HP EliteBook 640 14 inch G9"
    fallback = ns["_normalize_box_scope"]({**box, "model_label": "", "brand": "DELL", "model": "Latitude 5530"})
    assert ns["_box_model_label"](fallback) == "DELL Latitude 5530"
    mixed = ns["_normalize_box_scope"]({**box, "model_label": "Mixed - 3 models", "model_count": 3})
    assert ns["_box_model_label"](mixed) == "Mixed - 3 models"

    l1_payload = {}
    ns["_attach_box_scope_to_payload"](
        l1_payload,
        {"selected_layer": "Layer 1", "selected_box": box, "user": {}},
    )
    assert l1_payload["lot_no"] == "LOT-2206"
    assert l1_payload["lot_number"] == "LOT-2206"
    assert l1_payload["box_no"] == "BOX NO 01"
    assert l1_payload["box_number"] == "BOX NO 01"
    assert l1_payload["box_model_label"] == "HP EliteBook 640 14 inch G9"
    assert l1_payload["box_total"] == 20
    assert l1_payload["box_imaged"] == 7
    assert l1_payload["box_remaining"] == 13
    assert l1_payload["box_model_count"] == 1
    assert l1_payload["raw_data"]["box_scope"]["remaining"] == 13

    l2_payload = {}
    ns["_attach_box_scope_to_payload"](
        l2_payload,
        {"selected_layer": "Layer 2", "selected_box": box, "user": {}},
    )
    assert "lot_no" not in l2_payload
    assert "box_no" not in l2_payload

    assert 'if data.get("slot_filled"):' in TUI
    flush_fn = TUI[TUI.index("def flush_pending_ingests("):TUI.index("def post_ingest(")]
    assert "_refresh_operator_boxes_after_ingest(cfg, operator)" in flush_fn
    assert "_decrement_operator_box" not in TUI
    assert "VSTL 360 owns slot counts and auto-closes completed allocations" in TUI


def test_box_slot_fill_and_reconcile_responses_are_operator_readable():
    ns = load_tui_functions(
        "_nonnegative_int",
        "_normalize_box_scope",
        "_format_ingest_response",
    )
    payload = {"lot_no": "LOT-2206", "box_no": "BOX NO 01"}
    filled = ns["_format_ingest_response"](
        {"slot_filled": True, "l1_completed": True}, payload
    )
    assert filled == "Imaged into LOT-2206 / BOX NO 01 - L1 complete"

    overflow = ns["_format_ingest_response"](
        {"asset_created": True, "match_method": "bench_created"}, payload
    )
    assert overflow.startswith("WARNING: Not in this box - flagged for reconcile")


def test_layer_normalization_prefers_local_bench_selection_over_cloud_layer():
    ns = load_tui_functions(
        "_operator_user",
        "_operator_roles",
        "_normalize_layer",
        "_operator_layer",
        "_operator_technician_level",
    )
    assert ns["_normalize_layer"]("L1") == "Layer 1"
    assert ns["_normalize_layer"]("layer_2") == "Layer 2"
    assert ns["_operator_technician_level"](
        {"selected_layer": "Layer 1", "user": {"layer": "Layer 2", "roles": ["Layer 2"]}}
    ) == "L1"
    assert ns["_operator_technician_level"](
        {"selected_layer": "L2", "user": {"layer": "Layer 1", "roles": ["Layer 1"]}}
    ) == "L2"
    assert ns["_operator_technician_level"]({"user": {"layer": "Layer 2"}}) == "L2"
    assert ns["_operator_technician_level"]({"user": {"roles": ["Layer 1"]}}) == "L1"
    assert ns["_operator_technician_level"](
        {"user": {"roles": ["Layer 1", "Layer 2"]}}
    ) == ""


def test_login_pin_handles_need_layer_and_persists_selected_layer():
    ns = load_tui_functions(
        "_bench_id",
        "_normalize_layer",
        "_auth_session_from_response",
        "auth_login_pin",
    )
    saved = []
    ns["_save_auth_session"] = lambda session, cfg: saved.append((session, cfg))
    ns["_auth_request"] = lambda *args, **kwargs: (
        True,
        {
            "ok": False,
            "need_layer": True,
            "available_layers": ["Layer 1", "Layer 2"],
            "operator_name": "Rahul",
        },
        "ok",
        200,
    )
    ok, response, message = ns["auth_login_pin"]({"BENCH_ID": "B1"}, "0427")
    assert not ok
    assert response["need_layer"] is True
    assert "layer" in message

    ns["_auth_request"] = lambda *args, **kwargs: (
        True,
        {
            "ok": True,
            "token": "jwt",
            "selected_layer": "Layer 2",
            "user": {"id": "u1", "name": "Rahul", "roles": ["Layer 1", "Layer 2"]},
        },
        "ok",
        200,
    )
    ok, session, _message = ns["auth_login_pin"]({"BENCH_ID": "B1"}, "0427", "Layer 2")
    assert ok
    assert session["selected_layer"] == "Layer 2"
    assert session["user"]["layer"] == "Layer 2"
    assert saved[-1][0]["token"] == "jwt"


def test_pin_login_repairs_bad_rtc_on_tls_certificate_verify_failure():
    assert "import ssl" in TUI
    assert "from email.utils import parsedate_to_datetime" in TUI
    assert "def _repair_clock_after_cert_error(" in TUI
    assert 'method="HEAD"' in TUI
    assert '"Date"' in TUI
    assert '["date", "-u", "-s", f"@{epoch}"]' in TUI
    assert "ssl._create_unverified_context()" in TUI
    auth_fn = TUI[TUI.index("def _auth_request("):TUI.index("def _auth_session_from_response(")]
    assert "_certificate_verify_failed(e) and _repair_clock_after_cert_error(cfg)" in auth_fn
    assert "Authorization" not in TUI[
        TUI.index("def _read_server_http_date_epoch("):TUI.index("def _set_system_clock_utc(")
    ]
