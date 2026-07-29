# VSTL Imaging Bench — Operator Quickstart

**Print this page. Tape it to every bench.**

---

## What this USB does

When booted, it automatically:

1. Reads the laptop's **serial number, model, CPU, RAM, SSD health, battery health**
2. Sends everything to VSTL 360 (linked to the existing asset by serial)
3. Powers the laptop off

**Total operator time: 30 seconds** (plug USB → pick boot device → wait).

---

## What the operator does

| Step | Action |
|---|---|
| 1 | Plug the **VSTL Imaging USB** into the laptop |
| 2 | Plug the **LAN cable** into the laptop (required for network) |
| 3 | Power on → press boot-menu key (usually **F12** / F9 / Esc) |
| 4 | Select the USB from the list |
| 5 | At the GRUB menu, press **Enter** (or just wait 30 sec) |
| 6 | Wait ~60 seconds — the laptop powers itself off |
| 7 | Unplug the USB → the laptop is done |

That's it. No menus, no prompts, no technician input.

---

## How to verify it worked

Open VSTL 360 → **Assets** → search by serial number that was on the laptop's sticker.

You should see a new **Imaging** entry with timestamp matching today. Done.

---

## If something goes wrong

| Symptom | What it means | What to do |
|---|---|---|
| Laptop sits at blank screen for >2 min | Boot hardware issue | Try a different USB port, or try another laptop |
| Logs show `Network: no wired interface found` | LAN cable not plugged in | Plug the LAN cable, reboot |
| Logs show `Network: DHCP did not complete in 30s` | No DHCP server on this LAN | Ask IT to check the bench network |
| Logs show `Ingest failed` | Backend unreachable | Ping `tech-audit-system.emergent.host` from any other machine; contact admin if down |
| Laptop drops to a "Choose mode" menu after ~30 sec | Script exited silently early | Pick `cmd` → run `cat /var/log/vstl-imaging-client.log` → send photo to admin |

---

## Credentials in the USB

The USB has a config file at `/opt/vstl/config.env` on the bootable partition with:
- `VSTL_API_BASE` = `https://tech-audit-system.emergent.host/api`
- `VSTL_API_KEY` = (scoped imaging key from VSTL 360 → Imaging → API Keys)
- `BENCH_ID` = unique per bench (e.g. `bench-1`, `bench-2`)

If you rotate the API key in VSTL, re-generate the USB using:
```
sudo ./04_build_live_iso_clonezilla.sh
sudo dd if=build/vstl-live-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

---

## Build-server commands (admin only)

All on the **build server** at `~/Desktop/vstl/vstl-imaging-server_.../imaging_server`:

| Task | Command |
|---|---|
| Fresh ISO build (after Clonezilla upgrade or script rewrite) | `sudo ./04_build_live_iso_clonezilla.sh` |
| Re-squash after editing bench-client script | `sudo install -m 0755 bench-client/vstl-imaging-client.sh build/cz/rootfs/opt/vstl/vstl-imaging-client.sh && sudo rm -f build/cz/extract/live/filesystem.squashfs && sudo mksquashfs build/cz/rootfs build/cz/extract/live/filesystem.squashfs -comp xz -b 1048576 -Xbcj x86 -noappend -no-progress` |
| Re-package ISO | `sudo rm -f build/vstl-live-amd64.iso && sudo xorriso -indev build/clonezilla-live-3.2.2-15-amd64.iso -outdev build/vstl-live-amd64.iso -boot_image any replay -volid VSTL_LIVE -update_r build/cz/extract/live/filesystem.squashfs /live/filesystem.squashfs -update_r build/cz/extract/syslinux /syslinux -update_r build/cz/extract/boot /boot -update_r build/cz/extract/EFI /EFI -commit` |
| Flash USB | `sudo dd if=build/vstl-live-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync` (replace `sdX` with **real** device from `lsblk`) |

⚠️ Always verify device is a **block device** (`brw-rw----`, starts with `b`) before `dd`. Regular files (`-rw-r--r--`, starts with `-`) mean `dd` writes into a file, not the USB.

---

## Proven working on

- HP ProBook 640 G8 (i5-1145, 16GB, 256GB SSD, UEFI boot)
- Confirmed ingest on `https://tech-audit-system.emergent.host` (record_id `d10085fd-...`, 2026-05-02 15:07 UTC)

---

Version: 1.0 · Last updated: 2026-05-02
