#!/usr/bin/env bash
# Install a small systemd timer that recovers the FOG NFS image export.
set -euo pipefail

[[ ${EUID:-$(id -u)} -eq 0 ]] || {
    echo "Run this installer as root." >&2
    exit 1
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HEALTH_SCRIPT="$SCRIPT_DIR/vstl-nfs-health.sh"

[[ -f "$HEALTH_SCRIPT" ]] || {
    echo "Missing $HEALTH_SCRIPT" >&2
    exit 1
}

install -m 0755 "$HEALTH_SCRIPT" /usr/local/sbin/vstl-nfs-health

cat > /etc/systemd/system/vstl-nfs-health.service <<'EOF'
[Unit]
Description=VSTL FOG NFS image export health check
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/vstl-nfs-health
EOF

cat > /etc/systemd/system/vstl-nfs-health.timer <<'EOF'
[Unit]
Description=Check the VSTL FOG NFS image export every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=15s
Persistent=true
Unit=vstl-nfs-health.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now vstl-nfs-health.timer
systemctl start vstl-nfs-health.service

echo "Installed vstl-nfs-health.timer; /images/dev is monitored every minute."
