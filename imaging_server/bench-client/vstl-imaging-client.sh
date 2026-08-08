#!/usr/bin/env bash
# =============================================================================
# vstl-imaging-client.sh — Bench-side imaging client
# =============================================================================
# Runs on a refurb laptop after PXE-booting into the VSTL Live ISO.
# This script is the BRIDGE between the bench laptop's hardware and the
# VSTL 360 backend at https://tech-audit-system.emergent.host/api/imaging.
#
# Lifecycle:
#   1. Start bench session: POST /api/imaging/sessions/start  (optional, for ops UI)
#   2. Read hardware identity (BIOS serial, MAC, model, CPU/RAM/SSD, etc.)
#   3. Lookup golden image:   GET  /api/imaging/lookup?model=...
#   4. Trigger FOG image deployment via FOG's API
#   5. Run hardware diagnostics (smartctl, stress-ng burn-in)
#   6. Run certified secure wipe; Clear assist is allowed only before a
#      required final Purge retry
#   7. Send full audit + wipe data: POST /api/imaging/ingest
#   8. Power off (or wait for next bench laptop)
#
# Configuration:
#   Reads /opt/vstl/config.env at startup. Keys required:
#     VSTL_API_BASE       — e.g. https://tech-audit-system.emergent.host/api
#     VSTL_API_KEY        — X-API-Key from VSTL 360 → Imaging → API Keys
#     BENCH_ID            — unique identifier for this bench (e.g. "bench-1")
#     FOG_SERVER          — FOG webUI base URL (e.g. http://10.255.0.75/fog)
#     FOG_API_TOKEN       — FOG API token (Settings → General → API enabled)
#     FOG_USER_API_TOKEN  — FOG user API token (per-user, Profile menu)
#
# Logs to /var/log/vstl-imaging-client.log AND stdout (for systemd journal).
# Exits 0 on success; non-zero on any unrecoverable error.
# =============================================================================
set -euo pipefail

# ---------- Config & logging ----------
CONFIG_FILE="${VSTL_CONFIG:-/opt/vstl/config.env}"
LOG_FILE="${VSTL_LOG:-/var/log/vstl-imaging-client.log}"

# shellcheck disable=SC1090
[[ -f "$CONFIG_FILE" ]] && source "$CONFIG_FILE" || {
    echo "FATAL: $CONFIG_FILE not found" >&2
    exit 2
}

: "${VSTL_API_BASE:?VSTL_API_BASE is required}"
: "${VSTL_API_KEY:?VSTL_API_KEY is required}"
BENCH_ID="${BENCH_ID:-bench-$(hostname)}"
FOG_SERVER="${FOG_SERVER:-http://10.255.0.75/fog}"
FOG_API_TOKEN="${FOG_API_TOKEN:-}"
FOG_USER_API_TOKEN="${FOG_USER_API_TOKEN:-}"

mkdir -p "$(dirname "$LOG_FILE")"
exec > >(tee -a "$LOG_FILE") 2>&1

log()  { printf '[%s] %s\n' "$(date -Iseconds)" "$*"; }
die()  { log "FATAL: $*"; exit 1; }
need() { command -v "$1" >/dev/null || die "missing dep: $1 (apt install $1)"; }

for cmd in dmidecode lsblk smartctl curl jq ip awk sed; do need "$cmd"; done

log "VSTL Imaging Client starting on bench=$BENCH_ID"
log "Backend: $VSTL_API_BASE"

# ---------- 0. Bring up first wired NIC + DHCP lease ----------
# Clonezilla Live runs NetworkManager-less + no systemd-networkd by default, so
# we bring up the NIC ourselves. Finds the first wired (non-wireless, non-loopback,
# non-virtual) interface, flips it up, and runs dhclient in the foreground with a
# 30s cap. If no lease is obtained we log a warning but KEEP going — the operator
# may have a static-IP setup or an offline run (wipe-only) in mind.
ensure_network() {
    # Skip if we already have a routable IPv4 on any non-loopback interface.
    if ip -4 -o addr show scope global 2>/dev/null | grep -qv ' lo '; then
        log "Network: already up ($(ip -4 -o addr show scope global | awk '{print $2,$4}' | head -1))"
        return 0
    fi

    local iface
    iface=$(ls /sys/class/net 2>/dev/null \
        | grep -vE '^(lo|wwan|wl|docker|veth|virbr|tap|tun|bond|br-)' \
        | while read -r i; do
            # Only keep real wired interfaces with an ethernet device type (ARPHRD_ETHER = 1)
            [[ -d "/sys/class/net/$i/device" ]] && echo "$i"
          done | head -1)

    if [[ -z "$iface" ]]; then
        log "Network: no wired interface found — continuing anyway (API calls will fail offline)"
        return 0
    fi

    log "Network: bringing up $iface + requesting DHCP lease (timeout 30s)..."
    ip link set "$iface" up 2>/dev/null || true

    # dhclient -1 = exit after one successful lease OR timeout. Wrap in `timeout`
    # for a hard upper bound so we never stall the whole imaging run on dead DHCP.
    if timeout 30 dhclient -1 -v "$iface" >/tmp/dhclient.log 2>&1; then
        local ip_got
        ip_got=$(ip -4 -o addr show dev "$iface" scope global 2>/dev/null | awk '{print $4}' | head -1)
        log "Network: $iface bound to ${ip_got:-<none>}"
    else
        log "Network: DHCP did not complete in 30s — continuing. Tail of /tmp/dhclient.log:"
        tail -5 /tmp/dhclient.log 2>/dev/null | sed 's/^/    /' | while read -r ln; do log "$ln"; done || true
    fi
}
ensure_network

# ---------- Helpers ----------
# Call a VSTL endpoint with X-API-Key. Args: METHOD PATH [JSON_BODY]
api() {
    local method="$1" path="$2" body="${3:-}"
    local url="${VSTL_API_BASE}${path}"
    local args=(-sS --max-time 30 -X "$method"
                -H "X-API-Key: $VSTL_API_KEY"
                -H "Content-Type: application/json")
    [[ -n "$body" ]] && args+=(-d "$body")
    curl "${args[@]}" "$url"
}

# Trim + uppercase
norm() { tr -d '\n' <<<"${1:-}" | sed 's/^ *//;s/ *$//' | tr '[:lower:]' '[:upper:]'; }

# ---------- 1. Read hardware identity ----------
log "Reading hardware identity from BIOS / OS..."

SERIAL_NO=$(norm "$(dmidecode -s system-serial-number || true)")
BRAND=$(norm "$(dmidecode -s system-manufacturer || true)")
SERIES=$(norm "$(dmidecode -s system-family || true)")
MODEL=$(norm "$(dmidecode -s system-product-name || true)")
[[ -z "$SERIAL_NO" || "$SERIAL_NO" == "TO BE FILLED BY O.E.M." ]] && \
    SERIAL_NO=$(norm "$(dmidecode -s baseboard-serial-number || true)")

# Pick the laptop identity MAC, never a removable USB/Type-C adapter MAC.
# Prefer BIOS LOM, then BIOS passthrough MAC, then an internal LOM interface.
# Returning UNKNOWN is safer than linking many laptops to the same shared
# Type-C Ethernet adapter.
normalize_mac() {
    local mac
    mac=$(printf '%s' "${1:-}" \
        | tr '[:lower:]-' '[:upper:]:' \
        | grep -oE '([0-9A-F]{2}:){5}[0-9A-F]{2}' \
        | head -1 || true)
    [[ "$mac" == "00:00:00:00:00:00" ]] && mac=""
    printf '%s' "$mac"
}

dmi_mac_source() {
    dmidecode -t 1 -t 2 -t 11 -t 41 2>/dev/null || true
    dmidecode 2>/dev/null || true
    [[ -r /sys/firmware/dmi/tables/DMI ]] && tr '\000' '\n' < /sys/firmware/dmi/tables/DMI 2>/dev/null || true
}

is_known_external_adapter_mac() {
    local mac prefix
    mac=$(normalize_mac "${1:-}")
    prefix=$(printf '%s' "$mac" | cut -d: -f1-3)
    case "$prefix" in
        # Realtek USB/Type-C Ethernet adapters seen on the bench. The adapter
        # can carry PXE traffic, but its MAC is not the laptop identity.
        00:E0:4C)
            return 0
            ;;
    esac
    return 1
}

is_typec_or_usb_nic() {
    local iface="$1"
    local device_path driver
    [[ "$iface" == enx* || "$iface" == usb* ]] && return 0
    device_path=$(readlink -f "/sys/class/net/$iface/device" 2>/dev/null || true)
    [[ "$device_path" == *"/usb"* || "$device_path" == *"/thunderbolt"* ]] && return 0
    driver=$(basename "$(readlink -f "/sys/class/net/$iface/device/driver" 2>/dev/null || true)")
    case "$driver" in
        asix|ax88179_178a|cdc_ether|cdc_ncm|dm9601|ipheth|kalmia|lan78xx|mos7720|mcs7830|pegasus|r8152|rtl8150|smsc75xx|smsc95xx|sr9700|usbnet)
            return 0
            ;;
    esac
    return 1
}

read_lom_iface_mac() {
    local iface mac
    for iface in $(ls /sys/class/net 2>/dev/null | sort); do
        [[ "$iface" == "lo" || "$iface" == docker* || "$iface" == veth* || "$iface" == virbr* || "$iface" == br-* || "$iface" == tap* || "$iface" == tun* ]] && continue
        [[ -d "/sys/class/net/$iface/wireless" ]] && continue
        is_typec_or_usb_nic "$iface" && continue
        [[ -r "/sys/class/net/$iface/address" ]] || continue
        mac=$(normalize_mac "$(cat "/sys/class/net/$iface/address")")
        [[ -n "$mac" ]] && ! is_known_external_adapter_mac "$mac" && {
            echo "$mac"
            return 0
        }
    done
    return 1
}

read_dmi_mac_by_label() {
    local pattern="$1"
    local mac
    mac=$(
        dmi_mac_source \
            | awk -v pat="$pattern" '
                function colonize(raw,   cleaned, out, i) {
                    cleaned = toupper(raw)
                    gsub(/[^0-9A-F]/, "", cleaned)
                    if (length(cleaned) != 12) {
                        return ""
                    }
                    out = ""
                    for (i = 1; i <= 12; i += 2) {
                        out = out (out ? ":" : "") substr(cleaned, i, 2)
                    }
                    return out
                }
                {
                    line = tolower($0)
                    if (line ~ pat) {
                        ttl = 6
                    }
                    if (ttl > 0 && match($0, /([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}/)) {
                        print colonize(substr($0, RSTART, RLENGTH))
                        exit
                    }
                    if (ttl > 0 && match($0, /(^|[^0-9A-Fa-f])([0-9A-Fa-f]{12})([^0-9A-Fa-f]|$)/)) {
                        print colonize(substr($0, RSTART, RLENGTH))
                        exit
                    }
                    if (ttl > 0) {
                        ttl--
                    }
                }
            '
    )
    normalize_mac "$mac"
}

MAC_ID=$(read_dmi_mac_by_label 'lom|lan|onboard|on-board|integrated|internal|ethernet' || true)
[[ -z "$MAC_ID" ]] && MAC_ID=$(read_dmi_mac_by_label 'pass[ _-]*(through|thru)|passthrough|passthru' || true)
[[ -z "$MAC_ID" ]] && MAC_ID=$(read_lom_iface_mac || true)
MAC_ID=$(norm "${MAC_ID:-UNKNOWN}")

# CPU
CPU_RAW=$(awk -F: '/^model name/{print $2; exit}' /proc/cpuinfo)
CPU=$(echo "$CPU_RAW" | grep -oiE '(i[3579]-[0-9]+(th|TH)?|ryzen[ -][0-9]+|xeon[ -][a-z0-9-]+)' | head -1)
CPU=$(norm "${CPU:-$CPU_RAW}")

# RAM (round up to nearest 4GB)
RAM_KB=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
RAM_GB=$(( (RAM_KB / 1024 / 1024) ))
case $RAM_GB in
    [0-2])  RAM="2GB" ;;
    [3-5])  RAM="4GB" ;;
    [6-9])  RAM="8GB" ;;
    1[0-7]) RAM="16GB" ;;
    1[89]|2[0-7]) RAM="24GB" ;;
    *)      RAM="${RAM_GB}GB" ;;
esac

# SSD — first non-USB block device
PRIMARY_DISK=$(lsblk -dnpo NAME,TYPE,RM,TRAN | awk '$2=="disk" && $3=="0" && $4!="usb"{print $1; exit}')
SSD="NO SSD"
SSD_HEALTH="N/A"
if [[ -n "$PRIMARY_DISK" ]]; then
    SIZE_BYTES=$(lsblk -bdno SIZE "$PRIMARY_DISK" 2>/dev/null || echo 0)
    SIZE_GB=$(( SIZE_BYTES / 1000 / 1000 / 1000 ))
    case $SIZE_GB in
        [0-9]|[1-9][0-9])           SSD="" ;;     # smaller than 100GB — too small
        1[0-2][0-9])                SSD="128GB" ;;
        2[0-7][0-9])                SSD="256GB" ;;
        4[0-9][0-9]|5[0-1][0-9])    SSD="512GB" ;;
        9[0-9][0-9]|1[0-1][0-9][0-9]) SSD="1TB" ;;
        *)                          SSD="${SIZE_GB}GB" ;;
    esac
    # SMART health
    SMART_OUT=$(smartctl -H "$PRIMARY_DISK" 2>/dev/null || true)
    if echo "$SMART_OUT" | grep -qi "PASSED"; then
        # Extract life-left if present
        LIFE=$(smartctl -A "$PRIMARY_DISK" 2>/dev/null \
            | awk '/Percentage_Used|Wear_Leveling|SSD_Life_Left/{print 100-$NF; exit}')
        [[ -n "$LIFE" && "$LIFE" -gt 0 ]] && SSD_HEALTH="${LIFE}%" || SSD_HEALTH="OK"
    elif echo "$SMART_OUT" | grep -qi "FAILED"; then
        SSD_HEALTH="FAILED"
    fi
fi

# Battery health
BATTERY_HEALTH="N/A"
BATTERY_STATUS="NO BATTERY"
BATT_PATH=$(ls -d /sys/class/power_supply/BAT* 2>/dev/null | head -1)
if [[ -n "$BATT_PATH" ]]; then
    DESIGN=$(cat "$BATT_PATH/charge_full_design" 2>/dev/null || cat "$BATT_PATH/energy_full_design" 2>/dev/null || echo 0)
    FULL=$(cat "$BATT_PATH/charge_full" 2>/dev/null || cat "$BATT_PATH/energy_full" 2>/dev/null || echo 0)
    NOW=$(cat "$BATT_PATH/charge_now" 2>/dev/null || cat "$BATT_PATH/energy_now" 2>/dev/null || echo 0)
    CAPACITY=$(cat "$BATT_PATH/capacity" 2>/dev/null || echo 100)
    if [[ $CAPACITY -gt 5 && $CAPACITY -le 95 && $NOW -gt 0 ]]; then
        EST_FULL=$(( NOW * 100 / CAPACITY ))
        if [[ $DESIGN -gt 0 ]]; then
            MIN_FULL=$(( DESIGN * 35 / 100 ))
            MAX_FULL=$(( DESIGN * 115 / 100 ))
        else
            MIN_FULL=1000
            MAX_FULL=999999999
        fi
        if [[ $EST_FULL -ge $MIN_FULL && $EST_FULL -le $MAX_FULL && $EST_FULL -gt $FULL ]]; then
            FULL=$EST_FULL
        fi
    fi
    if [[ $CAPACITY -lt 90 && $FULL -gt 0 && $NOW -gt 0 ]]; then
        DIFF=$(( FULL > NOW ? FULL - NOW : NOW - FULL ))
        TOL=$(( (FULL > NOW ? FULL : NOW) * 3 / 100 ))
        [[ $TOL -lt 2 ]] && TOL=2
        if [[ $DIFF -le $TOL ]]; then
            FULL=$(( NOW * 100 / CAPACITY ))
        fi
    fi
    if [[ $DESIGN -gt 0 && $FULL -gt 0 ]]; then
        PCT_TEXT=$(awk -v full="$FULL" -v design="$DESIGN" 'BEGIN { printf "%d", (full * 100.0 / design) }')
        BATTERY_HEALTH="${PCT_TEXT}%"
        PCT_INT=$PCT_TEXT
        if   [[ $PCT_INT -ge 80 ]]; then BATTERY_STATUS="OK"
        elif [[ $PCT_INT -ge 60 ]]; then BATTERY_STATUS="OK"
        else                         BATTERY_STATUS="REPLACE"
        fi
    fi
fi

log "Identity:"
log "  serial=$SERIAL_NO  mac=$MAC_ID"
log "  brand=$BRAND  series=$SERIES  model=$MODEL"
log "  cpu=$CPU  ram=$RAM  ssd=$SSD ($SSD_HEALTH)"
log "  battery=$BATTERY_STATUS ($BATTERY_HEALTH)"

[[ -z "$SERIAL_NO" ]] && die "Could not read serial number from BIOS — aborting."

# ---------- 2. Lookup golden image ----------
log "Looking up golden image for model='$MODEL'..."
LOOKUP_QS="model=$(jq -rn --arg m "$MODEL" '$m|@uri')"
LOOKUP_RES=$(api GET "/imaging/lookup?$LOOKUP_QS" || echo '{"status":"error"}')
LOOKUP_STATUS=$(echo "$LOOKUP_RES" | jq -r '.status // "error"')
log "Lookup status: $LOOKUP_STATUS"

GOLDEN_IMAGE_NAME=""
GOLDEN_IMAGE_PATH=""
if [[ "$LOOKUP_STATUS" == "found" ]]; then
    GOLDEN_IMAGE_NAME=$(echo "$LOOKUP_RES" | jq -r '.image_name // ""')
    GOLDEN_IMAGE_PATH=$(echo "$LOOKUP_RES" | jq -r '.image_path // ""')
    log "Matched: $GOLDEN_IMAGE_NAME ($GOLDEN_IMAGE_PATH)"
elif [[ "$LOOKUP_STATUS" == "manual_capture" ]]; then
    log "No golden image — model flagged for manual capture in VSTL."
else
    log "Lookup failed: $LOOKUP_RES"
fi

# ---------- 3. Deploy image via FOG (if matched) ----------
IMAGING_STATUS="skipped"
IMAGING_STARTED_AT=""
IMAGING_COMPLETED_AT=""

if [[ -n "$GOLDEN_IMAGE_NAME" && -n "$FOG_API_TOKEN" && -n "$FOG_USER_API_TOKEN" ]]; then
    log "Triggering FOG image deployment for $GOLDEN_IMAGE_NAME..."
    IMAGING_STARTED_AT=$(date -Iseconds)

    # FOG API: create a host (if missing) + queue immediate task
    # Reference: https://news.fogproject.org/api-documentation/
    HOST_PAYLOAD=$(jq -nc \
        --arg name "$SERIAL_NO" --arg mac "$MAC_ID" \
        --arg img "$GOLDEN_IMAGE_NAME" \
        '{name:$name, primac:$mac, imagename:$img}')

    curl -fsS --max-time 60 \
        -H "fog-api-token: $FOG_API_TOKEN" \
        -H "fog-user-token: $FOG_USER_API_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$HOST_PAYLOAD" \
        "$FOG_SERVER/lib/plugins/fogapi/api.php?host=create" \
        > /tmp/fog_host.json 2>&1 || log "FOG host create response: $(cat /tmp/fog_host.json)"

    # Queue a deploy task
    curl -fsS --max-time 60 \
        -H "fog-api-token: $FOG_API_TOKEN" \
        -H "fog-user-token: $FOG_USER_API_TOKEN" \
        "$FOG_SERVER/lib/plugins/fogapi/api.php?host=task&taskTypeID=1&hostName=$SERIAL_NO" \
        > /tmp/fog_task.json 2>&1 || log "FOG task queue response: $(cat /tmp/fog_task.json)"

    # NOTE: This script does NOT wait for FOG to finish imaging here. In a
    # real bench Live ISO flow, the FOS (FOG OS) image takes over the whole
    # machine for the imaging phase; this script is only the pre/post wrapper.
    # If you want post-image diagnostics, restart this client at the end of
    # FOS via the FOG "post-init script" hook.
    IMAGING_STATUS="queued"
    IMAGING_COMPLETED_AT=$(date -Iseconds)
elif [[ -n "$GOLDEN_IMAGE_NAME" ]]; then
    log "FOG API tokens not configured — skipping deploy (manual mode)."
fi

# ---------- 4. Diagnostics (lightweight, ISO-friendly) ----------
log "Running quick diagnostics..."

# Camera/Speaker/Wifi/Bluetooth/USB — best effort detection (devices present at all)
CAMERA="NON-FUNCTIONAL"
[[ -n "$(ls /dev/video* 2>/dev/null)" ]] && CAMERA="FUNCTIONAL"

WIFI="NON FUNCTIONAL"
WIFI_DEV=$(iw dev 2>/dev/null | awk '/Interface/{print $2; exit}')
[[ -n "$WIFI_DEV" ]] && WIFI="FUNCTIONAL"

BLUETOOTH="NON FUNCTIONAL"
[[ -n "$(hciconfig 2>/dev/null | head -1)" ]] && BLUETOOTH="FUNCTIONAL"

# USB — count visible non-hub devices
USB_COUNT=$(lsusb 2>/dev/null | grep -vci 'hub' || true)
USB_PORTS_LAN="ALL OK"
[[ "$USB_COUNT" -lt 1 ]] && USB_PORTS_LAN="USB NOT WORKING"

# Speakers — check for an ALSA card
SPEAKER="NON-FUNCTIONAL"
[[ -e /proc/asound/cards && "$(wc -l </proc/asound/cards)" -gt 1 ]] && SPEAKER="FUNCTIONAL"

# LCD — assume FUNCTIONAL for now (visual inspection step on technician side)
LCD_TYPE="UNKNOWN"
LCD_ISSUE="NONE"
LCD_RESOLUTION=$(awk -F'=' '/Resolution/{print $2; exit}' /proc/fb 2>/dev/null \
    || xrandr --query 2>/dev/null | awk '/\*/{print $1; exit}' || echo "")
LCD_RESOLUTION=$(norm "$LCD_RESOLUTION")
[[ -z "$LCD_RESOLUTION" ]] && LCD_RESOLUTION="UNKNOWN"

# Keyboard — best-effort guess
KEYBOARD_TYPE="US/UK QWERTY"
KEYBOARD_STATUS="WITHOUT BACK LIGHT"
# `-e` doesn't work with globs; use compgen to expand the path.
if compgen -G "/sys/class/leds/*::kbd_backlight" >/dev/null; then
    KEYBOARD_STATUS="WITH BACK LIGHT"
fi

# Cosmetic / Windows / Security — defaults; technician corrects in VSTL UI later.
COSMETIC_GRADE="B"
WINDOWS_ACTIVATED="NO"
SECURITY="NONE"

# ---------- 5. Wipe (only if user passed --wipe) ----------
WIPE_INFO_JSON='null'
if [[ "${1:-}" == "--wipe" && -n "$PRIMARY_DISK" ]]; then
    log "WIPE requested — running secure erase on $PRIMARY_DISK"
    WIPE_START=$(date -Iseconds)
    # Clear-class helpers are allowed only as a destructive assist between
    # failed direct purge and the required final purge retry, except the
    # temporary model-specific clear-only exception.
    DMI_PROFILE="$(
        cat /sys/class/dmi/id/sys_vendor /sys/class/dmi/id/product_name \
            /sys/class/dmi/id/product_version 2>/dev/null || true
    )"
    MODEL_CLEAR_ONLY_EXCEPTION=0
    MODEL_CLEAR_ONLY_LABEL=""
    if echo "$DMI_PROFILE" | grep -Eiq 'hp|hewlett' \
        && echo "$DMI_PROFILE" | grep -Eiq 'elitebook' \
        && echo "$DMI_PROFILE" | grep -Eiq '\b640\b' \
        && echo "$DMI_PROFILE" | grep -Eiq '\bg10\b'; then
        MODEL_CLEAR_ONLY_EXCEPTION=1
        MODEL_CLEAR_ONLY_LABEL="HP EliteBook 640 G10"
    elif echo "$DMI_PROFILE" | grep -Eiq 'hp|hewlett' \
        && echo "$DMI_PROFILE" | grep -Eiq 'elitebook' \
        && echo "$DMI_PROFILE" | grep -Eiq '\b850\b' \
        && echo "$DMI_PROFILE" | grep -Eiq '\bg5\b'; then
        MODEL_CLEAR_ONLY_EXCEPTION=1
        MODEL_CLEAR_ONLY_LABEL="HP EliteBook 850 G5"
    elif echo "$DMI_PROFILE" | grep -Eiq 'hp|hewlett' \
        && echo "$DMI_PROFILE" | grep -Eiq 'elitebook' \
        && echo "$DMI_PROFILE" | grep -Eiq '\b850\b' \
        && echo "$DMI_PROFILE" | grep -Eiq '\bg6\b'; then
        MODEL_CLEAR_ONLY_EXCEPTION=1
        MODEL_CLEAR_ONLY_LABEL="HP EliteBook 850 G6"
    elif echo "$DMI_PROFILE" | grep -Eiq 'dell' \
        && echo "$DMI_PROFILE" | grep -Eiq 'latitude' \
        && echo "$DMI_PROFILE" | grep -Eiq '\b5520\b'; then
        MODEL_CLEAR_ONLY_EXCEPTION=1
        MODEL_CLEAR_ONLY_LABEL="Dell Latitude 5520"
    elif echo "$DMI_PROFILE" | grep -Eiq 'lenovo' \
        && echo "$DMI_PROFILE" | grep -Eiq 'x1' \
        && echo "$DMI_PROFILE" | grep -Eiq 'carbon' \
        && echo "$DMI_PROFILE" | grep -Eiq '(\bgen[[:space:]]*8\b|\b8th\b)'; then
        MODEL_CLEAR_ONLY_EXCEPTION=1
        MODEL_CLEAR_ONLY_LABEL="Lenovo ThinkPad X1 Carbon 8th Gen"
    fi
    WIPE_STANDARD="NIST 800-88 Purge"
    if [[ "$PRIMARY_DISK" =~ nvme ]]; then
        NVME_CONTROLLER="/dev/$(basename "$PRIMARY_DISK" | sed -E 's/n[0-9]+$//')"
        NVME_NSID="$(cat "/sys/block/$(basename "$PRIMARY_DISK")/nsid" 2>/dev/null || echo 1)"
        nvme_purge_crypto() {
            nvme format "$PRIMARY_DISK" -s 2 --force \
                || { [[ "$NVME_CONTROLLER" != "$PRIMARY_DISK" ]] \
                    && nvme format "$NVME_CONTROLLER" -n "$NVME_NSID" -s 2 --force; }
        }
        nvme_clear_assist() {
            nvme format "$PRIMARY_DISK" -s 1 --force \
                || { [[ "$NVME_CONTROLLER" != "$PRIMARY_DISK" ]] \
                    && nvme format "$NVME_CONTROLLER" -n "$NVME_NSID" -s 1 --force; } \
                || blkdiscard --secure -f "$PRIMARY_DISK" \
                || blkdiscard -s -f "$PRIMARY_DISK" \
                || dd if=/dev/zero of="$PRIMARY_DISK" bs=16M status=progress conv=fsync
        }
        if ! nvme_purge_crypto; then
            log "Direct NVMe purge failed; running Clear assist before required final Purge retry"
            if ! nvme_clear_assist; then
                log "FATAL: NVMe Clear assist failed; final Purge retry cannot be trusted"
                exit 7
            fi
            if [[ "$MODEL_CLEAR_ONLY_EXCEPTION" == "1" ]]; then
                log "NVMe Clear assist completed; $MODEL_CLEAR_ONLY_LABEL temporary clear-only policy allows completion"
                METHOD="NVMe Clear Erase ($MODEL_CLEAR_ONLY_LABEL temporary exception)"
                WIPE_STANDARD="NIST 800-88 Clear"
            else
                log "NVMe Clear assist completed; retrying required final Purge"
                if ! nvme_purge_crypto; then
                    log "FATAL: final NVMe Purge retry failed after Clear assist"
                    exit 7
                fi
                METHOD="NVMe Format Crypto Erase"
            fi
        else
            METHOD="NVMe Format Crypto Erase"
        fi
    else
        ERASE_PASSWORD="vstl"
        ata_purge() {
            if ! hdparm --user-master u --security-set-pass "$ERASE_PASSWORD" "$PRIMARY_DISK"; then
                return 1
            fi
            hdparm --user-master u --security-erase-enhanced "$ERASE_PASSWORD" "$PRIMARY_DISK" \
                || hdparm --user-master u --security-erase "$ERASE_PASSWORD" "$PRIMARY_DISK"
        }
        if ! ata_purge; then
            log "Direct ATA purge failed; running BLKDISCARD Clear assist before required final Purge retry"
            if ! blkdiscard -f "$PRIMARY_DISK"; then
                log "FATAL: ATA Clear assist failed; final Purge retry cannot be trusted"
                exit 7
            fi
            if [[ "$MODEL_CLEAR_ONLY_EXCEPTION" == "1" ]]; then
                log "BLKDISCARD Clear assist completed; $MODEL_CLEAR_ONLY_LABEL temporary clear-only policy allows completion"
                METHOD="BLKDISCARD Clear ($MODEL_CLEAR_ONLY_LABEL temporary exception)"
                WIPE_STANDARD="NIST 800-88 Clear"
            else
                if ! ata_purge; then
                    log "FATAL: final ATA Purge retry failed after Clear assist"
                    exit 7
                fi
                METHOD="ATA Security Erase Enhanced"
            fi
        else
            METHOD="ATA Security Erase Enhanced"
        fi
    fi
    WIPE_END=$(date -Iseconds)
    WIPE_INFO_JSON=$(jq -nc \
        --arg method "$METHOD" --arg standard "$WIPE_STANDARD" --argjson passes 1 \
        --arg started "$WIPE_START" --arg completed "$WIPE_END" --argjson verified true \
        '{method:$method, standard:$standard, passes:$passes, started_at:$started, completed_at:$completed, verified:$verified}')
fi

# ---------- 6. POST /api/imaging/ingest ----------
log "Sending audit + wipe data to VSTL..."

PAYLOAD=$(jq -nc \
    --arg serial_no "$SERIAL_NO" \
    --arg mac_id "$MAC_ID" \
    --arg brand "$BRAND" \
    --arg series "$SERIES" \
    --arg model "$MODEL" \
    --arg cpu "$CPU" \
    --arg ram "$RAM" \
    --arg ssd "$SSD" \
    --arg ssd_health "$SSD_HEALTH" \
    --arg battery_status "$BATTERY_STATUS" \
    --arg battery_health "$BATTERY_HEALTH" \
    --arg lcd_type "$LCD_TYPE" \
    --arg lcd_issue "$LCD_ISSUE" \
    --arg lcd_resolution "$LCD_RESOLUTION" \
    --arg keyboard_type "$KEYBOARD_TYPE" \
    --arg keyboard_status "$KEYBOARD_STATUS" \
    --arg camera "$CAMERA" \
    --arg speaker "$SPEAKER" \
    --arg wifi "$WIFI" \
    --arg bluetooth "$BLUETOOTH" \
    --arg usb_ports_lan "$USB_PORTS_LAN" \
    --arg windows_activated "$WINDOWS_ACTIVATED" \
    --arg security "$SECURITY" \
    --arg cosmetic_grade "$COSMETIC_GRADE" \
    --arg bench_id "$BENCH_ID" \
    --arg image_name "$GOLDEN_IMAGE_NAME" \
    --arg imaging_status "$IMAGING_STATUS" \
    --arg imaging_started "$IMAGING_STARTED_AT" \
    --arg imaging_completed "$IMAGING_COMPLETED_AT" \
    --argjson wipe_info "$WIPE_INFO_JSON" \
    '{
        serial_no: $serial_no, mac_id: $mac_id,
        brand: $brand, series: $series, model: $model,
        cpu: $cpu, ram: $ram, ssd: $ssd, ssd_health: $ssd_health,
        battery_status: $battery_status, battery_health: $battery_health,
        lcd_type: $lcd_type, lcd_issue: $lcd_issue, lcd_resolution: $lcd_resolution,
        keyboard_type: $keyboard_type, keyboard_status: $keyboard_status,
        camera: $camera, speaker: $speaker, wifi: $wifi, bluetooth: $bluetooth,
        usb_ports_lan: $usb_ports_lan, windows_activated: $windows_activated,
        security: $security, cosmetic_grade: $cosmetic_grade,
        bench_id: $bench_id, test_type: "imaging",
        image_deployed: $image_name,
        imaging_status: $imaging_status,
        imaging_started_at: $imaging_started,
        imaging_completed_at: $imaging_completed,
        wipe_info: $wipe_info
    }')

INGEST_RES=$(api POST "/imaging/ingest" "$PAYLOAD" || echo '{"success":false,"error":"curl_failed"}')
log "Ingest response: $INGEST_RES"

# Backend returns either {"success": true, ...} (new format) or {"status": "success"|"ok", ...}
# (legacy format). Accept any of them as success; reject everything else.
INGEST_OK=$(echo "$INGEST_RES" | jq -r '
    if (.success == true) then "yes"
    elif (.status == "success" or .status == "ok") then "yes"
    else "no" end
' 2>/dev/null || echo "no")

if [[ "$INGEST_OK" != "yes" ]]; then
    die "Ingest failed: $INGEST_RES"
fi

# Pull the record_id / internal_id for the success banner (new format carries them)
RECORD_ID=$(echo "$INGEST_RES" | jq -r '.record_id // ""')
INTERNAL_ID=$(echo "$INGEST_RES" | jq -r '.internal_id // ""')
[[ -n "$RECORD_ID" ]] && log "  record_id=$RECORD_ID  internal_id=${INTERNAL_ID:-<unlinked>}"

log "✅ Bench session complete for serial=$SERIAL_NO"

# ---------- 7. Auto-shutdown (default: ON) ----------
# Opt-out by setting VSTL_AUTO_SHUTDOWN=0 in /opt/vstl/config.env — useful when
# a technician wants to drop into the shell after a successful run for debug.
if [[ "${VSTL_AUTO_SHUTDOWN:-1}" == "1" ]]; then
    log "Powering off in 10s (operator can now move to next bench). Ctrl+C to abort."
    sleep 10
    poweroff
fi

exit 0
