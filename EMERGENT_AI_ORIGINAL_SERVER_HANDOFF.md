# VSTL 360 Imaging - Original Server Handoff For Emergent AI

Last updated: 2026-08-05

This document is the original/main-server-only handoff for Emergent AI or another AI coding tool. It describes the production imaging server setup only. It intentionally does not document the testing server setup.

## Latest Update For Emergent AI - 2026-08-05

### Operator Attribution / EXPIRED_UNVERIFIED

- The 72h token TTL is for the operator login/session only. It is not a DHCP/IP lease, it is not per-unit, and it does not reserve IP addresses.
- `EXPIRED_UNVERIFIED` can still appear after the 72h change when the upload uses an old cached 24h token, production was not redeployed at the time of upload, an existing historical row has not been cleaned, or the cloud can decode the JWT but cannot trust the backing session because it is revoked, missing, or too old.
- When the bench API key is valid, the physical unit scan should still be accepted and stored. This status is about operator credit/attribution review, not about whether the unit exists.
- Recommended answer to Emergent: proceed with the bulk `Confirm all detected operators` button and recurrence fix. Auto-attribute missing/aged sessions when the token is validly signed and the detected operator is unambiguous, while keeping true logout/switch-user/handoff cases for manual review.
- Bench-side requirement remains: after redeploy, every bench operator should `Logout / Switch User -> Login` again to obtain a fresh token. The bench queue flush must always attempt `POST /api/imaging/ingest` even if the local token looks expired. Dequeue only on HTTP 200.

### MAC ID / Type-C Ethernet Adapter Policy

- Report MAC policy: use the inbuilt LOM MAC first. If no LOM exists, use the BIOS/dock passthrough MAC as fallback. Never store a Type-C Ethernet adapter's own MAC address in reports.
- Current bench code should reject known external USB/Type-C adapter MACs and report `UNKNOWN` rather than storing the adapter MAC.
- Latest Dell no-LOM test: BIOS showed pass-through MAC `B4:45:06:4D:DA:A8`, but Linux bench diagnostics did not expose it in `dmidecode -t 11` or raw `/sys/firmware/dmi/tables/DMI`; the searched DMI strings contained Dell OEM fields only. In that state, the Linux bench cannot automatically read the passthrough value from DMI.
- If the desired value must be reported for these machines, the reliable fix is either:
  - enable BIOS MAC Address Pass-Through and use a supported Dell dock/adapter that presents the system passthrough MAC to PXE/Linux, or
  - add a controlled manual fallback prompt when LOM/DMI passthrough is missing. The prompt should validate the operator-entered BIOS pass-through MAC, normalize it to uppercase colon format, mark source as `manual_passthrough`, and still reject Type-C adapter MACs.

### Latest Main Bench Deployment

- Current original repo HEAD: `5dd903a Detect passthrough MAC from raw DMI`.
- Recent original-server bench identity commits:
  - `5555e7d Use LOM MAC for bench identity`
  - `e9fefd8 Prefer BIOS passthrough MAC for Type-C PXE`
  - `e67e276 Parse split BIOS passthrough MAC labels`
  - `5dd903a Detect passthrough MAC from raw DMI`
- Last known main deployment package: `dist\vstl-main-deploy-5dd903a-20260805_134957.tar.gz`.

## 1. Original Server Identity

Original/main server:

- Server IP: `10.255.0.75`
- Web/server UI: `http://10.255.0.75`
- Role: production VSTL 360 imaging server
- GitHub repo: `https://github.com/VSTL-Imaging/vstl-server-original.git`
- Git branch: `main`
- Git remote name: `original`

Credential rule:

- Do not store server passwords, GitHub passwords, API keys, tokens, or `.env` values in this file or in Git.
- Credentials must be requested from the VSTL owner and shared out-of-band.

## 2. Production Safety Rule

This server is the live/original system.

Do not apply experimental changes directly to `10.255.0.75`.

Any code, PXE, report, secure erase, network, QC, or bench workflow change must be reviewed and approved before touching this production server. When the user explicitly says `deploy main`, apply only the approved change to the original server.

## 3. Main Server Functions

The original server supports the full VSTL imaging workflow:

- IPv4 PXE boot into the VSTL Bench screen.
- Restore captured OS images to laptops.
- Run L1/L2 audit and QC workflows.
- Run secure erase / sanitize workflows.
- Generate local secure erase certificates.
- Submit audit and report payloads to the VSTL 360 cloud app.
- Capture full system images after certified secure erase.
- Generate local/server reports for Restore, QC, Secure Erase, and Capture.

## 4. VSTL Bench Workflow

Typical production flow:

1. Laptop boots by IPv4 PXE from the original server network.
2. VSTL Bench screen loads.
3. Operator logs in using VSTL 360 imaging login/PIN flow.
4. Operator selects the permitted process.
5. Bench runs Restore, QC, Secure Erase, or Capture.
6. Bench saves local report data.
7. Bench submits audit/report payload to VSTL 360 cloud.
8. End screen should show `ENTER restart system`, not power off, where this behavior is configured.

## 5. VSTL 360 Cloud Auth And Permissions

VSTL 360 is the authority for user identity and permissions.

Bench auth/ingest behavior:

- Bench must keep sending the existing `X-API-Key`.
- Bench should also send `Authorization: Bearer <imaging session token>` after login.
- Operator session token TTL is currently 72h after a fresh login. This is a human login token only; it does not reserve DHCP/IP addresses.
- The cloud stamps the verified operator into reports as `User`.
- The bench must not trust manually typed technician names over the authenticated cloud user.

Capture visibility:

- Use the cloud-provided `can_capture` field.
- Do not derive Capture access locally from `is_admin` alone.
- Backend-owned rule: `can_capture = is_admin OR ("Imaging" in roles)`.
- Admin users can see Capture.
- Users with the Imaging role can see Capture.
- L1/L2-only users should not see Capture.
- Imaging-only users may have `layer: null`; do not block Secure Erase or Capture only because layer is null.

L1/L2 selection:

- L1 and L2 users may use both L1 and L2 workflow selections.
- Only Capture is restricted by permission.

## 6. Box Picker Contract

The bench box picker must use only the logged-in operator's assigned boxes:

```http
GET /api/imaging/my-boxes
Authorization: Bearer <imaging session token>
```

Required behavior:

- Show only boxes returned in `boxes[]`.
- Do not fall back to showing all boxes.
- If `boxes: []`, show: `No boxes assigned to you. Ask your supervisor to allocate a box.`
- After every successful `/api/imaging/ingest`, call `/api/imaging/my-boxes` again.
- Use `model_label` from the response for display.
- Do not concatenate `brand + model` because the cloud already returns a clean label.
- Keep sending selected `lot_no` and `box_no` on ingest.

## 7. Report Requirements

Reports must be available for:

- Restore
- QC
- Secure Erase
- Capture

All report sections should show newest entries first.

Reports should include these fields when available:

- Operation
- Overall status
- Audit Submission Status (`Audit Submitted` / `Submission Failed`)
- Date and time in UAE time
- Technician level
- User
- Bench ID
- Lot number
- Box number
- Box model / model label
- Box total units
- Box imaged units
- Box remaining units
- Serial number
- SKU / product number
- MAC ID
- Model name
- CPU / GPU
- RAM details
- Storage details
- Battery designed capacity
- Battery full charged capacity
- Battery current capacity
- Battery health
- BIOS details
- OS details
- Restore elapsed time
- QC elapsed time
- Secure erase elapsed time
- Capture elapsed time
- Secure erase certificate ID
- Secure erase method
- QC test results
- Fingerprint result where sensor is detected
- Audio/microphone/speaker/headphone results
- Burn/stress test result
- Fan status/RPM if available
- Cosmetic Grade
- Parts Required
- Additional Remarks

Battery health display rule:

- Follow the BatteryInfoView-style capacity calculation.
- Display whole percent only.
- Truncate, do not round up: `79.3% -> 79%`, `79.9% -> 79%`.

Time rule:

- Report user-facing date/time in UAE time: `Asia/Dubai`.

## 8. Secure Erase Policy

The user requires trusted purge-class erase only.

Do not use these methods, even as fallback:

- `NVMe_SOFTWARE_ZERO_CLEAR`
- `NVMe_FORMAT_USER_DATA`
- `NVMe_SECURE_DISCARD_CLEAR`

Allowed/preferred purge-class methods include:

- `NVMe_SANITIZE_BLOCK_ERASE`
- `NVMe_SANITIZE_CRYPTO_ERASE`
- `NVMe_FORMAT_CRYPTO`
- ATA Security Erase
- ATA Enhanced Security Erase

If the controller rejects trusted purge methods, the bench should fail clearly and explain that the SSD/controller rejected the advertised native purge command. Do not silently downgrade to weak clear and call it secure purge.

2026-08-05 update: Clear-class NVMe methods are also removed from certificate/report standard mappings. If one appears in old or local cached data, certificate issuance is refused, the standard is reported as `Unsupported data sanitization method`, and capture remains unauthorized.

Capture readiness must require a matching certified secure erase record for the laptop serial and storage device.

## 9. PXE / Network Behavior

Original production network:

- Main server IP: `10.255.0.75`
- Bench laptops boot by IPv4 PXE on the production imaging network.

Known operational behavior:

- Built-in Ethernet ports are usually more reliable for PXE boot.
- Type-C Ethernet adapters may need special handling because iPXE can load the kernel/initrd, but Linux may later lose the correct boot NIC or fail to fetch the live filesystem.
- The desired fix is global adapter support, not per-model one-off fixes.

Network design guidance:

- Avoid physical switch loops unless STP/LACP is intentionally configured and verified.
- Prefer direct uplinks from the server/core area to each rack switch.
- For 10G fiber, the SFP+ module type and fiber type must match.
- HPE 10Gb SR SFP+ optics use multimode fiber, usually aqua OM3/OM4.
- Yellow single-mode fiber normally requires LR/single-mode optics on both ends.

## 10. GitHub Workflow For Original Server Work

Before starting any original-server work:

```powershell
cd "D:\Projects\VSTL Server"
git switch main
git pull original main
git status --short --branch
```

After an approved original-server documentation/code update:

```cmd
cd /d "D:\Projects\VSTL Server"
tools\push-main-to-github.cmd "Describe the original server change"
```

If PowerShell execution policy blocks `.ps1` scripts, use:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\push-to-github.ps1 -MainServer -Message "Describe the original server change"
```

Do not push original-server-only changes to the testing repo unless the user explicitly asks for the testing repo to receive the same file/change.

## 11. Files To Read With This Handoff

Read these files for additional context:

- `CODEX_LAPTOP_SYNC_HANDOFF.md`
- `GITHUB_SETUP.md`
- `VSTL_FIX_CHECKPOINT.md`
- `imaging_server/*.md`
- `imaging_server/bench-client/`
- `imaging_server/reporting/`
- `imaging_server/tools/`

## 12. Do Not Commit

Never commit these unless the user explicitly asks and the file is intentionally sanitized:

- `.env`
- passwords
- tokens
- API keys
- private certificates
- ISO files
- raw disk images
- backup archives
- temporary KVM screenshots/videos
- large transfer files
