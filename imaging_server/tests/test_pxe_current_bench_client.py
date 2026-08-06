from pathlib import Path


ROOT = Path(__file__).parent.parent
PXE_SETUP = (ROOT / "06_setup_pxe_netboot.sh").read_text(encoding="utf-8")
LIVE_DEPLOY = (ROOT / "tools" / "deploy_bench_client_live.sh").read_text(encoding="utf-8")
TUI = (ROOT / "bench-client" / "vstl-imaging-tui.py").read_text(encoding="utf-8")


def test_pxe_setup_reinjects_current_bench_client_into_squashfs():
    assert 'BENCH_DIR="$SCRIPT_DIR/bench-client"' in PXE_SETUP
    assert "BENCH_RUNTIME_FILES=(" in PXE_SETUP
    assert 'unsquashfs -d "$PATCH_ROOT/rootfs"' in PXE_SETUP
    assert 'install -m 0755 "$src" "$PATCH_ROOT/rootfs/opt/vstl/$f"' in PXE_SETUP
    assert 'mksquashfs "$PATCH_ROOT/rootfs" "$PATCHED_SQUASHFS"' in PXE_SETUP
    assert '. "$SCRIPT_DIR/tools/live_rootfs_permissions.sh"' in PXE_SETUP
    assert 'repair_live_rootfs_permissions "$PATCH_ROOT/rootfs"' in PXE_SETUP


def test_pxe_setup_allows_explicit_server_ip_to_override_env_file():
    assert 'ENV_SERVER_IP="${SERVER_IP:-}"' in PXE_SETUP
    assert 'ENV_PXE_SERVER_IP="${VSTL_PXE_SERVER_IP:-}"' in PXE_SETUP
    assert '[[ -n "$ENV_SERVER_IP" ]] && SERVER_IP="$ENV_SERVER_IP"' in PXE_SETUP
    assert '[[ -n "$ENV_PXE_SERVER_IP" ]] && VSTL_PXE_SERVER_IP="$ENV_PXE_SERVER_IP"' in PXE_SETUP
    assert 'SERVER_IP="${VSTL_PXE_SERVER_IP:-${SERVER_IP:-$(ip -4 -o addr show scope global' in PXE_SETUP


def test_pxe_setup_verifies_embedded_runtime_hashes():
    assert "for f in vstl-imaging-tui.py vstl-bench-entry.sh; do" in PXE_SETUP
    assert 'unsquashfs -cat "$PXE_ROOT/filesystem.squashfs" "opt/vstl/$f"' in PXE_SETUP
    assert 'die "PXE squashfs contains stale $f after rebuild"' in PXE_SETUP


def test_main_menu_key_cancel_does_not_render_infinite_countdown():
    assert 'deadline = float("inf")' in TUI
    assert "if math.isfinite(deadline):" in TUI
    assert "remaining = max(0, int(deadline - time.monotonic()))" in TUI


def test_quick_live_deploy_repairs_rootfs_before_rebuild():
    assert '. "$SRC_ROOT/tools/live_rootfs_permissions.sh"' in LIVE_DEPLOY
    assert 'repair_live_rootfs_permissions "$ROOTFS_DIR"' in LIVE_DEPLOY
    assert 'mksquashfs "$ROOTFS_DIR" "$tmp_squash"' in LIVE_DEPLOY
