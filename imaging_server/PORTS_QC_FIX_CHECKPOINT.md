# Ports QC Continuation Fix

Updated: 2026-06-07

## Fixed

- USB-A rows now track a companion-controller-safe socket fingerprint.
  Replugging the same physical USB socket cannot complete another USB row, even
  if the USB 2.0 and USB 3.x sides appear as different bus paths.
- USB-C has a fallback path for systems that do not expose a Type-C partner
  device: after all USB-A rows are complete, a new USB event can complete the
  remaining USB-C row.
- Display connector detection now parses connector names such as HDMI, VGA,
  DP/DisplayPort, and DVI instead of relying on a narrow prefix list.
- Ethernet link detection now checks carrier, operstate, interface flags, and
  `ethtool` link state when available.
- Audio-jack detection now also checks udev switch-tagged input devices, not
  only event device names containing headphone/mic terms.
- Future live builds include `ethtool`; the current live rootfs also has it
  installed.

## Live Deployment

- Server: `100.88.250.63`
- Deployment timestamp: `20260607_232125`
- Rollback backup:
  `/opt/vstl-backups/bench-client-deploy-20260607_232125`
- Live PXE squashfs SHA256:
  `4f0f6bf9b6237c0b3989f39b91c5fea305d2b906e6b267bb3ca7b1b49647a8da`
- Server setup-current SHA256:
  `a97c243605807274b809a3abeeeab20997f0d96296e1dcec2a535860813274fc`

The PXE loader, kernel, initrd, DHCP, and TFTP boot chain were not changed.
