from pathlib import Path


ROOT = Path(__file__).parent.parent
BENCH = ROOT / "bench-client"
DEPLOY = (ROOT / "tools" / "deploy_bench_client_live.sh").read_text(encoding="utf-8")
BUILD_ISO = (ROOT / "04_build_live_iso_clonezilla.sh").read_text(encoding="utf-8")

RUNTIME_FILES = [
    "vstl-imaging-client.sh",
    "vstl-bench-entry.sh",
    "vstl_network_setup.py",
    "vstl-imaging-tui.py",
    "vstl_hw_detect.py",
    "vstl_lock_audit.py",
    "vstl_qc_tests.py",
    "vstl_burn_stress.py",
    "vstl_secure_erase.py",
    "vstl_image_capture.py",
    "vstl_image_restore.py",
]


def test_all_tui_runtime_modules_are_present_in_bench_client_tree():
    for name in RUNTIME_FILES:
        assert (BENCH / name).is_file(), f"missing bench-client/{name}"


def test_live_deploy_copies_all_tui_runtime_modules_into_rootfs():
    for name in RUNTIME_FILES:
        assert f'"{name}"' in DEPLOY, f"deploy script does not include {name}"
    assert "ARCHIVE_OLD_IMAGES:-0" in DEPLOY


def test_iso_builder_bakes_all_tui_runtime_modules():
    for name in RUNTIME_FILES:
        assert name in BUILD_ISO, f"ISO builder does not include {name}"


def test_pxe_rootfs_does_not_enable_duplicate_vstl_systemd_start():
    assert "rm -f \"$ROOTFS_DIR/etc/systemd/system/multi-user.target.wants/vstl-imaging.service\"" in DEPLOY
    assert "rm -f \"$WORK_DIR/rootfs/etc/systemd/system/multi-user.target.wants/vstl-imaging.service\"" in BUILD_ISO
    assert "ocs_live_run=/opt/vstl/vstl-bench-entry.sh" in (ROOT / "06_setup_pxe_netboot.sh").read_text(encoding="utf-8")
