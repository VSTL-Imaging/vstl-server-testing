# Camera Live Video and Touch Detection Fix

Date: 2026-06-05

## Camera QC

- The primary preview uses FFmpeg to stream V4L2 camera video directly to
  `/dev/fb0` through the native `fbdev` output device.
- It tries common MJPEG and YUYV modes for compatibility with laptop webcams.
- The older Python BGRA/YUYV framebuffer renderers and still-image tools remain
  available only as fallbacks.
- The live image must include the Debian `ffmpeg` package with `fbdev` and
  `video4linux2` device support.

## Touch Display Detection

- Touch panels are detected from all of these Linux sources:
  - udev `ID_INPUT_TOUCHSCREEN=1`
  - evdev `INPUT_PROP_DIRECT`
  - absolute or multitouch coordinate capabilities
  - known direct-touch device names as a compatibility fallback
- Devices tagged as touchpads or indirect pointers are explicitly excluded.
- A non-touch result is shown for 3 seconds. Press `T` during that screen to
  force the touch-grid test when firmware or a driver does not expose the panel
  correctly.

## Live Deployment

- Server: `100.88.250.63`
- Rollback backup:
  `/opt/vstl-backups/bench-client-deploy-20260605_211538`
- Live PXE squashfs SHA256:
  `d3470e79eec3357d4fcea89b1c1cad97acac0ba73ff70bf58a23bfe4fa375bfd`
- Live embedded TUI SHA256:
  `28166d06adadbf6bf37fb827b0a54a761cc12491e3c1cd6a9eb77e919da3df88`
- Live embedded QC module SHA256:
  `0dd169f16ff740c01d0e500fcd5025f9ee9d1947ac316731bf64f94d0f7a39da`
- Setup-current backup SHA256:
  `d0670b3c34a38eedfa874286983fd76e2edcd31052fa538460ffb479a648f079`

The PXE kernel, raw initrd, iPXE loader, DHCP/TFTP flow, and boot handoff were
not changed by this repair.

## Touch Event Device Launch Repair

Updated: 2026-06-07

- The touch-map launcher now scans every `/dev/input/event*` node. It no
  longer rejects usable sibling event devices when the initial display probe
  reports a different HID-over-I2C event path.
- Event nodes are classified using coordinate capabilities, touch keys,
  direct/pointer properties, udev touchscreen/touchpad properties, and device
  names.
- Touchpads and indirect pointers remain excluded.
- The live entry script makes event nodes readable before launching the TUI.
- The live rootfs and both build scripts include:
  - `python3-evdev`
  - `evtest`
  - `libdrm-tests` (`modetest`)
- Deployed live PXE squashfs SHA256:
  `b83a9b3fe9b5fe75b7eb26954b8b9cd3dcea3b9d54297c777f47095e80ac1e4f`
- Rollback backup:
  `/opt/vstl-backups/bench-client-deploy-20260607_223727`
