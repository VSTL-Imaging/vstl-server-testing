# Speaker and Microphone QC Restoration

Updated: 2026-06-07

- Speaker and Microphone remain mandatory in both L1 and L2 QC workflows.
- The tests no longer disappear when ALSA device enumeration is delayed.
- Before each test, the client loads common Intel audio drivers, settles udev,
  initializes ALSA, and retries playback/capture detection.
- Detection also falls back to `/proc/asound/cards`, `/proc/asound/pcm`, and
  `/sys/class/sound`.
- The Clonezilla live rootfs includes:
  - `alsa-utils`
  - `firmware-sof-signed`
  - `firmware-intel-sound`

## Live Deployment

- Server: `100.88.250.63`
- Deployment timestamp: `20260607_225844`
- Rollback backup:
  `/opt/vstl-backups/bench-client-deploy-20260607_225844`
- Live PXE squashfs SHA256:
  `f548866c09e13c5f25f2d37c10f0d2bb7dcc524dcb6457c4669991ac84d53591`
- Server setup-current SHA256:
  `f3716ddd6a2b140b57d4dd4d09528f507d3d22bffa3c25d09d42e883d75df2a3`

The PXE loader, kernel, initrd, DHCP, and TFTP boot chain were not changed.
