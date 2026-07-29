# Touch Test and Capture Authorization Checkpoint

Updated: 2026-06-06

## Touch display QC

- Touch-capable displays wait for their evdev input device for up to 8 seconds.
- Udev-identified touchscreen event paths are opened first, then every sibling
  `/dev/input/event*` node is inspected for the usable coordinate stream.
- Generic HID-over-I2C multitouch controllers are accepted from their evdev
  capabilities while touchpads and indirect pointers remain excluded.
- A detected touch display cannot silently skip the full-screen touch map.
- If the touch map cannot initialize, the technician must retry or explicitly
  mark the touch test as failed.
- Non-touch displays still skip the touch map after the three-second display
  information screen.

## Capture authorization

Image capture is blocked until Certified Secure Erase has completed and passed
verification for the exact combination of:

- laptop/system serial number
- physical storage serial number or WWN

Successful erase writes a persistent authorization record to:

`/images/dev/.vstl-secure-erase/`

The authorization remains valid for the same laptop and physical drive across
reboots and reinstallations. Replacing the drive or moving the drive to a
different laptop requires another successful Certified Secure Erase.

Existing erase certificates created before this update do not have these local
authorization records. Run Certified Secure Erase once after this update before
capturing from each laptop and drive combination.

## Deployment

- Live PXE squashfs updated.
- DHCP, TFTP, iPXE, kernel, and raw initrd boot-chain files were not changed.
- The deployment archive process preserves `.vstl-secure-erase`.
