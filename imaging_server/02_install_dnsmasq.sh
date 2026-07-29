#!/usr/bin/env bash
# =============================================================================
# 02_install_dnsmasq.sh - Installs and configures dnsmasq for PXE-only DHCP
# =============================================================================
# Run as: sudo /opt/vstl-imaging/02_install_dnsmasq.sh
#
# What this does:
#  1. Installs dnsmasq if not present
#  2. Disables systemd-resolved's dnsmasq stub if it conflicts on port 53
#  3. Drops 03_dnsmasq_proxydhcp.conf into /etc/dnsmasq.d/
#  4. Substitutes server IP / subnet from .env (if set) into the config
#  5. Enables and restarts the dnsmasq service
#  6. Validates that PXE traffic is being received (passive listener test)
# =============================================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn() { echo -e "${YELLOW}[$(date +%H:%M:%S)] WARN:${NC} $*"; }
die()  { echo -e "${RED}[$(date +%H:%M:%S)] FATAL:${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Must be run as root (use sudo)."

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
[[ -f "$SCRIPT_DIR/.env" ]] && source "$SCRIPT_DIR/.env"
SERVER_IP="${SERVER_IP:-10.255.0.75}"
BENCH_SUBNET="${BENCH_SUBNET:-10.255.0.0/24}"
BENCH_GATEWAY="${BENCH_GATEWAY:-10.255.0.1}"
VSTL_PXE_DHCP_START="${VSTL_PXE_DHCP_START:-10.255.0.80}"
VSTL_PXE_DHCP_END="${VSTL_PXE_DHCP_END:-10.255.0.89}"
TFTP_ROOT="${TFTP_ROOT:-/tftpboot}"

# Older UEFI PXE firmware is picky about which NBP it will chainload. Prefer
# VSTL's embedded SNP-only binary, published under the firmware-friendly
# stock filename snponly.efi, so it reuses the NIC driver initialized by
# firmware but jumps straight to our HTTP boot.ipxe. A non-VSTL stock
# snponly.efi can carry a default.ipxe chain that never reaches VSTL on
# affected benches.
# Operators can still pin another loader in .env with, for example,
# VSTL_UEFI_BOOTFILE=ipxe.efi, intel.efi, vstl-clean-ipxe.efi, or snponly.efi.
if [[ -n "${VSTL_UEFI_BOOTFILE:-}" ]]; then
    UEFI_BOOTFILE="$VSTL_UEFI_BOOTFILE"
else
    UEFI_BOOTFILE="snponly.efi"
fi
# Latitude 5490-class systems stay MAC-tagged for VSTL's second-stage HTTP
# script, but should use a dedicated embedded SNP-only first stage. That keeps
# the firmware NIC driver open and jumps directly to VSTL's old-Dell HTTP
# script instead of the generic upstream `default.ipxe` chain.
if [[ -n "${VSTL_OLD_DELL_UEFI_BOOTFILE:-}" ]]; then
    OLD_DELL_UEFI_BOOTFILE="$VSTL_OLD_DELL_UEFI_BOOTFILE"
else
    OLD_DELL_UEFI_BOOTFILE="vstl-old-dell-snponly.efi"
fi

CONFIG_SRC="$SCRIPT_DIR/03_dnsmasq_proxydhcp.conf"
CONFIG_DST="/etc/dnsmasq.d/vstl-imaging.conf"
CONFIG_DIR="$(dirname "$CONFIG_DST")"

[[ -f "$CONFIG_SRC" ]] || die "Cannot find $CONFIG_SRC — bundle layout is wrong."

# --- 1. Install dnsmasq ---
if ! command -v dnsmasq >/dev/null; then
    log "Installing dnsmasq..."
    apt-get update -qq
    apt-get install -y -qq dnsmasq
fi

# --- 2. Disable systemd-resolved stub on port 53 (else dnsmasq won't bind) ---
# We disable DNS in our config (port=0) so this is belt-and-suspenders.
if systemctl is-active --quiet systemd-resolved; then
    if ss -lntu | grep -q ':53 '; then
        log "Reconfiguring systemd-resolved to free up port 53..."
        mkdir -p /etc/systemd/resolved.conf.d
        cat > /etc/systemd/resolved.conf.d/vstl-imaging.conf <<EOF
[Resolve]
DNSStubListener=no
EOF
        systemctl restart systemd-resolved
    fi
fi

# --- 3. Place the PXE DHCP config ---
log "Installing config to $CONFIG_DST ..."

# Strip the legacy server-IP from the bundled file and substitute the
# value from .env. This way one bundle works for any IP.
SERVER_IP_ESC=$(printf '%s' "$SERVER_IP" | sed 's/[\/&]/\\&/g')
UEFI_BOOTFILE_ESC=$(printf '%s' "$UEFI_BOOTFILE" | sed 's/[\/&]/\\&/g')
OLD_DELL_UEFI_BOOTFILE_ESC=$(printf '%s' "$OLD_DELL_UEFI_BOOTFILE" | sed 's/[\/&]/\\&/g')
BENCH_GATEWAY_ESC=$(printf '%s' "$BENCH_GATEWAY" | sed 's/[\/&]/\\&/g')
PXE_DHCP_START_ESC=$(printf '%s' "$VSTL_PXE_DHCP_START" | sed 's/[\/&]/\\&/g')
PXE_DHCP_END_ESC=$(printf '%s' "$VSTL_PXE_DHCP_END" | sed 's/[\/&]/\\&/g')
SUBNET_BASE=$(echo "$BENCH_SUBNET" | cut -d/ -f1)
SUBNET_BASE_ESC=$(printf '%s' "$SUBNET_BASE" | sed 's/[\/&]/\\&/g')
SUBNET_BASE_DEFAULT="10.255.0.0"
SUBNET_BASE_DEFAULT_ESC=$(printf '%s' "$SUBNET_BASE_DEFAULT" | sed 's/[\/&]/\\&/g')
BENCH_GATEWAY_DEFAULT="10.255.0.1"
PXE_DHCP_START_DEFAULT="10.255.0.80"
PXE_DHCP_END_DEFAULT="10.255.0.89"
BENCH_GATEWAY_DEFAULT_ESC=$(printf '%s' "$BENCH_GATEWAY_DEFAULT" | sed 's/[\/&]/\\&/g')
PXE_DHCP_START_DEFAULT_ESC=$(printf '%s' "$PXE_DHCP_START_DEFAULT" | sed 's/[\/&]/\\&/g')
PXE_DHCP_END_DEFAULT_ESC=$(printf '%s' "$PXE_DHCP_END_DEFAULT" | sed 's/[\/&]/\\&/g')

# Ensure the conf.d directory exists. Some minimal dnsmasq packages don't
# create it during apt-install if /etc/dnsmasq.conf is missing the
# `conf-dir=/etc/dnsmasq.d/,*.conf` line.
mkdir -p "$CONFIG_DIR"

# dnsmasq reads most files in /etc/dnsmasq.d, not only *.conf. A dated backup
# such as vstl-imaging.conf.bak-20260524_140742 can silently become a second
# live DHCP/PXE config and override the intended bootfile. Keep backups outside
# the active config directory.
DNSMASQ_ARCHIVE_DIR="/opt/vstl-backups/dnsmasq-conf-disabled-$(date +%Y%m%d_%H%M%S)"
shopt -s nullglob
STALE_DNSMASQ_FILES=(
    "$CONFIG_DIR"/vstl-imaging.conf.*
    "$CONFIG_DIR"/*.bak
    "$CONFIG_DIR"/*.bak-*
)
for stale in "${STALE_DNSMASQ_FILES[@]}"; do
    [[ -e "$stale" ]] || continue
    mkdir -p "$DNSMASQ_ARCHIVE_DIR"
    warn "Moving inactive dnsmasq backup out of live config dir: $stale"
    mv "$stale" "$DNSMASQ_ARCHIVE_DIR/$(basename "$stale")"
done
shopt -u nullglob

# Make sure /etc/dnsmasq.conf includes our conf.d directory. On stripped-down
# Ubuntu installs this isn't enabled by default, so files in /etc/dnsmasq.d/
# are silently ignored.
if [[ -f /etc/dnsmasq.conf ]] && ! grep -q "^conf-dir=/etc/dnsmasq.d" /etc/dnsmasq.conf; then
    log "Enabling /etc/dnsmasq.d/ include in /etc/dnsmasq.conf..."
    echo 'conf-dir=/etc/dnsmasq.d/,*.conf' >> /etc/dnsmasq.conf
fi

# sed in-place: replace the hardcoded IPs with values from .env
sed \
    -e "s/10\\.255\\.254\\.75/$SERVER_IP_ESC/g" \
    -e "s/10\\.255\\.0\\.75/$SERVER_IP_ESC/g" \
    -e "/^pxe-service=tag:efi64,/s/ipxe\\.efi/$UEFI_BOOTFILE_ESC/" \
    -e "/^pxe-service=tag:efix64,/s/ipxe\\.efi/$UEFI_BOOTFILE_ESC/" \
    -e "s|^dhcp-boot=tag:efi64,tag:!old_dell_uefi,.*$|dhcp-boot=tag:efi64,tag:!old_dell_uefi,$UEFI_BOOTFILE_ESC,vstl-imaging,$SERVER_IP_ESC|" \
    -e "s|^dhcp-boot=tag:efix64,tag:!old_dell_uefi,.*$|dhcp-boot=tag:efix64,tag:!old_dell_uefi,$UEFI_BOOTFILE_ESC,vstl-imaging,$SERVER_IP_ESC|" \
    -e "s|^dhcp-option=tag:!ipxe,tag:efi64,tag:!old_dell_uefi,option:bootfile-name,.*$|dhcp-option=tag:!ipxe,tag:efi64,tag:!old_dell_uefi,option:bootfile-name,$UEFI_BOOTFILE_ESC|" \
    -e "s|^dhcp-option=tag:!ipxe,tag:efix64,tag:!old_dell_uefi,option:bootfile-name,.*$|dhcp-option=tag:!ipxe,tag:efix64,tag:!old_dell_uefi,option:bootfile-name,$UEFI_BOOTFILE_ESC|" \
    -e "s|^dhcp-boot=tag:old_dell_uefi,.*$|dhcp-boot=tag:old_dell_uefi,$OLD_DELL_UEFI_BOOTFILE_ESC,vstl-imaging,$SERVER_IP_ESC|" \
    -e "s|^dhcp-option=tag:!ipxe,tag:old_dell_uefi,option:bootfile-name,.*$|dhcp-option=tag:!ipxe,tag:old_dell_uefi,option:bootfile-name,$OLD_DELL_UEFI_BOOTFILE_ESC|" \
    -e "s/$PXE_DHCP_START_DEFAULT_ESC/$PXE_DHCP_START_ESC/g" \
    -e "s/$PXE_DHCP_END_DEFAULT_ESC/$PXE_DHCP_END_ESC/g" \
    -e "s/$BENCH_GATEWAY_DEFAULT_ESC/$BENCH_GATEWAY_ESC/g" \
    -e "s/$SUBNET_BASE_DEFAULT_ESC/$SUBNET_BASE_ESC/g" \
    "$CONFIG_SRC" > "$CONFIG_DST"

chmod 644 "$CONFIG_DST"

for bootfile in pxelinux.0 "$UEFI_BOOTFILE" "$OLD_DELL_UEFI_BOOTFILE"; do
    if [[ ! -f "$TFTP_ROOT/$bootfile" ]]; then
        warn "TFTP boot file missing: $TFTP_ROOT/$bootfile"
        warn "PXE clients matching this loader will fail until the file exists."
    fi
done

for bootfile in "$UEFI_BOOTFILE" "$OLD_DELL_UEFI_BOOTFILE"; do
    if [[ -f "$TFTP_ROOT/$bootfile" ]] \
        && strings "$TFTP_ROOT/$bootfile" 2>/dev/null | grep -q '10\.255\.254\.75'; then
        warn "UEFI boot file $TFTP_ROOT/$bootfile contains stale 10.255.254.75 embedded script text."
        warn "Use a current loader such as ipxe.efi or replace the stale EFI loader."
    fi
done

if [[ "$UEFI_BOOTFILE" == "snponly.efi" && -f "$TFTP_ROOT/snponly.efi" ]]; then
    if ! strings "$TFTP_ROOT/snponly.efi" 2>/dev/null | grep -q "http://${SERVER_IP}/vstl-pxe/boot.ipxe"; then
        warn "TFTP $TFTP_ROOT/snponly.efi is not VSTL-embedded yet."
        warn "Run 06_setup_pxe_netboot.sh so generic UEFI PXE chains directly to VSTL boot.ipxe."
    fi
fi

# --- 4. Sanity check via dnsmasq's --test mode ---
log "Validating dnsmasq config..."
if ! dnsmasq --test --conf-dir=/etc/dnsmasq.d &>/tmp/dnsmasq_test.log; then
    cat /tmp/dnsmasq_test.log
    die "dnsmasq config has errors. See above."
fi
log "Config OK."

# Some Ubuntu/NetworkManager boots start dnsmasq before the bench NIC exists,
# causing "unknown interface eno1" and breaking PXE until a manual restart.
# Add a tiny systemd guard for the interface carrying SERVER_IP.
WAIT_IFACE="$(ip -o -4 addr show | awk -v ip="$SERVER_IP" '$4 ~ "^" ip "/" {print $2; exit}')"
WAIT_IFACE="${WAIT_IFACE:-eno1}"
log "Installing dnsmasq wait guard for interface $WAIT_IFACE ..."
mkdir -p /etc/systemd/system/dnsmasq.service.d
cat > /etc/systemd/system/dnsmasq.service.d/vstl-wait-for-interface.conf <<EOF
[Unit]
Wants=network-online.target
After=network-online.target NetworkManager-wait-online.service systemd-networkd-wait-online.service

[Service]
ExecStartPre=/bin/sh -c 'for i in \$(seq 1 45); do ip link show "$WAIT_IFACE" >/dev/null 2>&1 && exit 0; sleep 1; done; echo "VSTL dnsmasq interface $WAIT_IFACE not present" >&2; exit 1'
EOF
systemctl daemon-reload

# --- 5. Restart service ---
log "Enabling and restarting dnsmasq..."
systemctl enable dnsmasq
systemctl restart dnsmasq

# Wait a moment, then verify it's actually running
sleep 2
if ! systemctl is-active --quiet dnsmasq; then
    journalctl -u dnsmasq -n 50 --no-pager
    die "dnsmasq failed to start. See journal above."
fi

# --- 6. Keep TFTP option negotiation enabled -------------------------------
# Dell Latitude 5490-class UEFI PXE requests RFC2347 options such as blksize
# and tsize when fetching intel.efi. Packet traces showed this firmware accepts
# the OACK path and aborts if tftpd-hpa refuses options and sends plain 512-byte
# data, so keep FOG's normal negotiated TFTP mode for the first-stage loader.
TFTPD_DEFAULT="/etc/default/tftpd-hpa"
TFTPD_COMPAT_OPTIONS='-s'
if [[ -f "$TFTPD_DEFAULT" ]]; then
    log "Configuring tftpd-hpa for negotiated PXE TFTP..."
    if grep -q '^TFTP_OPTIONS=' "$TFTPD_DEFAULT"; then
        sed -i "s|^TFTP_OPTIONS=.*|TFTP_OPTIONS=\"$TFTPD_COMPAT_OPTIONS\"|" "$TFTPD_DEFAULT"
    else
        printf 'TFTP_OPTIONS="%s"\n' "$TFTPD_COMPAT_OPTIONS" >> "$TFTPD_DEFAULT"
    fi
    systemctl restart tftpd-hpa || warn "tftpd-hpa restart failed; check systemctl status tftpd-hpa"
fi

# --- 7. Open firewall ports for PXE DHCP / PXE ---
if command -v ufw >/dev/null; then
    log "Opening firewall ports..."
    ufw allow 67/udp comment 'DHCP server'   >/dev/null
    ufw allow 4011/udp comment 'PXE proxy'   >/dev/null
fi

# --- 8. Done ---
cat <<EOF

${GREEN}========================================================================
DNSMASQ / PXE DHCP READY
========================================================================${NC}

Config file:    $CONFIG_DST
Server IP:      $SERVER_IP
Bench subnet:   $BENCH_SUBNET
PXE DHCP range: $VSTL_PXE_DHCP_START - $VSTL_PXE_DHCP_END (tagged PXEClient only)
UEFI bootfile:  $UEFI_BOOTFILE
Old Dell UEFI:  $OLD_DELL_UEFI_BOOTFILE
Service status: $(systemctl is-active dnsmasq)
TFTP options:   $TFTPD_COMPAT_OPTIONS

To watch live PXE-boot traffic (run BEFORE booting a bench):
    sudo journalctl -u dnsmasq -f

When you boot a bench laptop with F12 → PXE, you should see:
    DHCPDISCOVER(eth0) AA:BB:CC:DD:EE:FF
    DHCPOFFER(eth0)    10.255.0.x
    BOOTP message ...  undionly.kpxe sent

If you do NOT see anything, the bench is on a different VLAN/broadcast domain
than the imaging server. Move it to the same VLAN as $SERVER_IP.

Next step: build the Live ISO
    sudo /opt/vstl-imaging/04_build_live_iso.sh

EOF
