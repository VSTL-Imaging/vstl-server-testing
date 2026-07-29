#!/usr/bin/env bash
set -euo pipefail

# Reset noisy operational logs after a VSTL update/deploy. Do not truncate
# /var/lib/vstl-reports/audits.jsonl; that file is the report database.
logs=(
  /var/log/apache2/access.log
  /var/log/apache2/other_vhosts_access.log
  /var/log/apache2/error.log
  /var/log/syslog
  /var/log/dnsmasq.log
  /var/log/vstl-imaging-bench.log
  /var/log/vstl-pxe-boot.log
  /var/log/vstl-pxe-boot.previous.log
)

truncate_logs() {
  local path
  for path in "${logs[@]}"; do
    if [[ -e "$path" ]]; then
      : >"$path" || true
    fi
  done
}

truncate_logs
systemctl reload apache2 >/dev/null 2>&1 || true
systemctl restart dnsmasq >/dev/null 2>&1 || true
# Service reload/restart can write a few fresh operational lines; clear those
# too so the next bench laptop is the first meaningful event after a deploy.
truncate_logs

printf 'VSTL operational logs reset at %s\n' "$(date -Is)" \
  >/var/log/vstl-operational-log-reset.marker
