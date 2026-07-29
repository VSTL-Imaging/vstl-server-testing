#!/usr/bin/env bash
# =============================================================================
# Regression test for INSTALL.sh placeholder-guard
# =============================================================================
# Real-world incident (2026-05-08): a fresh deployment was built with the
# default `.env.example` placeholder key (`REPLACE_ME_WITH_REAL_API_KEY`) and
# shipped to PXE. The bench got HTTP 401 because the placeholder is not a real
# imaging API key. This test ensures INSTALL.sh now refuses to run when a
# placeholder is detected, and accepts a properly-formatted real key.
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SH="$SCRIPT_DIR/../INSTALL.sh"

if [[ ! -f "$INSTALL_SH" ]]; then
    echo "FAIL: INSTALL.sh not found at $INSTALL_SH"
    exit 1
fi

# Set up an isolated temp dir so we don't touch the real .env.
TEST_DIR=$(mktemp -d)
trap "rm -rf $TEST_DIR" EXIT

# Copy INSTALL.sh + a stub .env.example so it has something to "create from".
cp "$INSTALL_SH" "$TEST_DIR/INSTALL.sh"
cp "$SCRIPT_DIR/../.env.example" "$TEST_DIR/.env.example"
# Make supporting scripts no-ops so we only test the config-sanity step.
touch "$TEST_DIR/04_build_live_iso_clonezilla.sh"
touch "$TEST_DIR/06_setup_pxe_netboot.sh"
chmod +x "$TEST_DIR/04_build_live_iso_clonezilla.sh" "$TEST_DIR/06_setup_pxe_netboot.sh"
mkdir -p "$TEST_DIR/build"
echo "stub" > "$TEST_DIR/build/vstl-live-amd64.iso"

PASS=0
FAIL=0

run_case() {
    local name="$1" env_content="$2" expect="$3"  # expect=fail or expect=pass-config
    echo "$env_content" > "$TEST_DIR/.env"
    chmod 600 "$TEST_DIR/.env"

    # Run INSTALL.sh with --pxe-only so we skip the actual ISO build.
    # We only care whether config sanity passes or hard-fails.
    cd "$TEST_DIR"
    output=$(bash INSTALL.sh --pxe-only 2>&1) || rc=$? && rc=${rc:-0}
    cd - >/dev/null

    if [[ "$expect" == "fail" ]]; then
        if [[ $rc -ne 0 ]]; then
            echo "  PASS: $name (correctly failed, rc=$rc)"
            PASS=$((PASS+1))
        else
            echo "  FAIL: $name — expected failure but got rc=0"
            echo "    output: $output" | head -3
            FAIL=$((FAIL+1))
        fi
    else
        # expect=pass-config: config sanity should pass (rc may still be non-zero from stub
        # 06_setup_pxe_netboot.sh, but config sanity output should show "VSTL_API_KEY format looks valid")
        if echo "$output" | grep -q "VSTL_API_KEY format looks valid"; then
            echo "  PASS: $name (config sanity accepted real key)"
            PASS=$((PASS+1))
        else
            echo "  FAIL: $name — config sanity rejected what should be a valid key"
            echo "    output: $output" | head -5
            FAIL=$((FAIL+1))
        fi
    fi
    rc=0
}

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "NOTE: This test must be run as root (the INSTALL.sh has a sudo guard)."
    echo "      Skipping (would otherwise fail at the first uid check, not the placeholder check)."
    exit 0
fi

echo "=== INSTALL.sh placeholder-guard regression tests ==="

run_case "placeholder REPLACE_ME_WITH_REAL_API_KEY (the actual incident)" \
'VSTL_API_BASE="https://x"
VSTL_API_KEY="REPLACE_ME_WITH_REAL_API_KEY"' "fail"

run_case "placeholder REPLACE_AT_ISO_BUILD_TIME (legacy form)" \
'VSTL_API_BASE="https://x"
VSTL_API_KEY="REPLACE_AT_ISO_BUILD_TIME"' "fail"

run_case "placeholder YOUR_API_KEY_HERE" \
'VSTL_API_BASE="https://x"
VSTL_API_KEY="YOUR_API_KEY_HERE"' "fail"

run_case "missing VSTL_API_KEY entirely" \
'VSTL_API_BASE="https://x"' "fail"

run_case "wrong prefix (typo: vstl_img → vstl_image)" \
'VSTL_API_BASE="https://x"
VSTL_API_KEY="vstl_image_ccd6338d249d86cc1951600775c83c8c71a7c27cbf9b0ba5"' "fail"

run_case "missing prefix entirely" \
'VSTL_API_BASE="https://x"
VSTL_API_KEY="ccd6338d249d86cc1951600775c83c8c71a7c27cbf9b0ba5"' "fail"

run_case "real-format key passes" \
'VSTL_API_BASE="https://x"
VSTL_API_KEY="vstl_img_ccd6338d249d86cc1951600775c83c8c71a7c27cbf9b0ba5"' "pass-config"

run_case "real-format key with leading whitespace passes" \
'VSTL_API_BASE="https://x"
VSTL_API_KEY="  vstl_img_ccd6338d249d86cc1951600775c83c8c71a7c27cbf9b0ba5  "' "pass-config"

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
