#!/usr/bin/env bash
# Build the Phase-1 deployment tarball that admins download via
# GET /api/imaging/bundle/phase1.
#
# Re-run this any time bench-client/* or the ISO build scripts change.
# The output is saved into imaging_server/dist/ where the FastAPI download
# endpoint reads from.
set -euo pipefail

cd "$(dirname "$0")/.."   # → /app/backend/imaging_server

DIST_DIR="dist"
TARBALL="$DIST_DIR/vstl-imaging-phase1.tar.gz"
mkdir -p "$DIST_DIR"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Stage everything we need shipped to a fresh imaging server into a single
# top-level directory so `tar xzf …` produces vstl-imaging-phase1/<files>.
STAGE="$WORK/vstl-imaging-phase1"
mkdir -p "$STAGE"
rsync -a --delete \
    --exclude='build/' \
    --exclude='dist/' \
    --exclude='.pytest_cache/' \
    --exclude='__pycache__/' \
    --exclude='.env' \
    ./ "$STAGE/"

# Make sure all shipped scripts are executable on the receiving end.
chmod +x "$STAGE"/*.sh \
         "$STAGE"/bench-client/*.sh \
         "$STAGE"/bench-client/*.py 2>/dev/null || true

tar -czf "$TARBALL" -C "$WORK" vstl-imaging-phase1
sha256sum "$TARBALL" > "${TARBALL}.sha256"

echo "Built: $(ls -lh "$TARBALL" | awk '{print $5,$9}')"
echo "SHA256: $(cat "${TARBALL}.sha256" | awk '{print $1}')"
