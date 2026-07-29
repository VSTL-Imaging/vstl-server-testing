#!/usr/bin/env bash
# Keep the FOG image export available without touching image or report data.
set -u

SERVICE="nfs-server.service"
REQUIRED_EXPORT="/images/dev"
LOCK_FILE="/run/lock/vstl-nfs-health.lock"
TAG="vstl-nfs-health"

log() {
    logger -t "$TAG" -- "$*"
    printf '%s\n' "$*"
}

has_required_export() {
    exportfs -v 2>/dev/null | awk -v required="$REQUIRED_EXPORT" \
        '$1 == required { found = 1 } END { exit(found ? 0 : 1) }'
}

recover_service() {
    systemctl reset-failed "$SERVICE" 2>/dev/null || true
    if systemctl restart "$SERVICE"; then
        return 0
    fi

    # NFSD can fail with errno 12 while most RAM is reclaimable filesystem
    # cache. Flush dirty data and reclaim caches only after a normal restart
    # has failed, then retry once.
    log "Normal NFS restart failed; reclaiming filesystem caches before one retry."
    sync
    printf '3\n' > /proc/sys/vm/drop_caches
    sleep 2
    systemctl reset-failed "$SERVICE" 2>/dev/null || true
    systemctl restart "$SERVICE"
}

mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if command -v flock >/dev/null 2>&1; then
    flock -n 9 || exit 0
fi

if systemctl is-active --quiet "$SERVICE" && has_required_export; then
    exit 0
fi

log "NFS image export is unavailable; starting recovery."
recover_service || {
    log "NFS recovery failed: $SERVICE could not be started."
    exit 1
}

exportfs -ra || {
    log "NFS recovery failed: exportfs could not publish exports."
    exit 1
}

if ! systemctl is-active --quiet "$SERVICE" || ! has_required_export; then
    log "NFS recovery failed: $REQUIRED_EXPORT is still unavailable."
    exit 1
fi

log "NFS recovery completed; $REQUIRED_EXPORT is active."
