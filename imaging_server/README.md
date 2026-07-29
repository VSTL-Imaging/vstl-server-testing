# VSTL Imaging Server — Complete Setup Bundle

End-to-end installation kit for the VSTL Imaging Server: FOG Project + dnsmasq
proxyDHCP + custom Live ISO + bench client that talks to your VSTL 360 backend.

---

## Target environment

| Item | Value |
|---|---|
| OS | Ubuntu 24.04.4 LTS (64-bit) |
| Imaging server IP | `10.255.0.75` (static) |
| Server hostname | `vstl-imaging` (recommended) |
| Local user | `vstl` (sudo) |
| FOG admin user | `fog` (set during install) |
| VSTL 360 API | `https://tech-audit-system.emergent.host/api` |

---

## Laptops without built-in Ethernet

Build `vstl-usb-live-amd64.iso` with `04_build_usb_iso.sh`, then write it to a
USB stick with Rufus or balenaEtcher. The USB environment starts the normal
VSTL L1/L2 workflow and tries:

1. An existing reachable connection.
2. Every physical wired adapter, including USB Ethernet.
3. Interactive Wi-Fi selection.

See `USB_BOOT.md` for the complete build and operator procedure.

---

## Bundle contents

```
imaging_server/
├── README.md                          ← you are here
├── 01_install_fog.sh                  ← installs FOG on Ubuntu 24.04
├── 02_install_dnsmasq.sh              ← installs dnsmasq + applies proxyDHCP config
├── 03_dnsmasq_proxydhcp.conf          ← dnsmasq config (proxyDHCP, coexists with existing DHCP)
├── 04_build_live_iso.sh               ← builds VSTL Live ISO (Debian live-build)
├── 05_register_iso_with_fog.md        ← step-by-step: tell FOG to PXE-serve the Live ISO
├── bench-client/
│   ├── vstl-imaging-client.sh         ← THE BRIDGE between bench laptops and VSTL backend
│   ├── vstl-imaging-client.service    ← systemd unit (auto-runs on Live ISO boot)
│   └── config.env.example             ← VSTL_API_BASE + VSTL_API_KEY placeholders
└── .env.example                       ← local config for the install scripts
```

---

## Order of operations (do exactly this on the imaging server)

### Step 0 — Prep the server (5 min)

SSH into `10.255.0.75` as `vstl` (or use a console). Make sure the host:
- has a static IP `10.255.0.75/24` set in `/etc/netplan/*.yaml`
- can reach the public internet (`apt update` works)
- has `sudo` for the `vstl` user (`sudo -v`)

```bash
sudo apt update && sudo apt -y upgrade
sudo hostnamectl set-hostname vstl-imaging
sudo apt -y install git curl wget vim
```

Clone (or `scp`) this `imaging_server/` directory to `/opt/vstl-imaging/` on the server:

```bash
sudo mkdir -p /opt/vstl-imaging
sudo chown vstl:vstl /opt/vstl-imaging
# Then copy the bundle into /opt/vstl-imaging/ via scp/rsync/git clone
chmod +x /opt/vstl-imaging/*.sh
```

### Step 1 — Configure local secrets

```bash
cd /opt/vstl-imaging
cp .env.example .env
nano .env                      # fill in VSTL_API_KEY (get from VSTL 360 → Imaging → API Keys tab)
chmod 600 .env                 # secrets — owner-only
```

### Step 2 — Install FOG (15-30 min, mostly waiting for build)

```bash
sudo /opt/vstl-imaging/01_install_fog.sh
```

The official FOG installer is **interactive** — it asks you to confirm IP, DB password, etc.
The wrapper script seeds answers; review the prompts and accept defaults unless noted.

When it finishes:
- FOG webUI: `http://10.255.0.75/fog/management`
- Default login: `fog` / `password` → **change immediately**.

### Step 3 — Install proxyDHCP / TFTP (2 min)

```bash
sudo /opt/vstl-imaging/02_install_dnsmasq.sh
```

This puts `03_dnsmasq_proxydhcp.conf` at `/etc/dnsmasq.d/vstl-imaging.conf` and
restarts the service. ProxyDHCP means dnsmasq does NOT hand out IPs (your
existing DHCP keeps doing that) — it only tells PXE-booting clients where to
download the boot file from FOG.

### Step 4 — Build the VSTL Live ISO (20-40 min, mostly waiting for `live-build`)

```bash
sudo /opt/vstl-imaging/04_build_live_iso.sh
```

Produces `/opt/vstl-imaging/build/vstl-live-amd64.iso` (~700 MB).
This ISO contains:
- Minimal Debian 12 base
- `vstl-imaging-client.sh` baked at `/opt/vstl/`
- `vstl-imaging-client.service` set to auto-run at boot
- `dmidecode`, `smartctl`, `nwipe`, `curl`, `jq` pre-installed

### Step 5 — Register the ISO with FOG so benches PXE-boot it

Follow `05_register_iso_with_fog.md` (5 min, FOG webUI work).

### Step 6 — Power on a bench laptop, press F12, choose PXE

If everything is wired correctly, the laptop will:
1. PXE-boot from FOG → load VSTL Live ISO
2. systemd starts `vstl-imaging-client.service`
3. Client reads BIOS → calls `GET /api/imaging/lookup?model=...` → gets golden image path
4. Client calls FOG to deploy the image → runs diagnostics → wipes → calls `POST /api/imaging/ingest`
5. Result shows up in VSTL 360 under `/imaging` → "Imaging Logs"

---

## Troubleshooting

### Bench doesn't get an IP at PXE
- Confirm the bench port is on the same VLAN as `10.255.0.75`.
- Check `journalctl -u dnsmasq -f` on the imaging server while the bench boots — you should see DHCPDISCOVER from the bench's MAC.
- Ensure UFW/firewall allows ports `67/udp`, `69/udp`, `4011/udp` inbound on the imaging server.

### PXE menu loads but ISO won't start
- Check FOG's `/images/` permissions: `ls -la /images/` → must be world-readable.
- Check FOG NFS export: `exportfs -rv` — you should see `/images/dev` exported.

### Bench client logs aren't reaching VSTL 360
- SSH into a running bench (it has `sshd` enabled with `vstl` / `vstl@2026`):
  ```
  ssh vstl@<bench-ip>
  systemctl status vstl-imaging-client
  journalctl -u vstl-imaging-client -n 200
  ```
- Most common cause: `VSTL_API_KEY` was not embedded into the ISO. Check `/opt/vstl/config.env` on the bench.
- Second most common: bench cannot reach `https://tech-audit-system.emergent.host` — usually a firewall / outbound HTTPS issue.

### Generated reports show wrong image deployed
- Verify the golden copy registered in VSTL 360 at `/imaging` → "Golden Copies" tab matches the model name FOG knows.
- Run a manual lookup against the API to see what the bench would receive:
  ```
  curl -H "X-API-Key: $VSTL_API_KEY" \
       "https://tech-audit-system.emergent.host/api/imaging/lookup?model=THINKPAD%20T490"
  ```

---

## Security notes

- The `vstl` / `vstl@2026` credentials are **for the LAB/bench server only** — the
  imaging server should NOT be reachable from the public internet. Confirm with:
  ```
  sudo ufw status verbose
  ```
- `VSTL_API_KEY` baked into the Live ISO grants ingest rights — treat it like a secret.
  When rotating, rebuild the ISO with `04_build_live_iso.sh`.
- FOG's default `fog`/`password` admin must be changed before going live.

---

## What this bundle does NOT do (yet)

- Multicast image deployment (FOG supports it; bench client would need to be told to wait)
- Bench load-balancing across multiple imaging servers
- Mass key rotation across already-built ISOs (currently requires rebuild)
- TLS for the FOG webUI (HTTP-only; intentional for LAN-only access — don't expose to internet)
