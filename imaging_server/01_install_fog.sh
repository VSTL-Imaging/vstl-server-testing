#!/usr/bin/env bash
# =============================================================================
# 01_install_fog.sh — Installs FOG Project on Ubuntu 24.04 LTS
# =============================================================================
# Run as: sudo /opt/vstl-imaging/01_install_fog.sh
#
# What this does:
#  1. Sanity checks (Ubuntu 24.04, root, network)
#  2. Installs all FOG prerequisites (apache, mysql, php, tftpd, nfs)
#  3. Clones the official FOG Project repo
#  4. Runs FOG's interactive installer with sensible pre-answers
#  5. Opens UFW ports for PXE + FOG webUI
#  6. Prints a post-install checklist
#
# Why we wrap the upstream installer instead of replacing it:
# FOG's `installfog.sh` is the source of truth — it handles MySQL setup,
# TFTP folder layout, image storage, NFS exports, and database schema.
# Reimplementing it would diverge from upstream and break upgrades.
# =============================================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn() { echo -e "${YELLOW}[$(date +%H:%M:%S)] WARN:${NC} $*"; }
die()  { echo -e "${RED}[$(date +%H:%M:%S)] FATAL:${NC} $*" >&2; exit 1; }

# --- 0. Pre-flight ---
[[ $EUID -eq 0 ]] || die "Must be run as root (use sudo)."

OS_VERSION=$(lsb_release -rs 2>/dev/null || echo "")
if [[ "$OS_VERSION" != "24.04" ]]; then
    warn "This script targets Ubuntu 24.04. Detected: $OS_VERSION"
    warn "Continuing anyway — FOG may need newer/older steps for your version."
    sleep 3
fi

# Load local config (server IP, hostname)
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
[[ -f "$SCRIPT_DIR/.env" ]] && source "$SCRIPT_DIR/.env"
SERVER_IP="${SERVER_IP:-10.255.0.75}"

# Verify the server actually has this IP bound
if ! ip -4 addr | grep -q "inet $SERVER_IP/"; then
    warn "$SERVER_IP is not bound to any interface on this host."
    warn "FOG will be configured for $SERVER_IP — make sure netplan assigns it."
fi

# Verify internet connectivity (for apt + git clone)
if ! curl -fsS --max-time 5 https://github.com >/dev/null; then
    die "No connectivity to github.com — FOG installer needs to clone the repo."
fi

# --- 1. Apt update + base tools ---
log "Updating apt cache and installing base build tools..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl wget ca-certificates lsb-release sudo

# --- 2. FOG dependencies ---
# These are what FOG's installer would install anyway, but we pre-install
# them to:
#   (a) fail fast on apt issues
#   (b) avoid the installer hanging on first-time package downloads
log "Installing FOG dependencies (this may take 5-10 min)..."
apt-get install -y -qq \
    apache2 php php-cli php-fpm php-mysql php-curl php-gd php-mbstring \
    php-xml php-bcmath php-soap php-zip libapache2-mod-php \
    mariadb-server mariadb-client \
    tftpd-hpa tftp-hpa \
    nfs-kernel-server \
    isc-dhcp-server \
    net-tools \
    cifs-utils \
    haveged \
    unzip tar gzip zstd \
    || die "Failed to install FOG dependencies."

# Ensure mariadb is started before FOG installer hits it
systemctl enable --now mariadb
systemctl enable --now apache2

# --- 3. Clone FOG Project ---
FOG_DIR="/opt/fogproject"
if [[ -d "$FOG_DIR/.git" ]]; then
    log "FOG repo already cloned at $FOG_DIR — pulling latest..."
    git -C "$FOG_DIR" pull --rebase
else
    log "Cloning FOG Project (stable branch)..."
    rm -rf "$FOG_DIR"
    git clone -b stable https://github.com/FOGProject/fogproject.git "$FOG_DIR"
fi

# --- 4. Run the FOG installer ---
# FOG installer is interactive. We pre-seed answers via expect-like input.
# Variables FOG asks about (from bin/installfog.sh):
#   linuxDist, ipaddress, interface, submask, hostname, routeraddress,
#   plainpassword (database root), username (mysql user), httpproto,
#   bldnoconfirm, install_type ('N' = normal server, 'S' = storage node)
#
# Reference: https://docs.fogproject.org/en/latest/installation/install-fog-server.html
log "Launching FOG installer (interactive — please review prompts!)..."
warn "When asked, accept defaults except:"
warn "  - HTTPS for FOG webUI? -> N  (HTTP only on LAN)"
warn "  - Send anonymous usage stats? -> N (or your choice)"
warn "  - Storage location? -> /images  (default)"
warn ""
warn "When the installer prints a database upgrade URL at the end:"
warn "  http://$SERVER_IP/fog/management"
warn "Open it in a browser and click 'Install/Upgrade Now' to finalize the schema."

cd "$FOG_DIR/bin"
./installfog.sh

# --- 5. Open firewall ports for PXE / FOG ---
if command -v ufw >/dev/null; then
    log "Opening UFW ports for PXE and FOG webUI..."
    ufw allow 22/tcp comment 'SSH'                 >/dev/null
    ufw allow 80/tcp comment 'FOG webUI HTTP'      >/dev/null
    ufw allow 67/udp comment 'DHCP server'         >/dev/null
    ufw allow 69/udp comment 'TFTP'                >/dev/null
    ufw allow 4011/udp comment 'PXE proxy'         >/dev/null
    ufw allow 2049/tcp comment 'NFS image storage' >/dev/null
    ufw allow 111/tcp comment 'rpcbind'            >/dev/null
    ufw allow 111/udp comment 'rpcbind'            >/dev/null
fi

# --- 6. Post-install checklist ---
cat <<EOF

${GREEN}========================================================================
FOG INSTALLATION COMPLETE
========================================================================${NC}

Next steps (in this order):

  1. Open the FOG webUI:
       http://$SERVER_IP/fog/management

     Default login:  fog / password
     ${RED}>> Change the password immediately under Users -> fog -> Change Password${NC}

  2. (Optional) Run the database upgrade if the installer asked you to.

  3. Run the dnsmasq / proxyDHCP installer next:
       sudo /opt/vstl-imaging/02_install_dnsmasq.sh

  4. Then build the Live ISO:
       sudo /opt/vstl-imaging/04_build_live_iso.sh

If anything failed, the FOG installer log is at:
    /opt/fogproject/error_logs/fog_error_*.log

EOF
