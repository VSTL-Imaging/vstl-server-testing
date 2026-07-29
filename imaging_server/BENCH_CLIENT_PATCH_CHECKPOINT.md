# Bench Client Patch Checkpoint

Updated: 2026-05-26 13:10 Asia/Dubai

Scope:
- Bench-client/runtime logic only.
- PXE bootloader, initrd, TFTP, DHCP, and boot chain were not modified.

Included fixes:
- Stricter MDM/Intune/Azure AD evidence detection with known false-positive probes removed.
- Quiet NFS fallback to local FOG `/images/dev` when backend NFS settings are missing.
- Windows OS name/version detection improvements for capture naming and restore selection.
- GPU memory display improvements for discrete GPUs, including MX550 fallback.
- Restore backup precheck before hardware detail screens, QC, secure erase, or restore execution.

Patched files:
- `bench-client/vstl-imaging-tui.py`
- `bench-client/vstl_hw_detect.py`
- `bench-client/vstl_image_capture.py`
- `bench-client/vstl_image_restore.py`
- `bench-client/vstl_lock_audit.py`
- `bench-client/vstl-bench-entry.sh`

Validation:
- Python compile-check passed for all patched Python files after copying into `imaging_server/bench-client`.

Deployment note:
- This package is ready to be copied to the FOG server and rebuilt into the live PXE squashfs/rootfs.
- Back up the live FOG `/opt/vstl-imaging-phase1` and `/var/www/html/vstl-pxe` state before deploying.
