# Step 5 — Register the VSTL Live ISO with FOG

After `04_build_live_iso.sh` finishes, you have a bootable ISO at:

```
/opt/vstl-imaging/build/vstl-live-amd64.iso
```

Now we need to tell FOG to PXE-serve this ISO so bench laptops can boot from it.

---

## Option A — Quick test (single bench, USB boot)

Burn the ISO to a USB stick and boot a bench laptop from it:

```bash
sudo dd if=/opt/vstl-imaging/build/vstl-live-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

(Replace `/dev/sdX` with your actual USB device — `lsblk` to find it.)

Plug it into a bench laptop, press F12, choose USB. The Live OS should boot, auto-login as `vstl`, and the systemd service `vstl-imaging-client` should fire automatically. Watch its progress with:

```bash
journalctl -u vstl-imaging-client -f
```

---

## Option B — Full PXE flow via FOG

This is the production path. Once configured, any bench laptop on the network can press **F12 → PXE boot** with no USB needed.

### B.1 — Make the ISO accessible to FOG

FOG serves files via TFTP from `/tftpboot/`. We'll put the ISO there and chain to it from the iPXE menu.

```bash
sudo mkdir -p /tftpboot/vstl
sudo cp /opt/vstl-imaging/build/vstl-live-amd64.iso /tftpboot/vstl/
sudo chmod -R 755 /tftpboot/vstl
sudo chown -R fogproject:root /tftpboot/vstl
```

### B.2 — Add an iPXE boot menu entry in FOG

1. Open **FOG webUI**: `http://10.255.0.75/fog/management`
2. Login (default `fog` / `password` — change immediately if you haven't).
3. Click **iPXE** in the top menu (or **FOG Configuration → iPXE New Menu Entry**).
4. Click **New Menu Entry** and fill in:
   - **Menu Item**: `vstl.live.boot`
   - **Description**: `VSTL Imaging — Live ISO`
   - **Parameters**:
     ```
     kernel memdisk iso raw
     initrd tftp://${fog-ip}/vstl/vstl-live-amd64.iso
     boot
     ```
   - **Menu Show with**: `All Hosts` (or restrict to a specific host group later)
   - **Default Item**: ☑ (so unknown hosts auto-PXE-boot it)
5. Click **Add**.

### B.3 — (Optional) Set as the default boot for unknown hosts

Settings → **FOG Configuration → FOG Settings → iPXE → FOG_PXE_DEFAULT** → set to `vstl.live.boot`.

This way, any laptop that PXE-boots without being registered in FOG will go straight into the VSTL Live ISO.

### B.4 — Test

1. Power on a bench laptop, press **F12** (or whatever the PXE-boot key is for that vendor — Lenovo: F12, HP: F9, Dell: F12).
2. Choose **Network** / **PXE IPv4**.
3. The FOG iPXE menu should appear; pick `VSTL Imaging — Live ISO`.
4. ISO loads; Live OS boots; bench client runs.

Watch the imaging server's logs while the bench boots:

```bash
sudo journalctl -u dnsmasq -f
```

You should see DHCP requests from the bench's MAC, followed by TFTP fetches.

---

## Troubleshooting

### iPXE shows the menu but boot says "Could not boot: Operation not supported"
Some UEFI firmwares can't boot a `memdisk`-loaded ISO. Two options:
1. Use a smaller PXE-friendly initramfs instead of the full ISO (`live-build` with `--binary-images netboot`).
2. Switch to legacy BIOS in the bench's BIOS setup.

### Bench gets an IP but PXE menu never loads
- Confirm `tftpd-hpa` is running: `systemctl status tftpd-hpa`.
- Confirm the firewall allows UDP 69: `sudo ufw status`.
- Tail TFTP logs: `sudo tail -f /var/log/syslog | grep tftp`.

### Bench boots but `vstl-imaging-client` does nothing
- SSH in: `ssh vstl@<bench-ip>` (password `vstl@2026`).
- `systemctl status vstl-imaging-client` and `journalctl -u vstl-imaging-client -n 200`.
- Most common cause: `VSTL_API_KEY` placeholder was never replaced. Check `/opt/vstl/config.env` on the bench. If wrong, rebuild the ISO with the correct `.env`.

### Lookup returns 401 Invalid API key
- The key in `/opt/vstl/config.env` doesn't match an active key in VSTL 360.
- Generate a new one in VSTL 360 → `/imaging` → **API Keys** tab.
- Update `/opt/vstl-imaging/.env` and rebuild the ISO.
