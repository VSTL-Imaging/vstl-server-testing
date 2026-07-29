# Touch, Ports, and HP PXE Fix - 2026-06-11

## Touch QC

- The touch map now uses the curses console renderer by default.
- Direct `/dev/fb0` drawing is disabled unless `VSTL_TOUCH_USE_FB=1` is set.
- This prevents Linux console/KVM text and scan lines from being drawn over the map.
- Completing every block returns `PASS`.
- Double-tapping `Esc` within 1.5 seconds, timing out, or failing to start the map returns `FAIL`.
- A dedicated result screen displays `RESULT: PASS` or `RESULT: FAIL` for three seconds. Enter skips the delay.

## Ports QC

- Removed model-name and generic business-laptop port guesses.
- Rows now come from SMBIOS external connectors, Linux Type-C ports, and DRM display connectors.
- A PCI network controller is not treated as proof of a physical RJ45 socket.
- USB interactions are tracked by stable udev hardware paths so repeatedly using one socket cannot complete another row.
- Audio, Ethernet, display, and power rows require a new disconnect/reconnect transition during the test.
- Devices already connected when the test opens are not automatically marked as tested.

## HP UEFI PXE

- 64-bit UEFI PXE now receives `snponly.efi`.
- The SNP-only loader reuses the NIC driver initialized by firmware and avoids the full iPXE native-device probe that can hang at `iPXE initialising devices` on affected HP laptops.
- BIOS and 32-bit UEFI boot paths are unchanged.

## Validation

- Focused touch, ports, and PXE tests: 31 passed.
- Full local suite: 120 passed; one unrelated pre-existing strict Intune GUID audit test remains failing.
- Server Python compilation and shell syntax checks passed.
- dnsmasq configuration passed `dnsmasq --test`.
- `dnsmasq`, `apache2`, and `tftpd-hpa` are active.
- PXE kernel, squashfs, and boot script return HTTP 200.
- PXE squashfs contains the current TUI runtime.
- Direct-boot USB ISO SHA-256:
  `2ba66ae47105a81c82279ddb51b9ed99ba51eebd6176f002094c6dcdea224793`

## 2026-06-12 PXE Panic and Touch Grid Follow-up

- Production PXE now defaults to `classic-raw-cpio`.
- The active PXE kernel line no longer passes a named gzip initrd argument.
- `boot.ipxe` now loads `/vstl-pxe/initrd-vstl-fixed.cpio` directly, bypassing the gzip decoder path that triggered `invalid magic at start of compressed archive`.
- Touch grid curses rendering now reserves top and footer rows for status text.
- Touch blocks are drawn with explicit gaps and a hidden cursor so the map stays separated and avoids stray black cursor/console lines.
- Focused local tests: `21 passed`.
- Server PXE HTTP verification:
  - `boot.ipxe` HTTP 200
  - `vmlinuz` HTTP 200
  - `initrd-vstl-fixed.cpio` HTTP 200
  - `filesystem.squashfs` HTTP 200
- Direct-boot USB ISO SHA-256:
  `e9042b1ff09ff3f943f97da30bf36d096ec760f24b362a6631c4e69521e3961f`
