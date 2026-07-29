# VSTL Imaging — PXE Netboot Setup (Production)

**Zero-USB workflow.** Bench operators plug in ethernet + press F12 → network boot. That's it.

---

## Architecture

```
                          ┌─────────────────────────────────┐
                          │  Bench Laptop  (UEFI + LAN)     │
                          │                                 │
                          │  Power-on → F12 → PXE IPv4      │
                          └───────────────┬─────────────────┘
                                          │
                                DHCP DISCOVER (broadcast)
                                          │
         ┌────────────────────────────────┼────────────────────────────────┐
         │                                │                                │
         ▼                                ▼                                ▼
┌─────────────────┐         ┌──────────────────────────┐         ┌─────────────────┐
│ Corporate DHCP  │         │  dnsmasq proxyDHCP       │         │  (nothing else) │
│ → gives IP      │         │  → gives PXE boot info   │         │                 │
│   10.255.x.y/24 │         │   points at FOG iPXE     │         │                 │
└─────────────────┘         └──────────┬───────────────┘         └─────────────────┘
                                       │
                          TFTP: undionly.kpxe (BIOS) / full ipxe.efi (UEFI)
                                       │
                                       ▼
                          ┌──────────────────────────┐
                          │  FOG iPXE Menu           │
                          │  → Default entry:        │
                          │    chain http://.../     │
                          │           boot.ipxe      │
                          └──────────┬───────────────┘
                                     │
                    HTTP (Apache): /vstl-pxe/vmlinuz + initrd + filesystem.squashfs
                                     │
                                     ▼
                          ┌──────────────────────────┐
                          │  VSTL Imaging Live       │
                          │  → auto-DHCP             │
                          │  → dmidecode hardware    │
                          │  → POST /imaging/ingest  │
                          │  → auto-poweroff         │
                          └──────────────────────────┘
```

---

## Prerequisites (one-time)

- ✅ `01_install_fog.sh` has been run on `10.255.0.75` → FOG + Apache + TFTP are up.
- ✅ `02_install_dnsmasq.sh` has been run → proxyDHCP active on your bench LAN.
- ✅ `04_build_live_iso_clonezilla.sh` has produced `build/vstl-live-amd64.iso` and you validated it end-to-end from a USB at least once.

If all three are green, you're ready.

---

## Step 1 — Publish the ISO as PXE assets

On your imaging server:

```bash
cd ~/Desktop/vstl/vstl-imaging-server_20260501_145959/imaging_server
sudo ./06_setup_pxe_netboot.sh
```

This takes ~20 seconds. It extracts kernel + initrd + squashfs from your ISO, drops them under `/var/www/html/vstl-pxe/`, writes a small iPXE boot script, and smoke-tests HTTP reachability. Re-run it after every ISO rebuild.

Expected output tail:
```
  OK  http://10.255.0.75/vstl-pxe/vmlinuz
  OK  http://10.255.0.75/vstl-pxe/initrd.img
  OK  http://10.255.0.75/vstl-pxe/filesystem.squashfs
  OK  http://10.255.0.75/vstl-pxe/boot.ipxe
✓ PXE NETBOOT READY
```

If any line says `FAIL` → Apache isn't running. `sudo systemctl restart apache2` and re-run.

---

## Step 2 — Add the menu entry in FOG (2 min, one-time)

1. Open **FOG webUI**: `http://10.255.0.75/fog/management`
2. Login. (Default: `fog` / `password` — change it now if you haven't.)
3. Top menu → **iPXE** → **New Menu Entry**.
4. Fill in:

   | Field | Value |
   |---|---|
   | Menu Item | `vstl.live.netboot` |
   | Description | `VSTL Imaging — Live Netboot` |
   | Parameters | `chain http://${fog-ip}/vstl-pxe/boot.ipxe` |
   | Menu Show with | `All Hosts` |
   | Default Item | ☑ (tick this for F12 → PXE → auto-boot) |
   | Hotkey | (optional) e.g. `V` for VSTL |

5. Click **Add**.

### Alternative — make it the default for unknown hosts too

FOG Configuration → FOG Settings → iPXE → **FOG_PXE_DEFAULT** → `vstl.live.netboot` → Save.

Now any laptop that PXE-boots on this LAN (registered or not) goes straight to VSTL imaging.

---

## Step 3 — Test on a bench

1. Plug bench laptop into the same LAN as the imaging server.
2. Power on → **F12** (or F9 on some HP, F12 on Dell/Lenovo).
3. Select **Network Boot / PXE IPv4** (or **IPv6** — both work with FOG).
4. (If default-item was ticked, skip this) Pick `VSTL Imaging — Live Netboot` from the iPXE menu.
5. Watch it go:
   - 5 sec: kernel (12 MB) fetches over HTTP
   - 15 sec: initrd (48 MB) fetches
   - Live system comes up → `[VSTL] ensure_network` → `[VSTL] Reading hardware identity` → `Ingest response: {"success":true}` → **powers off**.

Total: ~90 seconds from F12 press to poweroff. 30 bench laptops can run in parallel (limited only by your LAN's bandwidth to the imaging server).

---

## Operator workflow (final state)

1. Plug ethernet + power on.
2. Press F12 → Network Boot.
3. Walk away.

**No USB. No prompts. No technician input.**

---

## Refreshing after a bench-client script change

If you edit `bench-client/vstl-imaging-client.sh` (e.g. to change kernel cmdline, add telemetry, tweak a diagnostic check):

```bash
cd ~/Desktop/vstl/vstl-imaging-server_20260501_145959/imaging_server

# 1. Rebuild the squashfs (~90 sec)
sudo install -m 0755 bench-client/vstl-imaging-client.sh build/cz/rootfs/opt/vstl/vstl-imaging-client.sh
sudo rm -f build/cz/extract/live/filesystem.squashfs
sudo mksquashfs build/cz/rootfs build/cz/extract/live/filesystem.squashfs \
    -comp xz -b 1048576 -Xbcj x86 -noappend -no-progress

# 2. Rebuild the ISO (~30 sec)
sudo rm -f build/vstl-live-amd64.iso
sudo xorriso -indev build/clonezilla-live-3.2.2-15-amd64.iso \
    -outdev build/vstl-live-amd64.iso -boot_image any replay -volid VSTL_LIVE \
    -update_r build/cz/extract/live/filesystem.squashfs /live/filesystem.squashfs \
    -update_r build/cz/extract/syslinux /syslinux \
    -update_r build/cz/extract/boot /boot \
    -update_r build/cz/extract/EFI /EFI \
    -commit

# 3. Re-publish PXE assets (~20 sec)
sudo ./06_setup_pxe_netboot.sh
```

The next bench boot picks up the new code automatically. No USB re-flashing, no FOG menu changes.

---

## Troubleshooting

### `F12 → PXE` shows no devices / times out
- LAN cable not plugged in, or bench is on a different VLAN from `10.255.0.75`.
- Move bench to same VLAN, or configure IP helpers (DHCP relay) on your switch.

### Older Dell Latitude / HP ProBook IPv4 PXE never reaches the VSTL menu
- Re-run `sudo ./02_install_dnsmasq.sh` after this bundle is installed. It
  publishes a PXEClient-only DHCP range with direct bootfile/next-server
  fields. It deliberately does not publish dnsmasq `pxe-service` menu metadata;
  Latitude 5490-class firmware can accept the DHCPACK and then never TFTP-fetch
  the NBP when that metadata is present.
- Default UEFI loader preference is `ipxe.efi`, then `vstl-clean-ipxe.efi`, then
  `snponly.efi`. Latitude 5490-class firmware can accept `snponly.efi` and then
  fall back to BIOS before iPXE appears, so prefer the full iPXE EFI loader for
  those systems. To force one, set `VSTL_UEFI_BOOTFILE=ipxe.efi`,
  `vstl-clean-ipxe.efi`, or `snponly.efi` in `/opt/vstl-imaging/.env`, then
  re-run `sudo ./02_install_dnsmasq.sh`.
- Watch `sudo journalctl -u dnsmasq -f` while booting. You should see
  `undionly.kpxe` for legacy BIOS, `ipxe.efi` / `vstl-clean-ipxe.efi` for
  UEFI firmware, then `http://10.255.0.75/vstl-pxe/boot.ipxe` on iPXE's
  second DHCP pass.
- If syslog shows `in.tftpd: client does not accept options`, re-run
  `sudo ./02_install_dnsmasq.sh`. It sets `tftpd-hpa` to refuse RFC2347 TFTP
  options so old Dell PXE ROMs receive the first-stage loader via plain TFTP.

### iPXE menu appears but `chain` call fails with `Connection refused`
- Apache not running on imaging server: `sudo systemctl restart apache2`.
- Test from any other machine: `curl -I http://10.255.0.75/vstl-pxe/boot.ipxe` should return `200 OK`.

### Kernel loads, then `initramfs` prompt appears (not VSTL logs)
- The squashfs failed to download or verify. Check Apache logs: `sudo tail -f /var/log/apache2/access.log`.
- Ensure `/var/www/html/vstl-pxe/filesystem.squashfs` is world-readable.

### Bench shows Language/Keyboard prompts (Clonezilla wizard)
- Your `boot.ipxe` is missing the `ocs_live_*` kernel params. Re-run `06_setup_pxe_netboot.sh` — it regenerates the iPXE script correctly.

### VSTL client runs but `Ingest failed: Could not resolve host`
- Bench has an IP but can't resolve `tech-audit-system.emergent.host`. Check corporate DHCP is handing out a working DNS server. Quick test at the `user@debian:~$` prompt:
  ```
  nslookup tech-audit-system.emergent.host
  ```

### Two laptops boot simultaneously and one hangs
- Apache's default `MaxRequestWorkers` (150) is more than enough for 30+ concurrent bench boots, but if you have an older FOG install with nginx instead, check `worker_connections`.

---

## What's on disk after setup

```
/var/www/html/vstl-pxe/
├── boot.ipxe                 # ~1 KB  — iPXE script with kernel cmdline
├── vmlinuz                   # ~12 MB — Linux kernel
├── initrd.img                # ~48 MB — initial ramdisk
└── filesystem.squashfs       # ~391 MB — rootfs with /opt/vstl/ baked in

/tftpboot/
└── vstl.ipxe                 # Copy of boot.ipxe for TFTP clients (fallback)
```

Total LAN footprint per bench boot: **~460 MB** (same as the ISO), but only ~60 MB loads before the kernel comes up — the squashfs streams on demand while the client is already running.

---

Version: 1.0 · 2026-05-02
