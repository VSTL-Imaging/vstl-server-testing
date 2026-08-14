#!/usr/bin/env bash
set -euo pipefail

# Deploy only bench-client/rootfs logic. This intentionally does not modify
# DHCP, TFTP, iPXE boot files, kernel, initrd, or other PXE boot-chain files.

TS="$(date +%Y%m%d_%H%M%S)"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/vstl-imaging-phase1}"
PXE_ROOT="${PXE_ROOT:-/var/www/html/vstl-pxe}"
IMAGES_DIR="${IMAGES_DIR:-/images/dev}"
BACKUP_ROOT="${BACKUP_ROOT:-/opt/vstl-backups}"
BACKUP_DIR="$BACKUP_ROOT/bench-client-deploy-$TS"
BUILD_INFO_JSON="$BACKUP_DIR/vstl_build_info.json"
BUILD_INFO_CURRENT="$BACKUP_ROOT/vstl-build-current.json"
BUILD_HISTORY_JSONL="$BACKUP_ROOT/build-version-history.jsonl"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_BENCH="$SRC_ROOT/bench-client"
. "$SRC_ROOT/tools/live_rootfs_permissions.sh"

ROOTFS_DIR="${ROOTFS_DIR:-$INSTALL_ROOT/build/cz/rootfs}"
if [[ ! -d "$ROOTFS_DIR/opt/vstl" && -d "$INSTALL_ROOT/build/active-rootfs-patch/opt/vstl" ]]; then
  ROOTFS_DIR="$INSTALL_ROOT/build/active-rootfs-patch"
fi
ROOTFS_VSTL="$ROOTFS_DIR/opt/vstl"
BUILD_SQUASHFS="$INSTALL_ROOT/build/cz/extract/live/filesystem.squashfs"
PXE_SQUASHFS="$PXE_ROOT/filesystem.squashfs"

GIT_SHA="$(git -C "$SRC_ROOT" rev-parse --short=12 HEAD 2>/dev/null || true)"
GIT_BRANCH="$(git -C "$SRC_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
SERVER_ROLE="${VSTL_SERVER_ROLE:-}"
if [[ -z "$SERVER_ROLE" ]]; then
  case "$INSTALL_ROOT" in
    *phase1*|*testing*) SERVER_ROLE="testing" ;;
    *) SERVER_ROLE="main" ;;
  esac
fi
SERVER_ROLE_SLUG="$(printf '%s' "$SERVER_ROLE" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed -E 's/-+/-/g; s/^-//; s/-$//')"
SERVER_ROLE_SLUG="${SERVER_ROLE_SLUG:-server}"
BUILD_DEPLOYED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BUILD_VERSION="${VSTL_BUILD_VERSION:-VSTL-${SERVER_ROLE_SLUG}-${TS}${GIT_SHA:+-$GIT_SHA}}"

json_escape() {
  local value="${1:-}"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/ }"
  value="${value//$'\r'/ }"
  printf '%s' "$value"
}

write_build_info_json() {
  local out="$1"
  local filesystem_sha="${2:-}"
  {
    printf '{\n'
    printf '  "build_version": "%s",\n' "$(json_escape "$BUILD_VERSION")"
    printf '  "deployed_at": "%s",\n' "$(json_escape "$BUILD_DEPLOYED_AT")"
    printf '  "server_role": "%s",\n' "$(json_escape "$SERVER_ROLE")"
    printf '  "git_sha": "%s",\n' "$(json_escape "$GIT_SHA")"
    printf '  "git_branch": "%s",\n' "$(json_escape "$GIT_BRANCH")"
    printf '  "source_root": "%s",\n' "$(json_escape "$SRC_ROOT")"
    printf '  "install_root": "%s",\n' "$(json_escape "$INSTALL_ROOT")"
    printf '  "pxe_root": "%s"' "$(json_escape "$PXE_ROOT")"
    if [[ -n "$filesystem_sha" ]]; then
      printf ',\n  "filesystem_sha256": "%s"\n' "$(json_escape "$filesystem_sha")"
    else
      printf '\n'
    fi
    printf '}\n'
  } > "$out"
}

append_build_history() {
  mkdir -p "$(dirname "$BUILD_HISTORY_JSONL")"
  tr '\n' ' ' < "$BUILD_INFO_JSON" | sed -E 's/[[:space:]]+/ /g; s/[[:space:]]+$//' >> "$BUILD_HISTORY_JSONL"
  printf '\n' >> "$BUILD_HISTORY_JSONL"
}

FILES=(
  "vstl-imaging-client.sh"
  "vstl-bench-entry.sh"
  "vstl_network_setup.py"
  "vstl-imaging-tui.py"
  "vstl_hw_detect.py"
  "vstl_qc_tests.py"
  "vstl_burn_stress.py"
  "vstl_image_capture.py"
  "vstl_image_restore.py"
  "vstl_secure_erase.py"
  "vstl_lock_audit.py"
)

need_file() {
  local path="$1"
  if [[ ! -e "$path" ]]; then
    echo "ERROR: missing required path: $path" >&2
    exit 1
  fi
}

need_file "$SRC_BENCH"
need_file "$INSTALL_ROOT"
need_file "$ROOTFS_VSTL"
need_file "$PXE_ROOT"

for f in "${FILES[@]}"; do
  need_file "$SRC_BENCH/$f"
done

command -v mksquashfs >/dev/null 2>&1 || {
  echo "ERROR: mksquashfs is required on the FOG server." >&2
  exit 1
}

mkdir -p "$BACKUP_DIR"
write_build_info_json "$BUILD_INFO_JSON"
cp -a "$BUILD_INFO_JSON" "$BUILD_INFO_CURRENT"

echo "=== VSTL bench-client live deploy ==="
echo "Timestamp     : $TS"
echo "Build version : $BUILD_VERSION"
echo "Source        : $SRC_BENCH"
echo "Install root  : $INSTALL_ROOT"
echo "PXE root      : $PXE_ROOT"
echo "Backup dir    : $BACKUP_DIR"
echo

echo "=== Backing up current live state ==="
tar -czf "$BACKUP_DIR/install-root-bench-client-and-rootfs-vstl.tar.gz" \
  "$INSTALL_ROOT/bench-client" "$ROOTFS_VSTL"

if [[ -f "$BUILD_SQUASHFS" ]]; then
  cp -a "$BUILD_SQUASHFS" "$BACKUP_DIR/filesystem.squashfs.build.bak"
fi

if [[ -f "$PXE_SQUASHFS" ]]; then
  cp -a "$PXE_SQUASHFS" "$BACKUP_DIR/filesystem.squashfs.pxe.bak"
fi

find "$PXE_ROOT" -maxdepth 1 -type f \( -name '*.ipxe' -o -name '*.efi' -o -name 'vmlinuz' -o -name 'initrd*' \) \
  -print0 | tar --null -T - -czf "$BACKUP_DIR/pxe-boot-chain-files-readonly-snapshot.tar.gz" || true

echo "=== Installing patched bench-client files into rootfs ==="
mkdir -p "$INSTALL_ROOT/bench-client" "$ROOTFS_VSTL"
find "$SRC_BENCH" -maxdepth 1 -type f -print0 | while IFS= read -r -d '' src; do
  f="$(basename "$src")"
  dst="$INSTALL_ROOT/bench-client/$f"
  if [[ "$(readlink -f "$src")" != "$(readlink -f "$dst" 2>/dev/null || printf '%s' "$dst")" ]]; then
    install -m 0644 "$src" "$dst"
  fi
done
for f in "${FILES[@]}"; do
  install -m 0644 "$SRC_BENCH/$f" "$ROOTFS_VSTL/$f"
done
install -m 0644 "$BUILD_INFO_JSON" "$INSTALL_ROOT/bench-client/vstl_build_info.json"
install -m 0644 "$BUILD_INFO_JSON" "$ROOTFS_VSTL/vstl_build_info.json"
if [[ -d "$SRC_BENCH/sounds" ]]; then
  if [[ "$(readlink -f "$SRC_BENCH/sounds")" != "$(readlink -f "$INSTALL_ROOT/bench-client/sounds" 2>/dev/null || printf '%s' "$INSTALL_ROOT/bench-client/sounds")" ]]; then
    rm -rf "$INSTALL_ROOT/bench-client/sounds"
  fi
  rm -rf "$ROOTFS_VSTL/sounds"
  mkdir -p "$INSTALL_ROOT/bench-client" "$ROOTFS_VSTL"
  if [[ "$(readlink -f "$SRC_BENCH/sounds")" != "$(readlink -f "$INSTALL_ROOT/bench-client/sounds" 2>/dev/null || printf '%s' "$INSTALL_ROOT/bench-client/sounds")" ]]; then
    cp -a "$SRC_BENCH/sounds" "$INSTALL_ROOT/bench-client/sounds"
  fi
  cp -a "$SRC_BENCH/sounds" "$ROOTFS_VSTL/sounds"
fi
chmod +x "$INSTALL_ROOT/bench-client/"*.sh "$ROOTFS_VSTL/"*.sh 2>/dev/null || true
chmod +x "$INSTALL_ROOT/bench-client/vstl-bench-entry.sh" "$ROOTFS_VSTL/vstl-bench-entry.sh"
chmod +x "$INSTALL_ROOT/bench-client/vstl-imaging-tui.py" "$ROOTFS_VSTL/vstl-imaging-tui.py"

echo "=== Disabling duplicate VSTL systemd auto-start in PXE rootfs ==="
rm -f "$ROOTFS_DIR/etc/systemd/system/multi-user.target.wants/vstl-imaging.service" 2>/dev/null || true

echo "=== Captured image archive step ==="
if [[ "${ARCHIVE_OLD_IMAGES:-0}" == "1" && -d "$IMAGES_DIR" ]]; then
  ARCHIVE_DIR="$IMAGES_DIR/_archived_pre_new_naming_$TS"
  mkdir -p "$ARCHIVE_DIR"
  shopt -s nullglob dotglob
  moved=0
  for item in "$IMAGES_DIR"/*; do
    base="$(basename "$item")"
    case "$base" in
      _archived_pre_new_naming_*|lost+found|.vstl-secure-erase)
        continue
        ;;
    esac
    mv "$item" "$ARCHIVE_DIR/"
    moved=$((moved + 1))
  done
  shopt -u nullglob dotglob
  echo "Archived $moved existing image item(s) to $ARCHIVE_DIR"
elif [[ "${ARCHIVE_OLD_IMAGES:-0}" == "1" ]]; then
  echo "Images dir not present yet: $IMAGES_DIR"
else
  echo "Skipped image archive. Set ARCHIVE_OLD_IMAGES=1 only when intentionally migrating old captured-image naming."
fi

echo "=== Rebuilding filesystem.squashfs only ==="
echo "=== Repairing live rootfs ownership/modes ==="
repair_live_rootfs_permissions "$ROOTFS_DIR"

tmp_squash="$BUILD_SQUASHFS.new"
rm -f "$tmp_squash"
mksquashfs "$ROOTFS_DIR" "$tmp_squash" -comp xz -b 1048576 -Xbcj x86 -noappend -no-progress
mv "$tmp_squash" "$BUILD_SQUASHFS"
cp -a "$BUILD_SQUASHFS" "$PXE_SQUASHFS"
SQUASH_SHA="$(sha256sum "$PXE_SQUASHFS" | awk '{print $1}')"
write_build_info_json "$BUILD_INFO_JSON" "$SQUASH_SHA"
cp -a "$BUILD_INFO_JSON" "$BUILD_INFO_CURRENT"
cp -a "$BUILD_INFO_JSON" "$INSTALL_ROOT/bench-client/vstl_build_info.json"
append_build_history

echo "=== Writing full setup tar backup ==="
SETUP_TAR_TS="$BACKUP_ROOT/vstl-imaging-setup-$TS.tar.gz"
SETUP_TAR_CURRENT="$BACKUP_ROOT/vstl-imaging-setup-current.tar.gz"
tar -czf "$SETUP_TAR_TS" \
  --exclude="$INSTALL_ROOT/build/cz/rootfs/proc/*" \
  --exclude="$INSTALL_ROOT/build/cz/rootfs/sys/*" \
  --exclude="$INSTALL_ROOT/build/cz/rootfs/dev/*" \
  "$INSTALL_ROOT" "$PXE_ROOT"
cp -a "$SETUP_TAR_TS" "$SETUP_TAR_CURRENT"

sha256sum "$BUILD_SQUASHFS" "$PXE_SQUASHFS" "$SETUP_TAR_TS" "$SETUP_TAR_CURRENT" | tee "$BACKUP_DIR/deploy-sha256.txt"

echo
echo "=== Deploy complete ==="
echo "Rollback backup       : $BACKUP_DIR"
echo "Build info current    : $BUILD_INFO_CURRENT"
echo "Build history         : $BUILD_HISTORY_JSONL"
echo "Setup tar timestamped : $SETUP_TAR_TS"
echo "Setup tar current     : $SETUP_TAR_CURRENT"
echo "PXE boot chain        : unchanged"
