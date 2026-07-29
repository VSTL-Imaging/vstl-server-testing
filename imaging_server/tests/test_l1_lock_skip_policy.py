from pathlib import Path


TUI = (
    Path(__file__).resolve().parents[1]
    / "bench-client"
    / "vstl-imaging-tui.py"
).read_text(encoding="utf-8")


def test_l1_skip_is_limited_to_mdm_bios_and_drive_password_findings():
    assert (
        'L1_SKIPPABLE_LOCK_KEYS = MDM_LOCK_KEYS | {"bios_password", "ata_security"}'
        in TUI
    )
    assert "detected.issubset(L1_SKIPPABLE_LOCK_KEYS)" in TUI
    assert 'tech == "L1" and _l1_can_continue_lock_audit(audit)' in TUI


def test_l1_skip_is_visible_as_k_and_not_offered_as_admin_override():
    assert "[ K ]  L1 skip MDM/BIOS/drive-lock warning and continue" in TUI
    assert 'footer = "Press K / S"' in TUI
    assert 'ch in (ord("k"), ord("K"))' in TUI
    assert "show_override_actions = not can_l1_continue" in TUI
