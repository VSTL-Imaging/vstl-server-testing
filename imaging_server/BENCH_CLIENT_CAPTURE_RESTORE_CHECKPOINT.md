# Bench Client Capture/Restore Checkpoint

Date: 2026-05-26

Status: capture/restore naming and progress polish verified locally.

Changes completed in this checkpoint:
- Fixed duplicate capture prompt data by adding `image_old_name` to the capture image plan.
- Verified date-free capture image naming uses OS token format, for example `Win_11_Pro_25H2`.
- Verified old-image rotation names use `_old.img`.
- Verified the exact duplicate image prompt is present before capture starts.
- Verified capture and restore duration display uses `hh hr : mm min : ss sec`.
- Verified capture and restore progress screens use numeric percentages and progress bars.
- Verified restore lookup happens before restore intro/QC, and blocks restore when no exact/fallback image exists.

Files touched:
- `bench-client/vstl_image_capture.py`

Verification performed:
- `python -m py_compile bench-client/vstl_image_capture.py bench-client/vstl_image_restore.py bench-client/vstl-imaging-tui.py`
- Smoke-tested `build_capture_image_plan()` with `Windows 11 Pro` / `25H2`.

Packaged artifacts:
- Current rolling bundle: `D:\Projects\VSTL Server\dist\vstl-imaging-server_current.tar.gz`
- Timestamped bundle: `D:\Projects\VSTL Server\dist\vstl-imaging-server_20260526_134426.tar.gz`
- SHA256: `7F6173FBE7B0B10FE60786F1286027883FFAC6FA7FF473EA1195232B5D4E75DB`

Deployment status:
- Local patch and package are complete.
- Live FOG/PXE squashfs deployment is still pending from this Windows workspace.
- Next deploy should copy the packaged tar to the FOG server, back up the working `/var/www/html/vstl-pxe` and `/opt/vstl-imaging-phase1` state, then rebuild/deploy only the PXE root filesystem/squashfs.

Next checkpoint:
- Deploy the updated `bench-client` into the live PXE squashfs on the FOG server after taking a server-side backup.
- Do not touch the PXE bootloader/initrd chain unless explicitly requested.
