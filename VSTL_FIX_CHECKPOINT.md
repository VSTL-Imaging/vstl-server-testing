# VSTL Bench-Client Fix Checkpoint

Date: 2026-05-26
Scope: Local bench-client code fixes only. PXE boot chain not changed.
Status: Local fixes verified. PXE legacy boot fix deployed to live FOG server over Tailscale on 2026-06-01.

## Verified Local Fixes

- Capture progress UI has partition progress, overall progress, ETA, speed, written bytes, and a percentage-driven bar.
- Restore progress UI mirrors capture progress.
- Duration display uses `hh hr : mm min : ss sec` format for capture, restore, burn, and secure erase result screens.
- Capture filename logic removes date/time and includes OS token through `build_capture_image_plan`.
- Existing-image flow prompts with the requested message, deletes previous `_old`, renames current image to `_old`, then captures the new image.
- Restore checks local golden-copy availability before system details/QC flow and shows backup-not-found when no exact/fallback image exists.
- NFS backend-settings warning is removed; code falls back quietly to local FOG `/images/dev`.
- GPU detection includes discrete VRAM capacity fallback mapping, including GeForce MX550.
- MDM detection uses stronger Intune/Azure evidence paths and registry evidence, avoiding default Windows task-folder false positives.
- OS detection reads Windows SOFTWARE hive fields and maps Windows 11 build/version values.

## Compile Check

Passed:

```powershell
python -m py_compile `
  imaging_server\bench-client\vstl-imaging-tui.py `
  imaging_server\bench-client\vstl_image_capture.py `
  imaging_server\bench-client\vstl_image_restore.py `
  imaging_server\bench-client\vstl_hw_detect.py `
  imaging_server\bench-client\vstl_lock_audit.py
```

## Remaining Before Live Use

1. Back up the current working FOG/PXE setup.
2. Deploy only the updated bench-client/rootfs files into the live PXE squashfs.
3. Move/delete old captured images from `/images/dev` because the image naming logic changed.
4. Create/update the full setup tar backup after deployment.
5. PXE boot one bench and verify:
   - OS/version is no longer `Windows Unknown`.
   - NFS warning is gone.
   - GPU screen shows VRAM capacity where known.
   - Restore stops before QC when no image exists.
   - Capture and restore progress UI updates correctly.

## PXE Legacy Laptop Fix Added 2026-06-01

- `03_dnsmasq_proxydhcp.conf` now publishes architecture-tagged
  `pxe-service` entries as well as `dhcp-boot` entries. This restores
  proxyDHCP compatibility for older Dell Latitude 5490/5470 and HP ProBook
  640 G1/G2 IPv4 PXE firmware without sending UEFI clients a BIOS loader.
- 64-bit UEFI now defaults to FOG's stock `snponly.efi`, with installer
  fallback to `ipxe.efi` or `.env` override via `VSTL_UEFI_BOOTFILE`.
- `06_setup_pxe_netboot.sh` has the safer merged-initrd/named-iPXE handoff,
  `autoexec.ipxe`, and diagnostic boot variants from the capture-fix branch.
- `tools/deploy_pxe_legacy_bootfix.sh` can be run on the FOG server to back up
  current PXE state, install the updated PXE scripts/config, restart dnsmasq,
  and regenerate the PXE boot assets.
- Live deploy target: `vstl@100.88.250.63` over Tailscale, serving bench LAN
  PXE assets as `10.255.254.75`.
- The live server root filesystem was full before deployment. Removed only old
  dated VSTL scratch artifacts from `/tmp`, freeing about 8.8 GB.
- `dnsmasq` and Apache verified active after deployment.
- Live `/etc/dnsmasq.d/vstl-imaging.conf` now serves BIOS clients
  `undionly.kpxe` and 64-bit UEFI clients `snponly.efi`.
- Live `/var/www/html/vstl-pxe/boot.ipxe` now uses `initrd=initrd.img` plus
  `initrd --name initrd.img ... initrd.img`.

## PXE Panic Rollback 2026-06-01

- Dell Latitude 5530 BIOS 1.33.0 still panicked on the merged gzip/named-initrd
  path with `Initramfs unpacking failed: invalid magic at start of compressed
  archive`.
- Restored the last known working production boot path from
  `/opt/vstl-backups/pxe-legacy-bootfix-20260601_165918/vstl-pxe-before.tar.gz`.
- Live `boot.ipxe` now uses the classic raw-cpio handoff:
  `initrd http://10.255.254.75/vstl-pxe/initrd-vstl-fixed.cpio`.
- Live raw cpio artifact verified:
  `initrd-vstl-fixed.cpio` SHA256 prefix `5433d3c7a53b`.
- Kept the new dnsmasq legacy PXE compatibility config active.
- Updated `/opt/vstl-imaging-phase1/06_setup_pxe_netboot.sh` and the local
  bundle so future PXE rebuilds default to `VSTL_PXE_BOOT_STYLE=classic-raw-cpio`.

## Full Bench-Client Deploy 2026-06-01

- Rebuilt and deployed `dist/vstl-imaging-server_current.tar.gz` to the live
  FOG server so QC, burn/stress, secure erase, capture, restore, hardware
  detect, lock audit, entrypoint, and TUI files are all present.
- Added missing `bench-client/vstl_secure_erase.py` back into the local bundle.
- Updated `tools/deploy_bench_client_live.sh` so it copies all runtime modules,
  supports the server's `build/active-rootfs-patch` rootfs layout, and does not
  move existing `/images/dev` captured images unless `ARCHIVE_OLD_IMAGES=1`.
- Updated `04_build_live_iso_clonezilla.sh` so future ISO builds bake secure
  erase, capture, and restore modules too.
- Live hash verification passed for all runtime files in:
  `/opt/vstl-imaging-phase1/bench-client`,
  `/opt/vstl-imaging-phase1/build/active-rootfs-patch/opt/vstl`, and
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live PXE boot chain remained on the restored raw-cpio path.
- New live squashfs SHA256:
  `2df383ffe7604623ce46dcbb2d13e8c527de16966de7d4bbaa102e3bfe60b45a`.
- Final local package SHA256:
  `b5f6ca30fbaeb840288db09598f54ee4006c93f4f3d2a5cd47f1da9f895124ae`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_full_bench_20260601.tar.gz`.
- Server root filesystem was later expanded to the full disk: about `3.5T`
  total, `3.3T` free, `3%` used.

## Old-Generation PXE Compatibility Follow-Up 2026-06-01

- Server logs showed two different old-laptop behaviours:
  - MAC `cc:48:3a:43:87:ed` did receive proxyDHCP, TFTP `snponly.efi`, iPXE,
    kernel/initrd, and the full HTTP `filesystem.squashfs`.
  - MAC `ce:e8:70:6e:52:0b` sent plain DHCP only, with no `PXEClient` vendor
    class and no TFTP/HTTP fetches, matching a firmware fallback to Windows or
    a non-PXE DHCP request.
- Added low-risk dnsmasq compatibility options:
  - `dhcp-no-override` keeps boot server/filename in simple BOOTP fields for
    older or broken PXE clients.
  - `dhcp-pxe-vendor=PXEClient,HW-Client` accepts the alternate PXE vendor
    string documented by dnsmasq for vendor-customized firmware.
- Deployed the dnsmasq-only fix to the live server and restarted dnsmasq.
  The live boot chain stayed on the raw-cpio panic-safe path.
- Current local package SHA256:
  `6048436df1b23cb41143d34fea77c89339b03db4270bfd15f7f049da1bf5c970`.

## QC Camera, Keyboard, And Touch Display Update 2026-06-01

- Camera QC now tries a real visible framebuffer preview first:
  `v4l2-ctl` captures YUYV frames and `vstl-imaging-tui.py` paints them
  directly to `/dev/fb0`. Fallback remains repeated `fswebcam`/`fbi`, then
  stream-only evidence if no renderer works.
- Camera operator prompt now requires a live moving picture. LED-only behavior
  should be marked FAIL.
- Keyboard QC no longer shows or requires Fn modifier, Fn-combo, or media/Fn
  hotkey rows. It keeps standard F-keys, alpha/digits, numpad, modifiers,
  ESC, and navigation keys.
- QC order now includes touchscreen auto-detection immediately after Display
  for both L1 and L2.
- Non-touch displays are auto-detected and skipped as N/A before moving to the
  next test.
- Touch displays open a full-screen blue/green framebuffer grid. Dragging over
  cells turns them green; filling all cells auto-PASSes and advances. Pressing
  ESC twice within 1.5 seconds marks FAIL and advances.
- Local checks passed:
  `python -m py_compile imaging_server\bench-client\vstl-imaging-tui.py imaging_server\bench-client\vstl_qc_tests.py`
  plus focused assertions for removed keyboard rows and new camera/touch code.
- Deployed to live server `100.88.250.63` and rebuilt only
  `/var/www/html/vstl-pxe/filesystem.squashfs`; PXE boot chain remained on the
  restored raw-cpio path.
- Live file hashes verified in `/opt/vstl-imaging-phase1/bench-client`,
  `/opt/vstl-imaging-phase1/build/active-rootfs-patch/opt/vstl`, and inside
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- New live squashfs SHA256:
  `314bc28229304be1067d13b74c4858934d7a7c6c052aeb80bf286548c9ce3f28`.
- Final local package SHA256:
  `b812f37b8ac6d8e8d033b875e59ad8b81d7a21fc26bd19306fb419b57eaf4f85`.
- Nested phase1 package SHA256:
  `663ed42f61d4bd7433389d480c838286b62390b059907b489a5ce6c9e5a66592`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_qc_touch_camera_20260601.tar.gz`.
- Server capacity after deploy: about `3.5T` total, `3.2T` free, `4%` used.

## Windows 11 Pro Capture Naming Fix 2026-06-01

- Fixed offline Windows edition normalization in
  `bench-client/vstl_image_capture.py`.
- Root cause: raw SOFTWARE hive string scanning could find an unrelated
  Enterprise value near `EditionID` and override a real ProductName of
  Windows Pro.
- New behavior trusts `ProductName` first. If ProductName says Pro/Home/
  Enterprise/Education, loose `EditionID` hits no longer override it.
- Windows 10 ProductName with build `>=22000` is still normalized to
  Windows 11, preserving the ProductName edition. For the reported Latitude
  5530, the capture name now resolves to:
  `DELL_INC__LATITUDE_5530_0B06_12TH_GEN_INTEL_CORE_I7-1265U_Win_11_Pro_25H2`.
- Added regression test:
  `imaging_server/tests/test_windows_os_detection.py`.
- Local checks passed:
  `python -m py_compile imaging_server\bench-client\vstl_image_capture.py imaging_server\bench-client\vstl_image_restore.py imaging_server\bench-client\vstl-imaging-tui.py`
  and
  `python -m unittest discover -s imaging_server\tests -p test_windows_os_detection.py`.
- Removed the incorrect previously captured server image directories:
  `/images/dev/DELL_INC__LATITUDE_5530_0B06_12TH_GEN_INTEL_CORE_I7-1265U_Win_10_Enterprise_25h2`
  and its `_old` rotation. Removed size was about `16.3G`.
- Deletion manifest saved on server:
  `/opt/vstl-backups/removed_wrong_win11pro_capture_20260601.txt`.
- Deployed to live server `100.88.250.63` and rebuilt only the PXE
  `filesystem.squashfs`; PXE boot chain remained on the restored raw-cpio
  path.
- New live squashfs SHA256:
  `722fd5f873ee0fae9a5a05370bd6af75e88f638bbe47c77f44dfeac406622aad`.
- Final local package SHA256:
  `c29b646c3505ee0eb1a7d10b5943d21ffbeed528e56cf311f161c33de9a9aed3`.
- Nested phase1 package SHA256:
  `dcc7f3403ed5ca7fd8a3b74928ba2141cfddc1f2a103ebec7141b86104ba1676`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_win11pro_osfix_20260601.tar.gz`.
- Server capacity after cleanup and deploy: about `3.5T` total, `3.3T` free,
  `3%` used.

## Dell Latitude 5490 UEFI PXE F1 Retry Fix 2026-06-01

- Reported symptom on Dell Latitude 5490: F12 UEFI IPv4 showed
  `Start PXE over IPv4`, then returned to Dell firmware with
  `Press F1 key to retry boot` before any iPXE/VSTL screen appeared.
- Server logs for MAC `10:65:30:66:c1:3b` at `2026-06-01 20:00` showed
  UEFI arch `00007` receiving proxyDHCP with bootfile `snponly.efi`, but no
  Linux handoff followed.
- Root cause found on live server: `/tftpboot/snponly.efi`,
  `/tftpboot/snp.efi`, and `/tftpboot/ipxe.efi` were identical embedded
  `snponly.efi` binaries, so switching only the filename would not change the
  loader actually being executed.
- Updated `03_dnsmasq_proxydhcp.conf` and `02_install_dnsmasq.sh` so 64-bit
  UEFI defaults to full iPXE, and on this server pins UEFI boot to
  `/tftpboot/vstl-clean-ipxe.efi`.
- Added iPXE second-pass DHCP handling:
  `dhcp-match=set:ipxe,175` and
  `dhcp-boot=tag:ipxe,http://10.255.254.75/vstl-pxe/boot.ipxe`, preventing a
  full iPXE loader from being handed the EFI binary again.
- Local/current package rebuilt:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `e32d20333ac336d4b0e9252de0466c52704d5b275493690b228c24aada2a739e`.
- Nested phase1 package SHA256:
  `585e1041f6eba91ede1f8665413342352bf95dfe610541de3b0e7fd49f2aa0eb`.
- Deployed only dnsmasq/PXE-offer files to live server. Did not rebuild
  `filesystem.squashfs` and did not regenerate `boot.ipxe`.
- Live dnsmasq now offers `vstl-clean-ipxe.efi` for UEFI arch `00007` and
  `00009`.
- Live loader hashes:
  - `/tftpboot/vstl-clean-ipxe.efi`
    `67c7f1f8e062968209ca055283ca782f21faf6a18f55dd19848601bbaf8ed7aa`
  - `/tftpboot/snponly.efi` and `/tftpboot/ipxe.efi`
    `1026c8e0b463a9e5934b93264e40e67fe1bdb718cabf389cdf04426634fe7f2e`
- Boot chain remained unchanged:
  - `/var/www/html/vstl-pxe/boot.ipxe`
    `58517d1d9ad71a0700072e43ff2395b57d64c12d7d1211bdebb2a6885c81e06f`
  - `/var/www/html/vstl-pxe/initrd-vstl-fixed.cpio`
    `5433d3c7a53ba52bbaa4ef38a3c5aecc0da30fbf4f7fd564461b832420acdcef`
  - `/var/www/html/vstl-pxe/filesystem.squashfs`
    `722fd5f873ee0fae9a5a05370bd6af75e88f638bbe47c77f44dfeac406622aad`
- Live backup dir:
  `/opt/vstl-backups/pxe-uefi5490-loaderfix-20260601_203611`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_uefi5490_pxefix_20260601.tar.gz`.
- Verified `dnsmasq --test`, `dnsmasq`, `tftpd-hpa`, and `apache2` active.

## Exact Windows Edition Registry Fix 2026-06-01

- User confirmed capture intro still showed
  `Windows 10 Enterprise 25H2` for a laptop whose Windows Settings showed
  `Windows 11 Pro`.
- Root cause: even after the first normalization fix, the detector still used
  whole-hive `strings` as the primary source. That can find `ProductName` from
  unrelated SOFTWARE hive areas and return Enterprise.
- Updated `bench-client/vstl_image_capture.py` to use `reged` from `chntpw`
  first and export the exact key:
  `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion`.
- For exact-key values, `EditionID=Professional` is authoritative over a
  conflicting `ProductName=Windows 10 Enterprise`, and build `>=22000`
  normalizes the base OS to Windows 11.
- Whole-hive `strings` remains only as a last fallback if `reged` is
  unavailable.
- Added regression coverage for exact-key conflict:
  `ProductName=Windows 10 Enterprise`, `EditionID=Professional`,
  `CurrentBuild=26200` now resolves to `Windows 11 Pro`.
- Local checks passed:
  `python -m py_compile imaging_server\bench-client\vstl_image_capture.py imaging_server\bench-client\vstl_image_restore.py imaging_server\bench-client\vstl-imaging-tui.py`
  and
  `python -m unittest discover -s imaging_server\tests -p test_windows_os_detection.py`.
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `942e7fa198dbe94d4e56bc183c1eb314097c59f2001ec70d753c96bfcd2cf8ac`.
- Nested phase1 package SHA256:
  `f735a56cd43c67c9debd300560f1dfb21e7644f07334a7596ccfab9b27975cc6`.
- Deployed updated bench-client to live server and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live `vstl_image_capture.py` hash verified in install tree, rootfs patch,
  and inside squashfs:
  `13f65341f2269e3ec087c2b6b377efe15afa4c58376d29f03d42a226ad8041ac`.
- Live squashfs SHA256:
  `1b138407d5364ed0c2f2d7d6a66017787630854f5831b4da706751e83f800d77`.
- On-server assertion now produces:
  `DELL_INC__LATITUDE_5530_0B06_12TH_GEN_INTEL_CORE_I7-1265U_Win_11_Pro_25H2`.
- Rechecked `/images/dev`; no `Enterprise` capture directories or metadata
  hits remain.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_exact_win_edition_20260601.tar.gz`.

## Product ID Windows Pro Override 2026-06-01

- User confirmed a fresh PXE boot still showed
  `Windows 10 Enterprise 25H2` on the capture intro, while Windows Settings
  showed `Windows 11 Pro` and Product ID `00355-62989-47148-AAOEM`.
- Updated `bench-client/vstl_image_capture.py` again to parse hex/UTF-16
  `reged` exports and to use the narrow OEM Product ID pattern
  `00355-xxxxx-xxxxx-AAOEM` as a Pro override. This prevents a bad Enterprise
  ProductName/EditionID value from winning on this observed Dell Pro OEM image.
- Added regression coverage for:
  - Enterprise ProductName + Enterprise EditionID + Product ID
    `00355-62989-47148-AAOEM` => `Windows 11 Pro`.
  - Hex/UTF-16 `.reg` export values for `ProductName` and `ProductId`.
- Local checks passed:
  `python -m py_compile imaging_server\bench-client\vstl_image_capture.py imaging_server\bench-client\vstl_image_restore.py imaging_server\bench-client\vstl-imaging-tui.py`
  and
  `python -m unittest discover -s imaging_server\tests -p test_windows_os_detection.py`.
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `f2789f9b8e743758e2500170402ec031e213436eda83f05fd24a0c464392f1c6`.
- Nested phase1 package SHA256:
  `c1ef015ea0103be0272fdf4c78111c6fdae844b8e98848b6a06409acc93bdd41`.
- Deployed updated bench-client to live server and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live `vstl_image_capture.py` hash verified in install tree, rootfs patch,
  and inside squashfs:
  `04e8a66d8091b6bf72fd49c098f367dd87652ac5ff4e9f9564f287bb79c2aafc`.
- Live squashfs SHA256:
  `c6d2ddcd74cf02e6c133daaf7c7ab8d4893c812224e46f2f684314edfea63868`.
- On-server assertion now produces:
  `DELL_INC__LATITUDE_5530_0B06_12TH_GEN_INTEL_CORE_I7-1265U_Win_11_Pro_25H2.img`.
- Rechecked `/images/dev`; no `Enterprise` capture directories remain.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_productid_osfix_20260601.tar.gz`.

## Reged Exit-Code Windows Pro Fix 2026-06-01

- User confirmed the Product ID fix still showed `Windows 10 Enterprise`.
- Connected to the laptop through PiKVM `100.126.230.48` and ran the detector
  directly on the booted laptop.
- Evidence from the laptop:
  - The live client was running capture script hash
    `04e8a66d8091b6bf72fd49c098f367dd87652ac5ff4e9f9564f287bb79c2aafc`.
  - The normalizer itself returned `Windows 11 Pro`.
  - `reged` exported the correct CurrentVersion values but exited with
    signal/rc `134`, so the previous code discarded the good export and fell
    back to loose `strings`.
  - The valid export contained:
    `ProductName=Windows 10 Pro`, `EditionID=Professional`,
    `ProductId=00355-62989-47148-AAOEM`, `CurrentBuild=26200`,
    `DisplayVersion=25H2`.
- Updated `bench-client/vstl_image_capture.py` so `reged` output is accepted
  if the export file exists and parses successfully, even when `reged` exits
  non-zero after writing the file.
- Updated the `.reg` parser to read only the exact
  `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion` section, so later
  subkey values cannot overwrite the real OS values.
- Added regression coverage for the exact section parser and the observed
  CurrentVersion values.
- Local checks passed:
  `python -m py_compile imaging_server\bench-client\vstl_image_capture.py imaging_server\bench-client\vstl_image_restore.py imaging_server\bench-client\vstl-imaging-tui.py`
  and
  `python -m unittest discover -s imaging_server\tests -p test_windows_os_detection.py`.
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `bf76215408dce0e92e5d93ec076a63030cc24908944b7c6cd05b9bcf55a69b50`.
- Nested phase1 package SHA256:
  `661dbe710b0addbf46bcd0c9966ebd8a0ecabc2aa33a2a6c7c303d7b9d35d836`.
- Deployed updated bench-client to live server and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live `vstl_image_capture.py` hash verified in install tree and inside
  squashfs:
  `b08d2868677de3e22789f336def7ed5f406eac9aa0b99e081ad481eaf1ae1ce4`.
- Live squashfs SHA256:
  `6ceb85dad7aec0ac7d066dc12958147ea801003d819a03d98c13ac7c33658f3f`.
- Verified through KVM on the actual Latitude 5530 after PXE reboot:
  capture screen now shows `Windows 11 Pro 25H2 (26200)` and image name
  `DELL_INC__LATITUDE_5530_0B06_12TH_GEN_INTEL_CORE_I7-1265U_Win_11_Pro_25H2.img`.
- Rechecked `/images/dev`; no `Enterprise` capture directories remain.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_reged_rc_osfix_20260601.tar.gz`.

## Storage Capacity Decimal GB Fix 2026-06-01

- User requested storage capacity to show the marketed drive size, e.g.
  `256 GB`, instead of Linux binary GiB shown as `238.5 GB`.
- Root cause: `vstl_secure_erase.detect_primary_drive()` divided disk bytes
  by `1024**3`, which is GiB, but the UI label says GB.
- Updated `bench-client/vstl_secure_erase.py` to calculate physical drive
  capacity using decimal GB (`1,000,000,000` bytes) and round marketed drive
  sizes. Example: `256,060,514,304` bytes now displays as `256 GB`.
- Updated `bench-client/vstl_hw_detect.py` storage screen detection to use
  `lsblk -b` byte output and format vendor-style decimal labels, so the
  hardware storage screen and capture/erase screens match.
- Added regression tests in
  `imaging_server/tests/test_storage_capacity_units.py`.
- Local checks passed:
  `python -m py_compile imaging_server\bench-client\vstl_secure_erase.py imaging_server\bench-client\vstl_hw_detect.py imaging_server\bench-client\vstl-imaging-tui.py imaging_server\bench-client\vstl_image_capture.py`
  plus storage-capacity and Windows-edition unit tests.
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `9159420dab823764c0a238dfe785bc37d3a33cec6b197db31c3bd10b2dad767b`.
- Nested phase1 package SHA256:
  `d19732ba272d9788717a24f4a424540fca54080ae89d36e5fd736179c1e2c4ef`.
- Deployed updated bench-client to live server and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live file hashes verified in install tree and inside squashfs:
  - `vstl_secure_erase.py`
    `70904a53b35594bcd6c2fcac82bee65cf245630a7a1572b48e3dcdda424d9d3f`
  - `vstl_hw_detect.py`
    `47161f53cee9fef6cc41872c55d9f35d912c7a2360c5884a3f2bf323fed3b412`
- Live squashfs SHA256:
  `aaf1f345cd56ebdfe79e4e985338c0d7f07698cfad2d8aee5b6a5bf0bd87fee6`.
- Server-side embedded assertion passed:
  `256_060_514_304` bytes => `256` / `256 GB`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_storage_decimal_fix_20260601.tar.gz`.

## QC Display Resolution Fix 2026-06-01

- User requested QC Display Type screen to also show the detected display
  resolution, e.g. `1920 x 1080 (FHD)`.
- Added framebuffer/DRM resolution detection in
  `bench-client/vstl_qc_tests.py`:
  - Reads `/sys/class/graphics/fb0/virtual_size` first.
  - Falls back to `/sys/class/graphics/fb0/modes`.
  - Falls back to connected `/sys/class/drm/*/modes`.
  - Formats common laptop panels with friendly labels such as `FHD`,
    `WUXGA`, `QHD`, and `UHD 4K`.
- Updated active QC touch/display-type screen in
  `bench-client/vstl-imaging-tui.py` to show:
  `Display resolution: <width> x <height> (<class>)`
  directly below the auto-detected display type for both touch and
  non-touch displays.
- Added regression tests in
  `imaging_server/tests/test_display_resolution_label.py`.
- Local checks passed:
  `python -m py_compile imaging_server\bench-client\vstl_qc_tests.py imaging_server\bench-client\vstl-imaging-tui.py`
  and `python -m unittest discover imaging_server\tests`.
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `350ba73a98963390c5810a8254910e0a511bb526e7ad4fef174702a34033a6d9`.
- Nested phase1 package SHA256:
  `70df9588ec506cb939e0463939ad07b4881ab640f91bde637ddff1312ee8c47a`.
- Deployed updated bench-client to live server and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live file hashes verified in install tree and inside squashfs:
  - `vstl_qc_tests.py`
    `6a22ecbc9a01ed4d14db4647b017198e6704438ac0e305b97b1dfdffed3cea68`
  - `vstl-imaging-tui.py`
    `955f9aba065cc049f875e8a9721ab885ba2d2cf994e1f97695ab6924015b58db`
- Live squashfs SHA256:
  `64c5d0aa26448282b205ec433ee89c2f1db57f142e8816089390775dfa7f6a7c`.
- Server-side helper assertion passed:
  `1920 x 1080 (FHD)`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_display_resolution_fix_20260601.tar.gz`.

## QC Audio + Keyboard Numpad Fix 2026-06-01

- User reported Speaker QC shows 100% volume on screen but actual output is
  low, and requested the same loud playback behavior for Microphone QC.
- Updated `bench-client/vstl_qc_tests.py` audio handling:
  - New `_alsa_prepare_audio()` touches default plus each ALSA card.
  - Sets common playback paths to 100% and unmuted:
    `Master`, `Speaker`, `Headphone`, `PCM`, `Front`, `Line Out`, `DAC`,
    `Digital`, plus matching enumerated mixer controls.
  - Sets capture/mic paths to 100% and capture-enabled:
    `Capture`, `Mic`, `Internal Mic`, `Mic Boost`, `Internal Mic Boost`,
    plus matching enumerated mixer controls.
  - Disables `Auto-Mute Mode` where supported.
  - Speaker test now plays a generated high-amplitude stereo WAV through
    `aplay`, with `speaker-test` as fallback.
  - Microphone test records, normalizes quiet 16-bit PCM captures upward, and
    replays through `aplay` after output is forced to 100%.
- User reported laptop without numpad still shows numpad in Keyboard QC.
- Updated `bench-client/vstl-imaging-tui.py` keyboard map:
  - Numpad row is only shown when DMI model policy or an external/full-size
    keyboard device indicates a physical numpad.
  - No-numpad laptops hide the row and reduce the required key coverage count.
  - Prompt now says `All displayed keys` instead of always mentioning numpad.
- Added regression tests in
  `imaging_server/tests/test_qc_audio_keyboard_helpers.py`.
- Local checks passed:
  `python -m py_compile imaging_server\bench-client\vstl_qc_tests.py imaging_server\bench-client\vstl-imaging-tui.py`
  and `python -m unittest discover imaging_server\tests`.
- Local `bash -n` was blocked by local WSL needing an update, so the installer
  script syntax check was run on the live Ubuntu server and passed.
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `c6a6c8b7ac146a518dc97954cba8a7408340352f9102afc4d07b0c34eeacaaf7`.
- Nested phase1 package SHA256:
  `fc71afced2ac9860d812553cdbe5e5f99b96ba1dc1e01038a1f4c9d72ae541ab`.
- Deployed updated bench-client with targeted deploy and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live file hashes verified in install tree and inside squashfs:
  - `vstl_qc_tests.py`
    `aa85c92a3f8842af086c74846d945286fbd1f4315781036f492d80603768948d`
  - `vstl-imaging-tui.py`
    `e637f11231552bcff643129a2b127a99bb767e827d0c0380c291d263770da005`
- Live squashfs SHA256:
  `8a6e38ab8940d2f49525f30673118499beb19cae4bf961d157c981b2f2c061c9`.
- Server helper assertions passed:
  - ThinkPad T14 / Latitude 5490 / HP ProBook 640 policy hides numpad.
  - ThinkPad T15 / HP ProBook 650 / external USB keyboard policy shows numpad.
  - Generated speaker WAV has valid stereo 16-bit PCM format.
- A full deploy attempt exposed `dnsmasq` had failed after reboot because
  systemd started it before `eno1` was available. Live server was fixed with
  `/etc/systemd/system/dnsmasq.service.d/vstl-wait-for-interface.conf`, and
  `02_install_dnsmasq.sh` now installs the same wait guard for future installs.
- Live `dnsmasq` status after fix: `active`.
- Targeted deploy backup:
  `/opt/vstl-backups/bench-client-audio-keyboard-targeted-20260601_225607`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_audio_keyboard_fix_20260601.tar.gz`.

## Camera Live Video Stream Fix 2026-06-02

- User reported Camera QC still looks like frame-by-frame pictures and asked
  for a real full live camera video feed.
- Root cause: active camera fallback called `v4l2-ctl` once per frame,
  wrote each frame to a temp file, then blitted that single frame to `/dev/fb0`.
  That made the preview behave like a slideshow.
- Updated `bench-client/vstl-imaging-tui.py`:
  - Added `_camera_fb_v4l2_stream_preview()`.
  - Uses one continuous `v4l2-ctl --stream-mmap` session.
  - Streams raw YUYV frames into a named FIFO created with `os.mkfifo()`.
  - Reads each frame from the FIFO and blits it directly to `/dev/fb0`.
  - Runs this continuous stream path before `fswebcam`/`fbi`.
  - Keeps the old repeated single-frame logic only as a degraded last-resort
    fallback, with evidence text clearly saying `camera preview degraded`.
- Added regression tests in
  `imaging_server/tests/test_camera_live_stream_preview.py`.
- Local checks passed:
  in-memory syntax compile for `vstl-imaging-tui.py` and `vstl_qc_tests.py`,
  and `python -m unittest discover imaging_server\tests` (`19` tests).
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `8f46aedab05a5831de49e5e5ba9ebb0b761cca29858b0e6bda89acd8c71ac9a0`.
- Nested phase1 package SHA256:
  `82f6d65b32b9bfc782761a9ad9406bfa2aaabac86ce7fc31179a1b37cc8cb093`.
- Deployed updated bench-client with targeted deploy and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live file hashes verified in install tree and inside squashfs:
  - `vstl-imaging-tui.py`
    `69d27022f4d0415598ac453597b826f5e88af175029b2a4117029296290bddb4`
  - `vstl_qc_tests.py`
    `aa85c92a3f8842af086c74846d945286fbd1f4315781036f492d80603768948d`
- Live squashfs SHA256:
  `bf5849a4e82e9963325811fa0f1016c8f47ce9b0c335abc80dd67c7bfec8804a`.
- Live services after deploy:
  `dnsmasq=active`, `apache2=active`, `tftpd-hpa=active`.
- Targeted deploy backup:
  `/opt/vstl-backups/bench-client-camera-live-targeted-20260602_105500`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_camera_live_fix_20260602.tar.gz`.

## Clean TUI Startup Screen Fix 2026-06-02

- User reported that after IPv4 PXE boot, the laptop first shows a partial
  L1/L2 technician menu mixed with Clonezilla live boot messages, then a few
  seconds later redraws the normal purple-header technician screen.
- Root cause: `vstl-bench-entry.sh` can start the Python curses TUI while
  Clonezilla/live-config is still printing final console setup lines such as
  `Finished Clonezilla live env preparing...`.
- Updated `bench-client/vstl-bench-entry.sh`:
  - Added `wait_for_console_quiet()`.
  - Default waits `5` seconds before launching the TUI.
  - Briefly checks system boot state when `systemctl` is available.
  - Clears `/dev/tty1` screen and scrollback immediately before Python curses
    starts.
  - Delay is configurable with `VSTL_TUI_START_DELAY_SEC`.
- Added regression test in
  `imaging_server/tests/test_bench_entry_clean_tui_start.py`.
- Local checks passed:
  in-memory syntax compile for Python files and
  `python -m unittest discover imaging_server\tests` (`20` tests).
- Server-side shell syntax check passed:
  `bash -n /opt/vstl-imaging-phase1/bench-client/vstl-bench-entry.sh`.
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `6ecb9b7d396774dbd34e3930b57938a22dbbcc89495552ba9ec5f370e8107544`.
- Nested phase1 package SHA256:
  `7589620903f515f860990184976264fa158e4a56bd000423231ef591a8cd9be2`.
- Deployed updated bench-client with targeted deploy and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live file hashes verified in install tree and inside squashfs:
  - `vstl-bench-entry.sh`
    `9b68e128c2a92c159765ab1c61f22b989c0458c362d6a668bb9302634ad3e808`
  - `vstl-imaging-tui.py`
    `69d27022f4d0415598ac453597b826f5e88af175029b2a4117029296290bddb4`
- Live squashfs SHA256:
  `0d7535af05da016352a474ed27d538c55416d85f40c174c7d23024efa5e0ac86`.
- Live services after deploy:
  `dnsmasq=active`, `apache2=active`, `tftpd-hpa=active`.
- Targeted deploy backup:
  `/opt/vstl-backups/bench-client-clean-tui-start-targeted-20260602_112155`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_clean_tui_start_fix_20260602.tar.gz`.

## TUI Full Repaint / Stale Text Fix 2026-06-02

- User reported that after the previous cleanup, text from earlier screens could
  remain visible on the current screen, especially visible through PiKVM during
  QC screens such as CPU.
- Root cause: `stdscr.erase()` alone did not always fully invalidate every
  terminal cell in the framebuffer/KVM path, so old curses cells could remain
  until overwritten by new text.
- Updated `bench-client/vstl-imaging-tui.py`:
  - Added `_force_full_repaint(stdscr)`.
  - The helper erases the curses window, clears each row to end-of-line, writes
    blank cells across the screen, then requests `stdscr.clearok(True)` and
    `stdscr.redrawwin()`.
  - `draw_header()` now calls `_force_full_repaint(stdscr)` before drawing the
    purple header and page contents, so every normal TUI screen starts from a
    clean black canvas.
- Added regression test in
  `imaging_server/tests/test_tui_full_repaint.py`.
- Local checks passed:
  in-memory syntax compile for Python files and
  `python -m unittest discover imaging_server\tests` (`21` tests).
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `d217ba093d31f924376738cbbbba30ae162e1c4cde3124f097c116e6a7a0ed5d`.
- Nested phase1 package SHA256:
  `c4db6b59fbc7923920233ad2935f892ed6324fbe300165a721b1e37f2026de81`.
- Deployed updated bench-client with targeted deploy and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live file hashes verified in install tree and inside squashfs:
  - `vstl-imaging-tui.py`
    `84986eb0ede27ec935f67e221412fc1727eebbbd5293f90d4cbd747693fe04f4`
  - `vstl-bench-entry.sh`
    `9b68e128c2a92c159765ab1c61f22b989c0458c362d6a668bb9302634ad3e808`
- Live squashfs SHA256:
  `8cc5671ea7202c5e0662e8cace1e5fc4e132d08aadd2439c1a6589c666ee529f`.
- Server-side checks passed:
  - Python syntax compile for `vstl-imaging-tui.py` and `vstl_qc_tests.py`.
  - Squashfs contains `_force_full_repaint`, `stdscr.clearok(True)`, and
    `stdscr.redrawwin()`.
  - `dnsmasq=active`, `apache2=active`, `tftpd-hpa=active`.
- Server disk after deploy:
  `/` is `3.5T` total, `136G` used, `3.2T` available, `5%` used.
- Targeted deploy backup:
  `/opt/vstl-backups/bench-client-tui-full-repaint-targeted-20260602_113931`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_tui_full_repaint_fix_20260602.tar.gz`.

## TUI Transition Repaint Regression Fix 2026-06-02

- User reported that the previous full repaint fix made the TUI behave
  incorrectly:
  - Menus/prompts appeared to jump back to the L1/L2 technician screen.
  - Selecting QC could fall through to Restore due to stale/buffered keys or
    redraw timing.
  - QC screens blinked/flickered on every test transition.
- Root cause: `_force_full_repaint()` was called from shared `draw_header()`,
  so every normal redraw loop rewrote the whole terminal and requested a hard
  curses clear/redraw. That was too heavy for the PiKVM/framebuffer path and
  for interactive prompt loops.
- Updated `bench-client/vstl-imaging-tui.py`:
  - Removed `_force_full_repaint()`.
  - Removed `stdscr.clearok(True)` and `stdscr.redrawwin()` from the hot path.
  - Added `_prepare_screen_transition(stdscr, screen_key)`.
  - The new helper runs only when the logical screen key/subtitle changes.
  - It uses `stdscr.touchwin()` to prevent stale cells and `curses.flushinp()`
    to discard repeated/buffered keys between screens.
  - Normal redraw loops now keep their existing lightweight `stdscr.erase()`
    behavior, avoiding blinking/flicker.
- Updated regression test in
  `imaging_server/tests/test_tui_full_repaint.py`.
- Local checks passed:
  in-memory syntax compile for Python files and
  `python -m unittest discover imaging_server\tests` (`21` tests).
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `4aedcb554f61c9ce588b43f4dd564a3f56d83e67be5286cbb248ee03a6f284fe`.
- Nested phase1 package SHA256:
  `1d663ac2d659b1cf6a27218cf7663d7db3cc0e14ecd01a072c2b848be668c3ce`.
- Deployed updated bench-client with targeted deploy and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live file hashes verified in install tree and inside squashfs:
  - `vstl-imaging-tui.py`
    `d8bbb0a72eb8f6a168ceaa882749c27781327082a76bc50013fb4a200ab2f64f`
  - `vstl-bench-entry.sh`
    `9b68e128c2a92c159765ab1c61f22b989c0458c362d6a668bb9302634ad3e808`
- Live squashfs SHA256:
  `f86cebf96a31bd969a5e18cdc09a5ec765751bd71e404ff853d38c316bf6df58`.
- Server-side checks passed:
  - Python syntax compile for `vstl-imaging-tui.py` and `vstl_qc_tests.py`.
  - Squashfs contains `_prepare_screen_transition`, `stdscr.touchwin()`, and
    `curses.flushinp()`.
  - Squashfs no longer contains `_force_full_repaint`, `stdscr.clearok(True)`,
    or `stdscr.redrawwin()`.
  - `dnsmasq=active`, `apache2=active`, `tftpd-hpa=active`.
- Server disk after deploy:
  `/` is `3.5T` total, `137G` used, `3.2T` available, `5%` used.
- Targeted deploy backup:
  `/opt/vstl-backups/bench-client-tui-transition-targeted-20260602_120312`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_tui_transition_fix_20260602.tar.gz`.

## TUI Physical Console Clear Fix 2026-06-02

- User reported stale text still remained after the transition-only repaint fix:
  - BIOS manual-confirm text remained visible during Keyboard QC.
  - BIOS prompt text remained visible on SKU/hardware detection screens.
- Root cause: `stdscr.touchwin()` was not enough for the Linux
  framebuffer/PiKVM console. It marked curses cells dirty, but did not force
  the physical console to erase old cells that curses believed were already
  blank.
- Updated `bench-client/vstl-imaging-tui.py`:
  - Kept screen-change-only behavior so live loops do not continuously blink.
  - `_prepare_screen_transition()` now sends a real console clear sequence:
    `ESC[H ESC[2J ESC[3J`.
  - It then calls `stdscr.clear()`, `stdscr.clearok(True)`,
    `stdscr.touchwin()`, and `curses.flushinp()`.
  - This should clear stale physical console cells once when moving to a new
    screen, while avoiding the previous every-redraw flicker.
- Updated regression test in
  `imaging_server/tests/test_tui_full_repaint.py`.
- Local checks passed:
  in-memory syntax compile for Python files and
  `python -m unittest discover imaging_server\tests` (`21` tests).
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `9d8d7e4429e4d9262e48086e060649262d69ce262f8d5abd0fcb6066c48b44c8`.
- Nested phase1 package SHA256:
  `fd0a797c8e45d2b7a0148f1d92ece5167b885f9ba3b3af2ef9f3e6d9e6da59ae`.
- Deployed updated bench-client with targeted deploy and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live file hashes verified in install tree and inside squashfs:
  - `vstl-imaging-tui.py`
    `cb6e82369d1ce202440355cf4c1f67d5c2d02f9bcdca57fa32567f389e991b5f`
  - `vstl-bench-entry.sh`
    `9b68e128c2a92c159765ab1c61f22b989c0458c362d6a668bb9302634ad3e808`
- Live squashfs SHA256:
  `f8e4ae72e7ac1db4d7fa5140bf8a00c52c1824e8d22ec726d08f2768d8a0b9b7`.
- Server-side checks passed:
  - Python syntax compile for `vstl-imaging-tui.py` and `vstl_qc_tests.py`.
  - Squashfs contains `_prepare_screen_transition`, `os.write(1,
    b"\\033[H\\033[2J\\033[3J")`, `stdscr.clear()`,
    `stdscr.clearok(True)`, `stdscr.touchwin()`, and `curses.flushinp()`.
  - Squashfs no longer contains `_force_full_repaint` or
    `stdscr.redrawwin()`.
  - `dnsmasq=active`, `apache2=active`, `tftpd-hpa=active`.
- Server disk after deploy:
  `/` is `3.5T` total, `138G` used, `3.2T` available, `5%` used.
- Targeted deploy backup:
  `/opt/vstl-backups/bench-client-tui-console-clear-targeted-20260602_123447`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_tui_console_clear_fix_20260602.tar.gz`.

## TUI Menu Row / KVM Glitch Fix 2026-06-02

- User asked to check the laptop through PiKVM after continued glitches.
- PiKVM API check:
  - Video snapshot endpoint was reachable at
    `https://100.126.230.48/api/streamer/snapshot`.
  - Live snapshot showed the QC intro screen clean after fresh transition.
  - PiKVM HID reported keyboard online, but no active keyboard output, so API
    key injection did not advance the laptop even though the API returned OK.
- Remaining visible issue from screenshots:
  - Main menu could show more than one highlighted row at once.
  - This is a same-screen repaint problem, not only a between-screen stale text
    problem.
- Root cause:
  - Selectable menu rows were redrawn as short labels only.
  - On the framebuffer/PiKVM console, old reverse-video attributes and marker
    cells could remain when moving the highlight.
  - Unicode arrow markers also increased console/cell-width risk.
- Updated `bench-client/vstl-imaging-tui.py`:
  - Added `_draw_selectable_row()` to repaint the full available row width.
  - Technician and main menu selections now use fixed-width row repainting.
  - Main menu/technician marker changed to ASCII `>`.
  - Footer text changed from arrow glyphs to `UP/DOWN`.
  - `_prepare_screen_transition()` now immediately refreshes after the physical
    clear, so the console is actually blanked before the next screen draws.
  - `_confirm_yn()` and `_show_message()` now use non-empty screen keys
    (`Confirm`, `Message`) so they do not share an empty transition identity.
- Updated regression test in
  `imaging_server/tests/test_tui_full_repaint.py`.
- Local checks passed:
  in-memory syntax compile for Python files and
  `python -m unittest discover imaging_server\tests` (`22` tests).
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `0317b4f05ac147d6c00e994120d82cbeee5cb981e97f13317c4651081809bc6d`.
- Nested phase1 package SHA256:
  `a4ec1d1604179ac0d5adbb7a15a7c21b64d5a4781b5bb3529eb6234c91e76386`.
- Deployed updated bench-client with targeted deploy and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live file hashes verified in install tree and inside squashfs:
  - `vstl-imaging-tui.py`
    `f576179763fe7a945d94b370efa04d29da0375795e4dd9c3e23ec4e95b1a40b6`
  - `vstl-bench-entry.sh`
    `9b68e128c2a92c159765ab1c61f22b989c0458c362d6a668bb9302634ad3e808`
- Live squashfs SHA256:
  `e367f2133791c2083d042b13ddfcf0742ab60c5c96363f211bc28691c87c8243`.
- Server-side checks passed:
  - Python syntax compile for `vstl-imaging-tui.py` and `vstl_qc_tests.py`.
  - Squashfs contains `_draw_selectable_row`, `UP/DOWN` footer text,
    the physical clear sequence, and `draw_header(stdscr, "Confirm")`.
  - Squashfs no longer contains `_force_full_repaint`, `stdscr.redrawwin()`,
    or the Unicode menu marker assignment `marker = "▶"`.
  - `dnsmasq=active`, `apache2=active`, `tftpd-hpa=active`.
- Server disk after deploy:
  `/` is `3.5T` total, `139G` used, `3.2T` available, `5%` used.
- Targeted deploy backup:
  `/opt/vstl-backups/bench-client-tui-menu-row-targeted-20260602_125725`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_tui_menu_row_fix_20260602.tar.gz`.

## TUI Full Render Audit / Non-Reverse Selector Fix 2026-06-02

- User asked for a full script check after continued framebuffer/KVM glitches
  where old screens appeared on top of current screens.
- PiKVM verification showed the laptop had already booted a recent image
  containing the `UP/DOWN` footer changes, so the remaining issue was not a
  stale deploy or missed reboot of the server-side files.
- Audited the boot entry script and TUI renderer:
  - `vstl-bench-entry.sh` already pins `TERM=linux`, binds to `/dev/tty1`,
    and clears before launching the Python TUI.
  - The remaining faults were in the Python curses renderer.
- Root causes found:
  - `center_block()` did not split embedded newline text. Prompts containing
    `\n`, especially BIOS/password prompts, rendered the second line from
    column 0 and looked like stale text from the previous screen.
  - Normal menu selectors, header, and footer used reverse-video attributes.
    On the live Linux framebuffer through PiKVM, reverse-video cells can remain
    after a redraw and create grey bars or ghosted text.
  - Earlier `redrawwin()`/full-repaint attempts made the display flicker more
    without solving the retained-cell problem.
- Updated `bench-client/vstl-imaging-tui.py`:
  - `_prepare_screen_transition()` now sends a real terminal reset/clear once
    per screen: `ESC[0m`, cursor home, clear display, and clear scrollback.
  - Removed forced `redrawwin()` repaint loops.
  - Header and footer now use explicit color-pair fills instead of
    `A_REVERSE`.
  - Added `FOOTER_PAIR` for black-on-white footer rendering.
  - `center_block()` now renders each embedded newline as its own centered row.
  - `_draw_selectable_row()` now clears the whole physical row and redraws an
    ASCII `>` marker, without reverse-video.
  - Technician selection, main menu, and restore picker now use
    `_draw_selectable_row()`.
  - `run()` enables `keypad(True)` and `leaveok(True)` for more stable console
    input and cursor handling.
- Local checks passed:
  `python -m unittest discover imaging_server\tests` (`23` tests).
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `987af3da9be62eee23d003f9abb8dfb832ff5a6d6da506e4a2400121d535cd98`.
- Nested phase1 package SHA256:
  `81cdb9a2c09b931ef3c483b48642bf9bb3bc8bfde90a3f42c74b7f9878b70e1d`.
- Deployed updated bench-client with targeted deploy and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live file hashes verified in install tree and inside squashfs:
  - `vstl-imaging-tui.py`
    `d801eea53c3bdba9ef4e9a7842d7584a9dac59872fdb5589ea2a02c1cbed67ad`
  - `vstl-bench-entry.sh`
    `9b68e128c2a92c159765ab1c61f22b989c0458c362d6a668bb9302634ad3e808`
- Live squashfs SHA256:
  `242f0ddc979105632fd3ae7a058fc183f70ffc092a8e42dff62ad546cd94eeaf`.
- Server-side checks passed:
  - Python syntax compile for `vstl-imaging-tui.py` and `vstl_qc_tests.py`.
  - Squashfs contains `FOOTER_PAIR`, `splitlines()`,
    `_draw_selectable_row`, `keypad(True)`, `leaveok(True)`, and the physical
    `ESC[0m` clear sequence.
  - Squashfs no longer contains `_force_full_repaint`, `stdscr.redrawwin()`,
    the Unicode menu marker assignment, or normal reverse-video selector code.
  - The only remaining `A_REVERSE` use is in the display-test color fallback,
    not in normal menus or prompts.
  - `dnsmasq=active`, `apache2=active`, `tftpd-hpa=active`.
- Server disk after deploy:
  `/` is `3.5T` total, `140G` used, `3.2T` available, `5%` used.
- Targeted deploy backup:
  `/opt/vstl-backups/bench-client-tui-render-audit-targeted-20260602_135747`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_tui_render_audit_fix_20260602.tar.gz`.

## TUI Boot-Console Separation / tty2 Operator Screen Fix 2026-06-02

- User attached recording
  `C:\Users\BAHATH\AppData\Local\Packages\Microsoft.ScreenSketch_8wekyb3d8bbwe\TempState\Recordings\20260602-1032-13.5236590.mp4`
  showing different glitches after each reboot.
- Video inspection showed the true shared root cause:
  - PXE/live-boot, DHCP, squashfs download, and Clonezilla preparation output
    were all writing to `/dev/tty1`.
  - The VSTL curses UI was also launching on `/dev/tty1`.
  - Depending on timing, live-boot/Clonezilla text could appear before, during,
    or underneath VSTL screens, which made the glitches look different each
    reboot.
- Updated `bench-client/vstl-bench-entry.sh`:
  - Default VSTL operator TUI changed from `/dev/tty1` to `/dev/tty2`.
  - Added `TUI_VT` parsing and `chvt "$TUI_VT"` before Python/curses starts.
  - Added strong terminal reset/clear on the operator tty using `ESC c`,
    cursor show, clear display, and clear scrollback.
  - Added `stty sane` before launching curses.
  - `wait_for_console_quiet()` now switches to tty2 immediately and shows only
    `Preparing VSTL bench screen...` while tty1 continues receiving boot
    chatter.
  - Left PXE `ocs_live_run_tty=/dev/tty1` unchanged so Clonezilla boot/setup
    messages stay on the boot console instead of following the operator UI.
- Updated `imaging_server/tests/test_bench_entry_clean_tui_start.py`:
  - Verifies default `/dev/tty2`.
  - Verifies `TUI_VT`, `chvt`, hard terminal reset, `stty sane`, and the quiet
    loading message.
- Local checks passed:
  `python -m unittest discover imaging_server\tests` (`25` tests).
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `3d90c1d07d42d7d13d9fead2cc371c7d661710e162eeccbf5ed583010a05504e`.
- Nested phase1 package SHA256:
  `0fd54c10bf75d62910cb01a0b95fd63bfda038a8b1059e5bd7e3a98df4beeadb`.
- Deployed using `tools/deploy_bench_client_live.sh` and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live file hashes verified in install tree and inside squashfs:
  - `vstl-bench-entry.sh`
    `8cf5cde773d0dbcb4f106f1028c9856d0ffb1760ff5c4674bf2119233c4a81a8`
  - `vstl-imaging-tui.py`
    `d801eea53c3bdba9ef4e9a7842d7584a9dac59872fdb5589ea2a02c1cbed67ad`
- Live squashfs SHA256:
  `302eee65f2f1f0cd4f1b70981236d4ba6b239c497c2f77b21c00595b8f75fdab`.
- Server-side checks passed:
  - `bash -n` for `vstl-bench-entry.sh`.
  - Python syntax compile for `vstl-imaging-tui.py` and `vstl_qc_tests.py`.
  - Squashfs contains `/dev/tty2`, `TUI_VT`, `chvt`, `Preparing VSTL bench
    screen`, `stty sane`, and the hard terminal reset sequence.
  - PXE boot files still contain `ocs_live_run_tty=/dev/tty1` intentionally,
    keeping boot logs on tty1.
  - `dnsmasq=active`, `apache2=active`, `tftpd-hpa=active`.
- Server disk after deploy:
  `/` is `3.5T` total, `146G` used, `3.2T` available, `5%` used.
- Targeted deploy backup:
  `/opt/vstl-backups/bench-client-deploy-20260602_145634`.
- Full setup backup:
  `/opt/vstl-backups/vstl-imaging-setup-current.tar.gz`
  SHA256 `71a6c2e8a3193ee1fdada722d5066c57bbdb854473915044a9f6f040e2befc7d`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_tty2_console_fix_20260602.tar.gz`.

## TUI Full Canvas Overwrite Fix 2026-06-02

- User attached second recording
  `C:\Users\BAHATH\AppData\Local\Packages\Microsoft.ScreenSketch_8wekyb3d8bbwe\TempState\Recordings\20260602-1109-04.7644253.mp4`
  after the tty2 fix.
- Video inspection showed:
  - The laptop did load the tty2 fix because it displayed
    `Preparing VSTL bench screen...`.
  - The remaining glitch was inside the curses renderer: after selecting L1,
    the main menu appeared on the left while old L1/L2 technician text remained
    visible on the right.
- Root cause:
  - The Linux framebuffer/PiKVM console did not reliably clear cells that were
    untouched by the next narrower screen, even after `erase()`, clear escape
    sequences, and screen-transition clears.
- Updated `bench-client/vstl-imaging-tui.py`:
  - Added `_blank_canvas(stdscr)` to physically overwrite every visible row
    with normal blank cells.
  - `draw_header()` now calls `_blank_canvas(stdscr)` before drawing header,
    body, or footer content. This forces each screen to repaint a clean black
    canvas before adding current content.
- Updated `imaging_server/tests/test_tui_full_repaint.py`:
  - Verifies `_blank_canvas`.
  - Verifies every row is overwritten using
    `stdscr.addstr(y, 0, blank, curses.A_NORMAL)`.
  - Verifies `_blank_canvas(stdscr)` runs before the header title is drawn.
- Local checks passed:
  `python -m unittest discover imaging_server\tests` (`25` tests).
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `57ca79ca58e7481ddac8116889c8844fbc15be1559856c8171f5190fc46db34f`.
- Nested phase1 package SHA256:
  `fa6d3a6f5338a1c67f259b24fcdc05287f591bfe0103152c4c96e71589b7cc23`.
- Deployed using `tools/deploy_bench_client_live.sh` and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Live file hashes verified in install tree and inside squashfs:
  - `vstl-imaging-tui.py`
    `0649ccd5ec1c251563baa743f39cc93ba6fdafa8396b0fc6f2e374ae7b30d3d5`
  - `vstl-bench-entry.sh`
    `8cf5cde773d0dbcb4f106f1028c9856d0ff5c4674bf2119233c4a81a8`
- Live squashfs SHA256:
  `5042fc91ee6e2ad73ac28cedf4a78638fbfd6e1d23b47c2cda0b7ae9b98c337a`.
- Server-side checks passed:
  - `bash -n` for `vstl-bench-entry.sh`.
  - Python syntax compile for `vstl-imaging-tui.py` and `vstl_qc_tests.py`.
  - Squashfs contains `_blank_canvas`, full-row blank overwrite, tty2,
    `chvt`, and `Preparing VSTL bench screen`.
  - `dnsmasq=active`, `apache2=active`, `tftpd-hpa=active`.
- Server disk after deploy:
  `/` is `3.5T` total, `152G` used, `3.2T` available, `5%` used.
- Targeted deploy backup:
  `/opt/vstl-backups/bench-client-deploy-20260602_151823`.
- Full setup backup:
  `/opt/vstl-backups/vstl-imaging-setup-current.tar.gz`
  SHA256 `a2d5eec094c14b19b8656b4fe2ae2e7e1ac0f4a35c02f10c56a7fef6f23583f5`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_full_canvas_fix_20260602.tar.gz`.

## Console Physical Blank + Input Queue Fix 2026-06-02

- User attached recording
  `C:\Users\BAHATH\AppData\Local\Packages\Microsoft.ScreenSketch_8wekyb3d8bbwe\TempState\Recordings\20260602-1127-25.4599170.mp4`
  showing:
  - Previous-screen text still visible on new screens.
  - `Y`, `N`, `ENTER`, and menu selections appearing to require two keypresses.
- Root cause:
  - The framebuffer console still retained stale cells on some transitions.
  - `curses.flushinp()` in the screen-transition path could discard the first
    real operator key pressed just after a redraw.
- Updated `bench-client/vstl-imaging-tui.py`:
  - Added `_physical_blank_tty(stdscr)` to overwrite the active Linux console
    with blank cells using direct terminal escape output once per screen.
  - `_prepare_screen_transition()` now performs a terminal clear, then calls
    `_physical_blank_tty(stdscr)`, then lets curses repaint the current screen.
  - Removed `curses.flushinp()` from the transition path so the first operator
    key is no longer swallowed after prompts or screen changes.
- Updated `imaging_server/tests/test_tui_full_repaint.py`:
  - Verifies `_physical_blank_tty`.
  - Verifies the direct physical blank write.
  - Verifies `curses.flushinp()` is not present.
- Local checks passed:
  `python -m unittest discover -s imaging_server/tests -p "test_*.py"`
  (`25` tests).
- `python -m pytest` was not available locally because `pytest` is not
  installed; unittest coverage passed.
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local package SHA256:
  `fdd87dd28cb29a1d089efb27cf1ee3e3d587d7a367665baa040fc915541d734b`.
- Nested phase1 package SHA256:
  `273dee3066f8cadd9bcc97556fff5477361616a3650e035835f67d14fedbecf1`.
- Timestamp package copy:
  `dist/vstl-imaging-server_20260602_153807.tar.gz`.
- Deployed using `tools/deploy_bench_client_live.sh` and rebuilt
  `/var/www/html/vstl-pxe/filesystem.squashfs`.
- Server deploy timestamp:
  `20260602_153955`.
- Live squashfs SHA256:
  `d1361c9fb7f53c9259113d21d5e527f0c759dd332e6841d44607fdf511ddc7a6`.
- Server-side checks passed:
  - Installed source contains `_physical_blank_tty` and no `flushinp`.
  - Squashfs contains `_physical_blank_tty` and no `flushinp`.
  - Python syntax compile passed for `vstl-imaging-tui.py` and
    `vstl_qc_tests.py`.
  - `dnsmasq=active`, `apache2=active`, `tftpd-hpa=active`.
- Server disk after deploy:
  `/` is `3.5T` total, `158G` used, `3.2T` available, `5%` used.
- Targeted deploy backup:
  `/opt/vstl-backups/bench-client-deploy-20260602_153955`.
- Full setup backup:
  `/opt/vstl-backups/vstl-imaging-setup-current.tar.gz`.
- Final package saved on server:
  `/opt/vstl-backups/vstl-imaging-server_current_console_blank_input_fix_20260602.tar.gz`.
- KVM validation after deploy:
  - Booted the attached Latitude 5530 through Dell F12 -> `ONBOARD NIC (IPV4)`.
  - PXE downloaded from server `10.255.254.75` and loaded the patched VSTL TUI.
  - One `ENTER` on the technician level screen advanced immediately.
  - After closing/focusing away from the PiKVM virtual keyboard overlay and
    clicking the KVM video stream, one `N` on the BIOS Admin Password prompt
    advanced immediately to the lock-audit clear screen.
  - Continued to the next CPU prompt; screen was clean with no stale technician
    menu or old prompt text.
  - Note: if PiKVM's virtual keyboard/sidebar has focus, browser keypresses may
    be consumed by PiKVM before reaching the host. Click inside the black VSTL
    video stream once before testing keys through PiKVM.

## PXE Config Directory Cleanup 2026-06-03

- User reported Dell PXE showed only `Start PXE over IPv4`, then fell into
  Dell SupportAssist/on-board diagnostics.
- Server services were up: `dnsmasq`, `tftpd-hpa`, and `apache2` active.
- Root cause found in `/etc/dnsmasq.d`:
  - Current `vstl-imaging.conf` correctly used proxyDHCP and
    `vstl-clean-ipxe.efi`.
  - Old backup `vstl-imaging.conf.bak-20260524_140742` was still in the same
    directory.
  - Debian dnsmasq reads non-ignored files from `/etc/dnsmasq.d`, so the backup
    was also live and reintroduced the old `10.255.254.30-70` DHCP range plus
    `snponly.efi`.
- Live fix:
  - Moved readable non-current dnsmasq files to
    `/opt/vstl-backups/dnsmasq-conf-disabled-20260603_105852`.
  - Restarted dnsmasq.
  - Fresh log now shows only `DHCP, proxy on subnet 10.255.254.0`.
  - Live `/etc/dnsmasq.d` now contains only `vstl-imaging.conf`.
- Interference/IP finding:
  - No external rogue PXE server was confirmed.
  - Normal DHCP server observed as `10.255.254.1`
    (`68:cc:ae:46:6c:3d`).
  - The interfering PXE/DHCP source was the VSTL server itself
    (`10.255.254.75`) because of the stale backup config.
- Installer hardening:
  - `02_install_dnsmasq.sh` now archives stale `vstl-imaging.conf.*` and
    `.bak*` files out of `/etc/dnsmasq.d` before validating/restarting dnsmasq.
  - Updated both `/opt/vstl-imaging/02_install_dnsmasq.sh` and
    `/opt/vstl-imaging-phase1/02_install_dnsmasq.sh` on the server.
  - Added regression assertion in
    `imaging_server/tests/test_dnsmasq_legacy_pxe.py`.
- Local assertion checks passed by direct Python execution because local
  `pytest` is not installed.
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local/server package SHA256:
  `3de7e8182f785b79576350a2361d479de10b35831d9f9f4b121ac75a4fa8a3d5`.
- Server package saved:
  `/opt/vstl-backups/vstl-imaging-server_current_dnsmasq_confdir_fix_20260603.tar.gz`.

## UEFI Initrd Panic Fix 2026-06-03

- After the dnsmasq cleanup, the Latitude 5530 reached iPXE and downloaded:
  - `/vstl-pxe/boot.ipxe`
  - `/vstl-pxe/vmlinuz`
  - `/vstl-pxe/initrd-vstl-fixed.cpio`
- It then panicked with:
  `Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)`.
- Apache logs showed no request for `/vstl-pxe/filesystem.squashfs`, so the
  failure was before live-boot userspace.
- Root cause:
  - The clean full EFI iPXE loader downloaded the unnamed raw cpio initrd but
    did not attach it to the Linux EFI stub.
  - The previous raw-cpio/no-name path was safe for the initrd content, but not
    for this EFI handoff.
- Live fix:
  - Updated `/var/www/html/vstl-pxe/boot.ipxe`.
  - Updated `/tftpboot/vstl.ipxe`.
  - Production boot now uses named raw cpio:
    `initrd=initrd-vstl-fixed.cpio`
    plus
    `initrd --name initrd-vstl-fixed.cpio http://10.255.254.75/vstl-pxe/initrd-vstl-fixed.cpio initrd-vstl-fixed.cpio`.
  - Kept the verified raw cpio content:
    SHA256 `5433d3c7a53ba52bbaa4ef38a3c5aecc0da30fbf4f7fd564461b832420acdcef`.
- Updated generator:
  - `imaging_server/06_setup_pxe_netboot.sh`
  - `/opt/vstl-imaging/06_setup_pxe_netboot.sh`
  - `/opt/vstl-imaging-phase1/06_setup_pxe_netboot.sh`
- Updated regression test:
  - `imaging_server/tests/test_06_diagnostic_boot_variants.py`
- Local assertion checks passed by direct Python execution for:
  - `test_06_diagnostic_boot_variants.py`
  - `test_dnsmasq_legacy_pxe.py`
- Rebuilt local/current package:
  `dist/vstl-imaging-server_current.tar.gz`.
- Local/server package SHA256:
  `d9540bdef813580d064b6b5b7369533ff871419927f7fb3075e0db34385dbc99`.
- Server package saved:
  `/opt/vstl-backups/vstl-imaging-server_current_named_raw_cpio_20260603.tar.gz`.

## Prepared FOG Deploy Script

Script:

```text
imaging_server/tools/deploy_bench_client_live.sh
```

Run on the FOG server after copying the latest tar to `/tmp/vstl-imaging-server_current.tar.gz`:

```bash
cd /tmp
rm -rf vstl-deploy
mkdir -p vstl-deploy
tar -xzf /tmp/vstl-imaging-server_current.tar.gz -C vstl-deploy
cd /tmp/vstl-deploy/imaging_server
sudo bash tools/deploy_bench_client_live.sh
```

Expected setup tar after successful FOG deploy:

```text
/opt/vstl-backups/vstl-imaging-setup-current.tar.gz
/opt/vstl-backups/vstl-imaging-setup-<timestamp>.tar.gz
```

## TUI Repaint Glitch Fix 2026-06-03

- Fixed stale menu/text cells where the technician-selection UI could remain
  visible behind the main menu or QC/manual-confirm screens.
- Updated `imaging_server/bench-client/vstl-imaging-tui.py`:
  - Added robust transition clearing that writes terminal reset/clear bytes to
    stdout, `/dev/tty`, `/dev/tty1`, and `/dev/console` when available.
  - Strengthened the curses canvas clear to blank each row independently with
    normal attributes, `clrtoeol()`, `redrawwin()`, and `doupdate()`.
  - Added a short input drain between technician selection and the main menu so
    transition key repeats do not leak into the next prompt.
- Updated `imaging_server/tools/deploy_bench_client_live.sh`:
  - Added a same-path guard so deploy does not fail when the server source
    bench-client folder is also the install bench-client folder.
- Updated `imaging_server/tests/test_tui_full_repaint.py` to cover the stronger
  physical TTY clear, row blanking, redraw, and input-drain behavior.
- Server deploy completed on `100.88.250.63`:
  - Patched TUI SHA256:
    `973033f60a83e5d2eedbad271ab9a3858896776d711cc0e6fe9e290a015ed174`
  - Live PXE squashfs SHA256:
    `ebaa2039941d2e80f4a6de8c44014d81862306c81ee31aa7ea87e9026e987ea1`
  - Server setup-current tar SHA256:
    `e0d5f5cb81f1ebfe8a528e5e6a1fb38bb520527ff283a7b42231fa9da8691a66`
  - Server rollback backup:
    `/opt/vstl-backups/bench-client-deploy-20260603_200835`
- Server storage after deploy:
  - `/dev/mapper/ubuntu--vg-ubuntu--lv` mounted at `/`
  - Size `3.5T`, used `166G`, available `3.2T`, use `5%`
- Verification:
  - Local Python syntax compile passed for `vstl-imaging-tui.py`.
  - Local repaint unittest passed: `python -m unittest imaging_server.tests.test_tui_full_repaint`.
  - Server syntax compile passed using no-bytecode compile mode.
  - Server repaint unittest passed with `PYTHONDONTWRITEBYTECODE=1`.
- Rebuilt local package/reference tar:
  - `dist/vstl-imaging-server_20260603_201638.tar.gz`
  - `dist/vstl-imaging-server_current.tar.gz`
  - Local/current tar SHA256:
    `470faa5c4ec74f6014b75f53079e3f076ce37fb192ce369f8f031fa2046bf49a`
- Uploaded matching package reference to server:
  - `/opt/vstl-backups/vstl-imaging-server_current.tar.gz`
  - `/opt/vstl-backups/vstl-imaging-server_current_tui_repaint_fix_20260603.tar.gz`
  - Server package SHA256:
    `470faa5c4ec74f6014b75f53079e3f076ce37fb192ce369f8f031fa2046bf49a`

## Single UI Owner Fix 2026-06-03

- Follow-up after the TUI repaint work:
  - The remaining overlay showed the L1/L2 technician-selection text being
    drawn while the main menu was already active.
  - Root cause was a launch race, not just dirty screen cells.
- Confirmed launch race:
  - PXE kernel command line runs:
    `ocs_live_run=/opt/vstl/vstl-bench-entry.sh`
  - The live rootfs also has `vstl-imaging.service` with:
    `ExecStart=/opt/vstl/vstl-bench-entry.sh`
  - After the earlier startup-delay/clean-tty fix, both launchers could overlap
    long enough for two entry scripts/TUIs to write to the operator console.
- Updated `imaging_server/bench-client/vstl-bench-entry.sh`:
  - Added a single-instance lock at `/run/vstl-bench-entry.lock`.
  - Uses `flock -n` when available.
  - Falls back to an atomic `/run/vstl-bench-entry.lock.d` directory if
    `flock` is missing.
  - Duplicate launchers log
    `Another VSTL bench entry is already running; exiting duplicate launcher`
    and exit `0`, leaving the first UI as the only console owner.
- Updated `imaging_server/tests/test_bench_entry_clean_tui_start.py`:
  - Added regression coverage for the single-owner lock.
- Server deploy completed on `100.88.250.63`:
  - Entry script SHA256:
    `2856c13f0c8538710fa82aebbc237da2d46588474a3bdcf3871e54cebf1f21e4`
  - Live PXE squashfs SHA256:
    `907540c6fef0e1dccc28b0f57bc68b3cbb9c21f435d38fe5728c3ffeb9ab703f`
  - Server setup-current tar SHA256:
    `d2c14a0e18ced5a1e72752277080ef0b7a42bbe0dbb33d23411a61765e84490b`
  - Server rollback backup:
    `/opt/vstl-backups/bench-client-deploy-20260603_202534`
- Verification:
  - Server `bash -n bench-client/vstl-bench-entry.sh` passed.
  - Server unittests passed:
    `tests.test_bench_entry_clean_tui_start` and
    `tests.test_tui_full_repaint`.
  - Local unittests passed for the same entry/TUI repaint coverage.
- Rebuilt local package/reference tar:
  - `dist/vstl-imaging-server_20260603_203342.tar.gz`
  - `dist/vstl-imaging-server_current.tar.gz`
  - Local/current tar SHA256:
    `daf6a3f1562da1c38c1c14713c8b7669161f1f4e48f5556603e010181886493e`
- Uploaded matching package reference to server:
  - `/opt/vstl-backups/vstl-imaging-server_current.tar.gz`
  - `/opt/vstl-backups/vstl-imaging-server_current_single_ui_lock_20260603.tar.gz`
  - Server package SHA256:
    `daf6a3f1562da1c38c1c14713c8b7669161f1f4e48f5556603e010181886493e`
- Server storage after deploy:
  - `/dev/mapper/ubuntu--vg-ubuntu--lv` mounted at `/`
  - Size `3.5T`, used `172G`, available `3.2T`, use `6%`

## Display Type Pause Fix 2026-06-03

- Updated `imaging_server/bench-client/vstl-imaging-tui.py`:
  - The QC display type result screen now holds for 3 seconds for both
    `TOUCH display` and `NON-TOUCH display`.
  - Pressing Enter skips the 3-second hold immediately.
  - Non-touch text now shows:
    `Touch test skipped. Moving to the next QC step in 3 seconds.`
  - Touch text now shows:
    `Opening full-screen touch map in 3 seconds.`
- Added regression test:
  - `imaging_server/tests/test_display_type_pause.py`
- Server deploy completed on `100.88.250.63`:
  - Patched TUI SHA256:
    `a30140de4e9cc068bcb47f2acfc1323a90d5dcf599d9de9bc376e08d91818314`
  - Live PXE squashfs SHA256:
    `94b6a23dada2b665314f4cced207dec7e568a30dd731d663886f5fc2d4ccc9ce`
  - Server setup-current tar SHA256:
    `37a378e4494bc6cc3fbbe5b5d7a02a523f93548a5623beb515237b182c1d0510`
  - Server rollback backup:
    `/opt/vstl-backups/bench-client-deploy-20260603_204258`
- Verification:
  - Local Python syntax compile passed for `vstl-imaging-tui.py`.
  - Local unittests passed:
    `tests.test_display_type_pause` and `tests.test_tui_full_repaint`.
  - Server syntax compile passed using no-bytecode compile mode.
  - Server unittests passed:
    `tests.test_display_type_pause` and `tests.test_tui_full_repaint`.
- Rebuilt local package/reference tar:
  - `dist/vstl-imaging-server_20260603_204856.tar.gz`
  - `dist/vstl-imaging-server_current.tar.gz`
  - Local/current tar SHA256:
    `7de6074cf8cbaa888fa06d097184fbb1b654f9434fbce07fb2c5eefc65b2c3db`
- Uploaded matching package reference to server:
  - `/opt/vstl-backups/vstl-imaging-server_current.tar.gz`
  - `/opt/vstl-backups/vstl-imaging-server_current_display_type_pause_20260603.tar.gz`
  - Server package SHA256:
    `7de6074cf8cbaa888fa06d097184fbb1b654f9434fbce07fb2c5eefc65b2c3db`
- Server storage after deploy:
  - `/dev/mapper/ubuntu--vg-ubuntu--lv` mounted at `/`
  - Size `3.5T`, used `178G`, available `3.2T`, use `6%`

## Dell Old-Generation IPv4 PXE Vendor Fix 2026-06-03

- Updated `imaging_server/03_dnsmasq_proxydhcp.conf`:
  - `dhcp-pxe-vendor` now accepts:
    `PXEClient,HW-Client,MSFT 5.0`
  - This keeps the server in proxyDHCP mode, so it still does not hand out IP
    leases or create a second full DHCP server.
  - Purpose: older Dell UEFI PXE paths can show only `Start PXE over IPv4` and
    then fall back to internal diagnostics/Windows when the firmware does not
    identify itself as a plain `PXEClient`.
- Updated regression coverage:
  - `imaging_server/tests/test_dnsmasq_legacy_pxe.py`
- Server deploy completed on `100.88.250.63`:
  - Live active config:
    `/etc/dnsmasq.d/vstl-imaging.conf`
  - Source install template:
    `/opt/vstl-imaging-phase1/03_dnsmasq_proxydhcp.conf`
  - Server backup:
    `/opt/vstl-backups/pxe-msft-vendor-20260603_210334`
- Verification:
  - Local direct PXE config assertions passed.
  - Server `dnsmasq --test --conf-dir=/etc/dnsmasq.d` passed.
  - Services active:
    `dnsmasq`, `tftpd-hpa`, `apache2`.
  - Live UEFI loader stayed:
    `vstl-clean-ipxe.efi`
- Rebuilt local package/reference tar:
  - `dist/vstl-imaging-server_20260603_210452.tar.gz`
  - `dist/vstl-imaging-server_current.tar.gz`
  - Local/current tar SHA256:
    `476a90d43635b1426d1326a5b8dac0d928996a5cd7a32a8c5abd4a7312da9e79`
- Uploaded matching package reference to server:
  - `/opt/vstl-backups/vstl-imaging-server_current.tar.gz`
  - `/opt/vstl-backups/vstl-imaging-server_current_dell_old_pxe_vendor_20260603.tar.gz`
  - Server package SHA256:
    `476a90d43635b1426d1326a5b8dac0d928996a5cd7a32a8c5abd4a7312da9e79`

## Do Not Change Unless Explicitly Requested

- Do not modify the working PXE boot chain, iPXE loader, initrd handoff, DHCP/TFTP flow, or KVM behavior during the next deploy.

## Physical Port Tracking Fix 2026-06-07

- Updated `imaging_server/bench-client/vstl_qc_tests.py`:
  - USB devices now resolve to a stable physical root-port path instead of a
    changing USB device number.
  - Type-C partner detection now reads the normal sibling
    `/sys/class/typec/portN-partner` layout and returns stable `portN` IDs.
  - Audio-jack detection now checks every ALSA card, HDA pin sense, and Linux
    input jack switches through `evtest --query`.
  - HDMI, VGA, DisplayPort, and DVI detection now also checks connector
    `enabled` state, EDID presence, XRandR, and `modetest`.
- Updated `imaging_server/bench-client/vstl-imaging-tui.py`:
  - Each USB-A and USB-C row requires a previously untested physical connector.
  - Reconnecting the same connector cannot complete another row.
  - Added a 1.2-second USB classification window so USB-C enumeration is not
    incorrectly counted as USB-A before the Type-C partner event arrives.
- Added regression coverage in
  `imaging_server/tests/test_ports_wireless_qc.py`.
- Verification:
  - Python syntax compile passed.
  - Port/audio focused tests passed: `19 passed`.
  - Full local suite: `94 passed`; one unrelated Windows-only synthetic
    EnterpriseMgmt case-sensitivity fixture failed.
- Server deploy completed on `100.88.250.63`:
  - `vstl_qc_tests.py` SHA256:
    `b88f42ef705abc9ab7441b9e75dc0fc0b23842c7fa93437422880f5f892d018a`
  - `vstl-imaging-tui.py` SHA256:
    `9ca8c80e499e426d15f5b55bf0efb1f25a2180ea4211511fc951eab10f368faf`
  - Live PXE squashfs SHA256:
    `3effe2c92135ab1df3005ca9f6b778827cd375e0de168a801563e338fe79ff14`
  - Server setup-current tar SHA256:
    `95f69f810816d041573191f16ae610fcee7feb348f031a191f984912b28ade38`
  - Rollback backup:
    `/opt/vstl-backups/bench-client-deploy-20260607_220730`
- PXE boot chain, DHCP, TFTP, iPXE loader, kernel, and initrd were unchanged.
## Ports QC Continuation Fix 2026-06-07

- Continued the interrupted ports repair.
- USB-A rows now track a companion-controller-safe socket fingerprint so
  replugging the same physical port cannot complete another USB row.
- USB-C can fall back to a new USB insertion after all USB-A rows are complete
  when the laptop does not expose `/sys/class/typec` partner state.
- Display connector detection now parses HDMI, VGA, DP/DisplayPort, and DVI
  names directly instead of relying on a narrow prefix list.
- Ethernet link detection now checks carrier, operstate, interface flags, and
  `ethtool` link state.
- Audio-jack detection now checks udev switch-tagged event devices as well as
  ALSA jack controls and HDA pin sense.
- Added `ethtool` to the live rootfs and future build scripts.
- Verification:
  - Focused ports/audio tests: `23 passed`.
  - Full local suite: `100 passed`; one unrelated Windows-only synthetic
    EnterpriseMgmt path-case fixture failed.
  - Served PXE squashfs contains the updated code and `ethtool`.
- Server deployment on `100.88.250.63`:
  - Deployment timestamp: `20260607_232125`
  - `vstl_qc_tests.py` SHA256:
    `9a62b1a5e6f1ee3d02159b45a474f276b3f89e69c75523635448c717c9a14412`
  - `vstl-imaging-tui.py` SHA256:
    `fbcd250fa7c79becaffd585a1f07a4af8ea261ecd5e09246bbabc516065f0251`
  - Live PXE squashfs SHA256:
    `4f0f6bf9b6237c0b3989f39b91c5fea305d2b906e6b267bb3ca7b1b49647a8da`
  - Server setup-current tar SHA256:
    `a97c243605807274b809a3abeeeab20997f0d96296e1dcec2a535860813274fc`
  - Rollback backup:
    `/opt/vstl-backups/bench-client-deploy-20260607_232125`
- Rebuilt local/reference packages:
  - `imaging_server/dist/vstl-imaging-phase1.tar.gz`
    SHA256 `db33983c8461b4fe77675b7bafbf95b2fbecfe09cfb1c8c3d3219bfc1366d5a3`
  - `dist/vstl-imaging-server_20260607_232859.tar.gz`
  - `dist/vstl-imaging-server_current.tar.gz`
    SHA256 `0f336ce0cb57cfa0b313a6ff0248b1d986a007178f8c0c2c7931cf08b14ac1df`
- PXE boot chain, DHCP, TFTP, iPXE loader, kernel, and initrd were unchanged.
## Speaker and Microphone QC Restoration 2026-06-07

- Restored Speaker and Microphone as mandatory tests in both L1 and L2 QC.
- Removed the runtime behavior that silently marked either test N/A when ALSA
  had not yet enumerated a playback or capture device.
- Added an audio readiness pass that loads common Intel audio drivers, settles
  sound udev devices, initializes ALSA, and retries device enumeration.
- Added kernel-card and `/proc/asound/pcm` fallback detection.
- Added Intel SOF firmware to the live rootfs and future Clonezilla builds:
  - `firmware-sof-signed`
  - `firmware-intel-sound`
- Verification:
  - Audio regression tests: `11 passed`.
  - Source and active-rootfs client hashes match.
  - Both firmware packages are installed; 535 SOF firmware files are present.
- Server deployment on `100.88.250.63`:
  - Deployment timestamp: `20260607_225844`
  - `vstl-imaging-tui.py` SHA256:
    `137301d995f98e9a1ad97d61fdd8e0cf93cde05d3f412a8641efaa1d0dda9672`
  - `vstl_qc_tests.py` SHA256:
    `4f1e36230370024564e5edcd8dbe293da4374fa92797d9adabf17a1c0d82cc77`
  - Live PXE squashfs SHA256:
    `f548866c09e13c5f25f2d37c10f0d2bb7dcc524dcb6457c4669991ac84d53591`
  - Server setup-current tar SHA256:
    `f3716ddd6a2b140b57d4dd4d09528f507d3d22bffa3c25d09d42e883d75df2a3`
  - Rollback backup:
    `/opt/vstl-backups/bench-client-deploy-20260607_225844`
- Rebuilt local/reference packages:
  - `imaging_server/dist/vstl-imaging-phase1.tar.gz`
    SHA256 `defb7d25baf5b76245c3a4cc588c3b63418d85de1e6b43e2694ad1756009a044`
  - `dist/vstl-imaging-server_20260607_230741.tar.gz`
  - `dist/vstl-imaging-server_current.tar.gz`
    SHA256 `f7bd8f5df5dceff61937ef2ead5743c0cd130352deb96c4af6c9b42cd393c6b6`
- Updated server reference archives:
  - `/opt/vstl-backups/vstl-imaging-server_current.tar.gz`
  - `/opt/vstl-backups/vstl-imaging-server_current_audio_restore_20260607.tar.gz`
  - `/opt/vstl-backups/vstl-imaging-phase1-current.tar.gz`
- PXE boot chain, DHCP, TFTP, iPXE loader, kernel, and initrd were unchanged.

## Touch Map Event Device Fix 2026-06-07

- Fixed touch-map startup on systems that expose multiple HID-over-I2C event
  nodes:
  - The TUI now checks every `/dev/input/event*` device instead of rejecting
    all devices except one preferred path.
  - Each event device is classified using coordinates, touch keys, input
    properties, udev touchscreen/touchpad properties, and device name.
  - Generic multitouch controllers with touch coordinates are accepted while
    touchpads and indirect pointers remain excluded.
- Updated `vstl-bench-entry.sh` to make evdev event nodes readable before the
  Python TUI starts.
- Repaired and completed the live rootfs package installation:
  - `python3-evdev`
  - `evtest`
  - `libdrm-tests` (`modetest`)
- Updated both live-image build scripts so future builds include those runtime
  tools.
- Verification:
  - Focused touch/ports/runtime tests: `21 passed`.
  - Full local suite: `96 passed`; one unrelated Windows-only synthetic
    EnterpriseMgmt path-case fixture failed.
  - Server Python compile and Bash syntax checks passed.
  - Source and active-rootfs client hashes match.
  - `dnsmasq`, `apache2`, and `tftpd-hpa` are active.
- Server deployment on `100.88.250.63`:
  - Deployment timestamp: `20260607_223727`
  - `vstl-imaging-tui.py` SHA256:
    `b147e2f75b12d0c030ce9ee78ac10a74d206ea325cc5ebd17fe5840f31c0ce8d`
  - `vstl_qc_tests.py` SHA256:
    `eed2593dbac9b844c6043f142ab8bcf4bff7e2d37635c95fa29f6bb63cd4b110`
  - `vstl-bench-entry.sh` SHA256:
    `18345c0d7e595431716ba9c3751a77139634b5820c0c6d275a1cca3194bd060f`
  - Live PXE squashfs SHA256:
    `b83a9b3fe9b5fe75b7eb26954b8b9cd3dcea3b9d54297c777f47095e80ac1e4f`
  - Server setup-current tar SHA256:
    `13d8bc3da89e56e2cdc76bc04dc363019576bdfda77fb06a41a17a2ddfe7581c`
  - Rollback backup:
    `/opt/vstl-backups/bench-client-deploy-20260607_223727`
- Rebuilt local/reference packages:
  - `imaging_server/dist/vstl-imaging-phase1.tar.gz`
    SHA256 `7effb7b557b776e18b5a9efc2315691f113c7196ed423bfd78eb558c85d205a8`
  - `dist/vstl-imaging-server_20260607_224703.tar.gz`
  - `dist/vstl-imaging-server_current.tar.gz`
    SHA256 `f9b087ba9b092555cba6b2a5608a423fce223c7ca95f67dc88c42e1b67c01368`
- PXE boot chain, DHCP, TFTP, iPXE loader, kernel, and initrd were unchanged.

## Guided Keyboard Profile QC 2026-06-10

- Added a six-step keyboard profile wizard before keyboard QC:
  - numeric keypad with/without
  - US ANSI or UK ISO physical layout
  - QWERTY, AZERTY, or QWERTZ arrangement
  - printed format/language, including a manual Other entry
  - pointing stick/joystick with/without
  - keyboard backlight with/without
- Keyboard names are normalized as:
  `REGION ARRANGEMENT WITH/WITHOUT BACK LIGHT`.
- Arabic legends are retained as `WITH ARABIC PRINT`.
- The raw evdev keyboard map now follows the selected profile:
  - numpad rows only appear when selected
  - UK ISO includes the extra ISO key
  - QWERTZ swaps Y/Z labels
  - AZERTY uses its physical letter positions
- The selected profile is saved in the keyboard QC result and final ingest
  payload as `keyboard_type`, `keyboard_status`, and `keyboard_profile`.
- Local verification:
  - focused keyboard/UI tests: `23 passed`
  - full imaging-server suite: `111 passed`, with one unrelated existing
    Windows-only EnterpriseMgmt synthetic path test failing
  - Python compile checks passed
- Live deployment completed together with the Wi-Fi scan correction below.

## Wi-Fi QC Scan Reliability and Live Deployment 2026-06-10

- Fixed false `SSID not found` failures when the laptop Wi-Fi adapter works:
  - ignores `P2P-device` and monitor helper interfaces
  - discovers physical wireless interfaces from `iw` and sysfs
  - unblocks Wi-Fi with `rfkill`
  - raises the interface before scanning
  - retries the scan up to three times
  - falls back to `iwlist` when available
  - counts hidden BSS networks as valid nearby Wi-Fi evidence
- The QC result screen now shows the detected Wi-Fi adapter and any visible or
  hidden networks.
- Local verification:
  - focused keyboard/Wi-Fi tests: `23 passed`
  - full imaging-server suite: `114 passed`, with one unrelated existing
    Windows-only EnterpriseMgmt synthetic path test failing
  - Python compile checks passed
- Live server `100.88.250.63` deployment:
  - source update backup:
    `/opt/vstl-backups/source-update-20260610_203224`
  - rollback backup:
    `/opt/vstl-backups/bench-client-deploy-20260610_203250`
  - setup archive:
    `/opt/vstl-backups/vstl-imaging-setup-20260610_203250.tar.gz`
  - setup-current SHA256:
    `ca1b7c285bc4fa2874a6a1132fa949ded7d7dccc0f8102c1fe86067d71a53de7`
  - live PXE squashfs SHA256:
    `327d4c934aaaf1491ccec4ff7b222b33a30f53d195ea400ce127207f8494e7c5`
  - `dnsmasq`, `apache2`, and `tftpd-hpa` are active
  - `boot.ipxe` and `filesystem.squashfs` return HTTP 200
  - active and build squashfs hashes match
  - server free space after deployment: approximately `3.0T`
- PXE boot chain, DHCP, TFTP, iPXE loader, kernel, and initrd were unchanged.
- Rebuilt local/reference packages:
  - `imaging_server/dist/vstl-imaging-phase1.tar.gz`
    SHA256 `7b367153968cc647540eae8e6862ee0dd92b5dcd517bf3de5e7f32afe60ff96c`
  - `dist/vstl-imaging-server_20260610_204322.tar.gz`
  - `dist/vstl-imaging-server_current.tar.gz`
    SHA256 `4e3f0737a7d6fcc7fdbbd42d1288667c77672085b8a7abfd1f074d3b412116fd`

## PXE Direct HTTP Handoff 2026-06-10

- Diagnosed connected client MAC `10:65:30:66:c1:3b`:
  - received PXE lease `10.255.0.81`
  - downloaded `snponly.efi`
  - completed iPXE DHCP
  - never requested `/vstl-pxe/boot.ipxe` from Apache
- Removed the failing iPXE-to-TFTP `default.ipxe` handoff.
- iPXE DHCP option 67 now points directly to:
  `http://10.255.0.75/vstl-pxe/boot.ipxe`.
- Firmware still downloads its first-stage EFI loader through TFTP.
- Live server backup:
  `/opt/vstl-backups/pxe-direct-http-20260610_210925`.
- Verification:
  - dnsmasq syntax check passed
  - `dnsmasq`, `apache2`, and `tftpd-hpa` active
  - HTTP boot script returns 200
  - focused PXE tests: `10 passed`
  - full suite: `114 passed`, with one unrelated existing Windows-only
    EnterpriseMgmt synthetic path test failing
- Refreshed packages:
  - `imaging_server/dist/vstl-imaging-phase1.tar.gz`
    SHA256 `adf01371f31ae6d41c2f997da0004bbf2a080ca33f26b2840992e04916f22261`
  - `dist/vstl-imaging-server_20260610_211440.tar.gz`
  - `dist/vstl-imaging-server_current.tar.gz`
    SHA256 `451cabca81c308a8ceb2f4ef068bca5d182d69213491ebbe73d2c4d4495a338c`
- Refreshed server archives:
  - `/opt/vstl-backups/vstl-imaging-server_current.tar.gz`
    SHA256 `451cabca81c308a8ceb2f4ef068bca5d182d69213491ebbe73d2c4d4495a338c`
  - `/opt/vstl-backups/vstl-imaging-setup-20260610_211655.tar.gz`
  - `/opt/vstl-backups/vstl-imaging-setup-current.tar.gz`
    SHA256 `e4717438442e373033e9147e53bcdd453a394e7220af0bfa296dbab3e286c49a`

## PXE No Second DHCP Shim 2026-06-10

- Fresh laptop attempt after the direct-HTTP change proved iPXE received:
  `http://10.255.0.75/vstl-pxe/boot.ipxe`.
- The laptop still did not request Apache because FOG's `snponly.efi` contains
  an embedded chain to `tftp://${next-server}/default.ipxe`, and the old TFTP
  shim ran a second DHCP request.
- Updated the live TFTP shims:
  - `/tftpboot/default.ipxe`
  - `/tftpboot/autoexec.ipxe`
  - `/var/www/html/vstl-pxe/autoexec.ipxe`
- The shim now reuses the firmware PXE network lease and chains directly to
  `http://10.255.0.75/vstl-pxe/boot.ipxe` without calling `dhcp`.
- Updated `06_setup_pxe_netboot.sh` so future PXE setup regenerates the same
  no-second-DHCP shims.
- Kept iPXE DHCP option 67 as the HTTP URL and restored option 66/150 metadata
  for this iPXE build.
- Live server backup:
  `/opt/vstl-backups/pxe-no-second-dhcp-20260610_213348`.
- Verification:
  - focused PXE tests: `10 passed`
  - server `bash -n /opt/vstl-imaging-phase1/06_setup_pxe_netboot.sh` passed
  - dnsmasq syntax check passed
  - `dnsmasq` active after restart
- Refreshed packages:
  - `imaging_server/dist/vstl-imaging-phase1.tar.gz`
    SHA256 `383b3ddb97b930c68bfe60dba00edab9662e5b502774b38e3b2ad39487d24272`
  - `dist/vstl-imaging-server_20260610_213753.tar.gz`
  - `dist/vstl-imaging-server_current.tar.gz`
    SHA256 `26a22a0d7a06cd08c1e894d2de4978bd4f78604325c84d4f737e271952c19869`
- Refreshed server archives:
  - `/opt/vstl-backups/vstl-imaging-server_current.tar.gz`
    SHA256 `26a22a0d7a06cd08c1e894d2de4978bd4f78604325c84d4f737e271952c19869`
  - `/opt/vstl-backups/vstl-imaging-setup-20260610_213827.tar.gz`
  - `/opt/vstl-backups/vstl-imaging-setup-current.tar.gz`
    SHA256 `eb52f841545c8a30040b0664843b1efd302298d64fa9783ce74c6d2a5981a63a`

## PXE Clean iPXE Loader Pin 2026-06-10

- Fresh server logs after the no-second-DHCP shim showed the Latitude 5490
  still received FOG's `snponly.efi` as the first-stage loader.
- Root cause: `/tftpboot/snponly.efi` has an embedded FOG script that runs its
  own DHCP step and then tries `tftp://${next-server}/default.ipxe`. On the
  old Dell UEFI path this prevented the client from reaching Apache for
  `/vstl-pxe/boot.ipxe`.
- Removed the old-Dell dnsmasq override that forced `snponly.efi`.
- Pinned the live server to the clean full iPXE loader:
  `VSTL_UEFI_BOOTFILE=vstl-clean-ipxe.efi`.
- Active dnsmasq now offers:
  - first-stage UEFI bootfile: `vstl-clean-ipxe.efi`
  - iPXE boot script: `http://10.255.0.75/vstl-pxe/boot.ipxe`
- Updated the packaged `.env.example` defaults for future installs:
  - `SERVER_IP=10.255.0.75`
  - `BENCH_SUBNET=10.255.0.0/24`
  - `BENCH_GATEWAY=10.255.0.1`
  - `BENCH_DNS=10.255.0.75`
  - `VSTL_PXE_DHCP_START=10.255.0.244`
  - `VSTL_PXE_DHCP_END=10.255.0.247`
  - `VSTL_UEFI_BOOTFILE=vstl-clean-ipxe.efi`
- Live server backups:
  - `/opt/vstl-backups/pxe-server-ip-env-fix-20260610_220502`
  - `/opt/vstl-backups/pxe-pin-clean-ipxe-env-20260610_220617`
- Verification:
  - focused PXE tests: `24 passed`
  - dnsmasq syntax check passed
  - `dnsmasq`, `apache2`, and `tftpd-hpa` active
  - `http://10.255.0.75/vstl-pxe/boot.ipxe` returns HTTP 200
  - server free space after backup: approximately `3.0T`
- Refreshed packages:
  - `imaging_server/dist/vstl-imaging-phase1.tar.gz`
    SHA256 `bb8fec86e1d7765c8807084b44881736789734cab8640aa462a02419f1b62099`
  - `dist/vstl-imaging-server_20260610_221506.tar.gz`
  - `dist/vstl-imaging-server_current.tar.gz`
    SHA256 `2684ddabe1c0d1f33feccdc8dfcd2ea3ed86e78f1fcc29b74883518a92f34205`
- Refreshed server archives:
  - `/opt/vstl-backups/vstl-imaging-server_current.tar.gz`
    SHA256 `2684ddabe1c0d1f33feccdc8dfcd2ea3ed86e78f1fcc29b74883518a92f34205`
  - `/opt/vstl-backups/vstl-imaging-server_current_clean_ipxe_loader_20260610_221620.tar.gz`
    SHA256 `2684ddabe1c0d1f33feccdc8dfcd2ea3ed86e78f1fcc29b74883518a92f34205`
  - `/opt/vstl-backups/vstl-imaging-setup-20260610_221650.tar.gz`
  - `/opt/vstl-backups/vstl-imaging-setup-current.tar.gz`
    SHA256 `e8618a893ff391c052b1759907fe28114e598265092aab47c77b7557fbabff66`

## Bootable USB Network Support 2026-06-11

- Added `bench-client/vstl_network_setup.py` to prepare networking before the
  L1/L2 interface starts.
- Network selection order:
  - keep an existing connection when it can reach the server
  - try every physical wired interface, including USB Ethernet
  - offer an interactive Wi-Fi SSID/password menu
  - allow explicit offline mode
- Wi-Fi credentials are entered at boot and are not stored in the ISO.
- Added `04_build_usb_iso.sh` to rebuild a bootable hybrid USB image from the
  current VSTL live ISO while injecting the latest runtime and configuration.
- Added `USB_BOOT.md` with Rufus/balenaEtcher and operator instructions.
- Built and verified:
  - local ISO: `dist/vstl-usb-live-amd64.iso`
  - server ISO:
    `/opt/vstl-imaging-phase1/build/vstl-usb-live-amd64.iso`
  - size: `644153344` bytes
  - SHA256:
    `bc4091351032b6e08e49fa8611a0e4a9b2a00a53f401e7b8473a34ccfdc76d43`
- ISO verification:
  - BIOS El Torito boot image: `/syslinux/isolinux.bin`
  - UEFI El Torito boot image: `/boot/grub/efi.img`
  - embedded `VSTL_SERVER_IP="10.255.0.75"`
  - embedded network helper and bench entry scripts are executable
- Tests:
  - USB/network focused tests: `17 passed`
  - complete local suite: `120 passed, 1 pre-existing Windows-only synthetic
    Intune task-path test failed`
- Final artifact verification:
  - focused runtime/archive tests: `14 passed`
  - `vstl_network_setup.py` Python compile passed
  - `dnsmasq`, `apache2`, and `tftpd-hpa` are active
  - `http://10.255.0.75/vstl-pxe/boot.ipxe` returns HTTP 200
- Refreshed local packages:
  - `imaging_server/dist/vstl-imaging-phase1.tar.gz`
    SHA256 `5691eeb4db52a156b38febd6fa8cc42e99281c9faf331d08da732c063d3d87f8`
  - `dist/vstl-imaging-server_20260611_195123.tar.gz`
  - `dist/vstl-imaging-server_current.tar.gz`
    SHA256 `d33c0273c744c26a09c6176aacfe00e88ecc937dcc00a6d87f0f4134d36fdab4`
- Refreshed server backups:
  - `/opt/vstl-backups/vstl-imaging-server_current.tar.gz`
  - `/opt/vstl-backups/vstl-imaging-server_current_usb_boot_20260611_195123.tar.gz`
  - `/opt/vstl-backups/vstl-usb-live-amd64.iso`
  - `/opt/vstl-backups/vstl-usb-live-amd64_20260611_195123.iso`
- The server ISO backup paths are hard links to the build artifact and therefore
  do not consume duplicate ISO storage.

## USB Direct VSTL Startup 2026-06-11

- The first USB ISO preserved Clonezilla's visible GRUB menu even though its
  live runtime was configured for VSTL.
- Updated `04_build_usb_iso.sh` to replace all active USB boot configurations:
  - UEFI: `/boot/grub/grub.cfg`
  - BIOS ISO mode: `/syslinux/isolinux.cfg`
  - BIOS USB/DD mode: `/syslinux/syslinux.cfg`
- UEFI now has one hidden `VSTL 360 Bench Imaging` entry with a zero-second
  timeout.
- BIOS now has one non-interactive `vstl` entry with no menu prompt.
- Both entries launch `/opt/vstl/vstl-bench-entry.sh` directly.
- Rebuilt ISO:
  - `dist/vstl-usb-live-amd64.iso`
  - size: `644153344` bytes
  - SHA256:
    `5701b9a3449a44b535945fd50455b6824811507d58243077a5642f630b3e03f0`
- Verification:
  - direct-start configuration extracted and checked from the completed ISO
  - Clonezilla menu text is absent from all active boot configuration files
  - BIOS and UEFI El Torito entries remain present
  - focused tests: `15 passed`
- Refreshed packages:
  - `imaging_server/dist/vstl-imaging-phase1.tar.gz`
    SHA256 `b748405d7267729f43a2e29cb092043f2ed785cb2bc47d5453c798c6343a636d`
  - `dist/vstl-imaging-server_20260611_202215.tar.gz`
  - `dist/vstl-imaging-server_current.tar.gz`
    SHA256 `a41cf718eadf0afa26a78bbf372de545a07205ac31ebe498e6b0819be46594fb`
- Server backups:
  - `/opt/vstl-backups/vstl-imaging-server_current_usb_direct_20260611_202215.tar.gz`
  - `/opt/vstl-backups/vstl-usb-live-amd64_direct_20260611_202215.iso`
# 2026-06-11 - Touch Result, Accurate Port Inventory, and HP UEFI PXE

- Touch-map rendering now stays under curses ownership by default, eliminating framebuffer/console interference lines.
- Touch QC now shows an explicit three-second PASS/FAIL result screen.
- Removed guessed laptop port profiles. Port rows are derived from SMBIOS external connectors, Type-C sysfs, and DRM.
- A PCI Ethernet controller alone no longer creates a physical RJ45 test row.
- Port credit now requires individual plug/replug transitions; already-connected audio, Ethernet, display, and power states do not auto-pass.
- 64-bit UEFI PXE now uses `snponly.efi` to avoid the HP `iPXE initialising devices` native-driver hang.
- Rebuilt and published the PXE squashfs at `http://10.255.0.75/vstl-pxe/`.
- Rebuilt the direct VSTL USB ISO and copied it to `D:\win\vstl-usb-live-amd64.iso`.
- USB ISO SHA-256: `2ba66ae47105a81c82279ddb51b9ed99ba51eebd6176f002094c6dcdea224793`.

# 2026-06-12 - PXE Raw CPIO Default and Separated Touch Grid

- Production PXE was changed from named gzip initrd to `classic-raw-cpio`.
- Active server `boot.ipxe` now shows `VSTL VERIFIED RAW CPIO BOOT`.
- Active server kernel line does not contain `initrd=initrd.img`.
- Active server initrd line loads `http://10.255.0.75/vstl-pxe/initrd-vstl-fixed.cpio`.
- This is intended to avoid HP/Dell UEFI PXE panics with `Initramfs unpacking failed: invalid magic at start of compressed archive`.
- Touch-map curses rendering now reserves a title/progress row and footer row, hides the cursor during the grid, and draws blocks with explicit gaps.
- Focused local tests: `21 passed`.
- Server PXE HTTP verification passed for `boot.ipxe`, `vmlinuz`, `initrd-vstl-fixed.cpio`, and `filesystem.squashfs`.
- Updated USB ISO:
  - `D:\Projects\VSTL Server\dist\vstl-usb-live-amd64.iso`
  - `D:\Projects\VSTL Server\vstl-usb-live-amd64.iso`
  - `D:\win\vstl-usb-live-amd64.iso`
  - SHA256 `e9042b1ff09ff3f943f97da30bf36d096ec760f24b362a6631c4e69521e3961f`
- Updated current package:
  - `D:\Projects\VSTL Server\dist\vstl-imaging-server_current.tar.gz`
  - SHA256 `02d4c213fab06cf6fc51a0cf2df1fa5879c03d7ce46bf7a5fa81b4b9d8c46549`
- Server backups refreshed:
  - `/opt/vstl-backups/vstl-imaging-server_current.tar.gz`
  - `/opt/vstl-backups/vstl-usb-live-amd64.iso`

# 2026-06-13 - AX201 Wi-Fi, USB Audio Parity, Ports, and Keyboard Result

- Fixed the live ISO dependency builder:
  - Debian packages now use the Debian 13 `trixie` CDN over IPv4.
  - Chroot mounts are always cleaned before SquashFS creation.
  - Required firmware/package failures stop the build instead of publishing a partial image.
- Added `firmware-iwlwifi`, `iwlwifi`, and `iwlmvm` initialization for Intel AX201 and related adapters.
- Verified the deployed SquashFS contains 81 Intel Wi-Fi firmware files.
- USB and PXE images now share the same complete QC runtime, including speaker and microphone tests.
- Microphone capture rejects silent ALSA paths and chooses the strongest valid recording before playback.
- Keyboard QC now shows a PASS/FAIL result for at least three seconds and supports `R` to retest.
- Port inventory now prefers kernel USB connector topology and asks the technician to confirm physical
  connector counts before individual plug/replug testing. This avoids presenting guessed HDMI, audio,
  RJ45, or USB connector counts as detected hardware.
- PXE root filesystem deployed without changing the validated kernel, initrd, or iPXE boot-chain files.
- PXE SquashFS SHA256:
  `9aae7dd8e21bb58a5be9ab9a300c08747b161a0492947a90934cfad0b9e9257b`
- Updated USB ISO copied to:
  - `D:\Projects\VSTL Server\vstl-usb-live-amd64.iso`
  - `D:\Projects\VSTL Server\dist\vstl-usb-live-amd64.iso`
  - `D:\win\vstl-usb-live-amd64.iso`
- USB ISO SHA256:
  `09165d17232b774074cf312bd2c038385ba08b7b822a3c9de6ffb7cb765a718c`
- Local verification: `142 passed`.

# 2026-06-14 - Dell 5490 PXE Panic, Report Ordering, and Mirrored Camera

- Reviewed `20260614-1424-55.4139589.mp4`.
- The affected Latitude 5490 used MAC `10:65:30:66:c1:3b` and was loading
  iPXE `1.0.0+git-20190125`. That obsolete loader downloaded the kernel/initrd
  but did not complete the live root filesystem handoff, causing:
  `VFS: Unable to mount root fs on unknown-block(0,0)`.
- Changed the matching `10:65:30:*:*:*` Dell UEFI route from
  `vstl-clean-ipxe.efi` to the current `/tftpboot/intel.efi` loader.
- The current loader chains `/tftpboot/default.ipxe`, which retries the VSTL
  HTTP boot script at `http://10.255.0.75/vstl-pxe/boot.ipxe`.
- Kept normal 64-bit UEFI clients on `snponly.efi`.
- Report columns now place:
  - `RAM Type` immediately after `Total RAM (GB)`.
  - `Storage Type` immediately after `Storage Capacity (GB)`.
  - designed/current battery capacities immediately after `Battery Health (%)`.
- RAM type, storage type, and battery capacity result cells are centered
  horizontally and vertically in XLSX exports.
- Camera QC is horizontally mirrored in the framebuffer, FFmpeg, mpv, and
  ffplay preview paths.
- Rebuilt and deployed the PXE SquashFS:
  `91c5a7742f0fac7dfdba367137e7f0a246bd04bbd3b0e88463fc92c636856665`.
- Rebuilt USB ISO and copied it to the project, `dist`, and `D:\win`:
  `44f64c330e8d7e28743118093ece0a8a79d0b3dc6c4606ef2f8cc4e18769f53c`.
- Rebuilt the rolling source package:
  `D:\Projects\VSTL Server\dist\vstl-imaging-server_current.tar.gz`
  SHA256 `3177c0b335b0b6681a55a37c527de235376169de518de95ce4d8366e1f8259c7`.
- Refreshed server backups:
  - `/opt/vstl-backups/vstl-imaging-server_current.tar.gz`
  - `/opt/vstl-backups/vstl-usb-live-amd64.iso`
- Verification: `151 passed`; local/server Python and server Bash syntax checks
  passed; `dnsmasq`, `apache2`, and `tftpd-hpa` are active.

# 2026-06-14 - Visible Report Borders and Complete Camera Mirroring

- Report headers now use medium black enclosed borders.
- Every populated report value cell now uses thin black enclosed borders.
- Confirmed report column order:
  - `RAM Type` immediately follows `Total RAM (GB)`.
  - `Storage Type` immediately follows `Storage Capacity (GB)`.
  - battery designed/current capacities immediately follow `Battery Health (%)`.
- RAM type, storage type, and both battery capacity values are centered
  horizontally and vertically.
- Added horizontal mirroring to the remaining camera fallback paths, including
  `fswebcam`, legacy `mpv`, and legacy `ffplay`.
- Verification:
  - Full local test suite: `151 passed`.
  - Reporting endpoint: HTTP `200`.
  - `dnsmasq`, `apache2`, and `tftpd-hpa`: active.
  - Local source, installed reporting exporter, PXE embedded TUI, and USB
    embedded TUI hashes match.
- PXE SquashFS SHA256:
  `32ab2e1d9fcea2ff92074192e5d848090fba0f82e6f682a5a0d91837e005ef60`.
- USB ISO SHA256:
  `85d2caa267dafb697e89f1c32f4a8f4bda3443b6ad9e1848c3a73ad27626a148`.
- Updated USB ISO copied to:
  - `D:\Projects\VSTL Server\vstl-usb-live-amd64.iso`
  - `D:\Projects\VSTL Server\dist\vstl-usb-live-amd64.iso`
  - `D:\win\vstl-usb-live-amd64.iso`
- Rolling source package SHA256:
  `6f0d8744bdae9cc4809393de746e2c30ce00ee9fbbb9151368ff012835435330`.
- Refreshed server backups:
  - `/opt/vstl-backups/vstl-imaging-server_current.tar.gz`
  - `/opt/vstl-backups/vstl-usb-live-amd64.iso`
