#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_DIR="/var/www/html/vstl-reports"
LIB_DIR="/usr/local/lib/vstl-reporting"
DATA_DIR="/var/lib/vstl-reports"
TOKEN_FILE="/etc/vstl-report-token"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Run as root (sudo)." >&2
    exit 1
fi

mkdir -p "$WEB_DIR" "$LIB_DIR" "$DATA_DIR"
install -m 0644 "$SCRIPT_DIR/reporting/index.php" "$WEB_DIR/index.php"
install -m 0644 "$SCRIPT_DIR/reporting/ingest.php" "$WEB_DIR/ingest.php"
install -m 0644 "$SCRIPT_DIR/reporting/bench-state.php" "$WEB_DIR/bench-state.php"
install -m 0644 "$SCRIPT_DIR/reporting/download.php" "$WEB_DIR/download.php"
install -m 0755 "$SCRIPT_DIR/reporting/export_reports.py" "$LIB_DIR/export_reports.py"
install -m 0755 "$SCRIPT_DIR/reporting/import_secure_erase_records.py" "$LIB_DIR/import_secure_erase_records.py"
install -m 0755 "$SCRIPT_DIR/reporting/import_capture_records.py" "$LIB_DIR/import_capture_records.py"
mkdir -p "$DATA_DIR/bench-state"
touch "$DATA_DIR/audits.jsonl"
chown -R www-data:www-data "$DATA_DIR"
chmod 0750 "$DATA_DIR"
chmod 0640 "$DATA_DIR/audits.jsonl"
if [[ -d /images/dev/.vstl-secure-erase ]]; then
    chmod 0755 /images/dev/.vstl-secure-erase || true
    chmod 0644 /images/dev/.vstl-secure-erase/*.json 2>/dev/null || true
fi
if [[ -d /images/dev/.vstl-secure-erase ]]; then
    python3 "$LIB_DIR/import_secure_erase_records.py" \
        --records-root /images/dev/.vstl-secure-erase \
        --data "$DATA_DIR/audits.jsonl" || true
fi
if [[ -d /images/dev ]]; then
    python3 "$LIB_DIR/import_capture_records.py" \
        /images/dev "$DATA_DIR/audits.jsonl" || true
fi
chown www-data:www-data "$DATA_DIR/audits.jsonl"
chmod 0640 "$DATA_DIR/audits.jsonl"

if [[ ! -s "$DATA_DIR/audits.jsonl" && -s /var/log/vstl-imaging-phase1.json ]]; then
    python3 - /var/log/vstl-imaging-phase1.json "$DATA_DIR/audits.jsonl" <<'PY'
import json
import sys
from datetime import datetime, timezone

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
record = {"received_at": datetime.now(timezone.utc).isoformat(), "payload": payload}
with open(sys.argv[2], "a", encoding="utf-8") as target:
    target.write(json.dumps(record, separators=(",", ":")) + "\n")
PY
    chown www-data:www-data "$DATA_DIR/audits.jsonl"
    chmod 0640 "$DATA_DIR/audits.jsonl"
fi

token=""
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    token="$(grep -E '^VSTL_REPORT_TOKEN=' "$SCRIPT_DIR/.env" | head -1 | cut -d= -f2- | tr -d "\"'" || true)"
fi
if [[ -z "$token" || "$token" == *REPLACE_ME* ]]; then
    if [[ -s "$TOKEN_FILE" ]]; then
        token="$(cat "$TOKEN_FILE")"
    else
        token="$(openssl rand -hex 24)"
    fi
fi
printf '%s\n' "$token" > "$TOKEN_FILE"
chown root:www-data "$TOKEN_FILE"
chmod 0640 "$TOKEN_FILE"

if [[ -f "$SCRIPT_DIR/.env" ]]; then
    if grep -q '^VSTL_REPORT_TOKEN=' "$SCRIPT_DIR/.env"; then
        sed -i "s|^VSTL_REPORT_TOKEN=.*|VSTL_REPORT_TOKEN=\"$token\"|" "$SCRIPT_DIR/.env"
    else
        printf '\nVSTL_REPORT_TOKEN="%s"\n' "$token" >> "$SCRIPT_DIR/.env"
    fi
    if ! grep -q '^VSTL_REPORT_BASE=' "$SCRIPT_DIR/.env"; then
        server_ip="$(grep -E '^(VSTL_SERVER_IP|SERVER_IP)=' "$SCRIPT_DIR/.env" | head -1 | cut -d= -f2- | tr -d "\"'" || true)"
        printf 'VSTL_REPORT_BASE="http://%s/vstl-reports"\n' "${server_ip:-10.255.0.75}" >> "$SCRIPT_DIR/.env"
    fi
    chmod 0600 "$SCRIPT_DIR/.env"
fi

echo "VSTL reporting installed at /vstl-reports/"
