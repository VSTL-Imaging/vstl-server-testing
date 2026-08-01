# VSTL Server Codex Laptop Sync Handoff

Last updated: 2026-08-01

Use this file when opening the project from another laptop or another Codex app. GitHub is the shared source of truth so both laptops see the same project instructions, server logic notes, and update history.

## Emergent AI Full Setup Report - Read First

This file is the current high-level handoff for Emergent AI, Codex, or any other AI coding tool working on the VSTL imaging setup. It explains the full operating model, server roles, networking/PXE behavior, VSTL Bench workflow, cloud-app contracts, reporting expectations, and the rules for testing versus main deployment.

### Current Operating Model

VSTL has two imaging server environments:

- Main/original server: `10.255.0.75`
- Testing server: `10.255.0.45`

The main server is the live production system. Do not change it during investigation or experimentation. The testing server is where updates must be installed first, tested, and approved. Only after the user explicitly says `deploy main` should the approved change be applied to `10.255.0.75`.

### Server Responsibilities

The VSTL server setup supports these end-to-end processes:

- IPv4 PXE boot into the VSTL Bench screen.
- Restore OS images to laptops.
- Run L1/L2 QC tests.
- Run secure erase / sanitize workflows and issue local/server certificates.
- Capture full system images after a valid certified secure erase.
- Save local server reports.
- Submit audit/report data to the VSTL 360 cloud app.
- Export report sections for Restore, QC, Secure Erase, and Capture.

### VSTL 360 / Cloud App Relationship

VSTL 360 is the cloud system of record for user identity, role/permission logic, imaging audit ingest, box allocation, and cloud reports. The bench client must treat cloud-provided fields as authoritative rather than recreating the rules locally.

Important cloud-facing behavior:

- Bench login uses VSTL 360 imaging auth endpoints.
- Bench ingest sends the existing `X-API-Key` plus the operator bearer token when available.
- `can_capture` from the auth response controls whether `Capture Full System Image` is visible.
- The client must not decide Capture visibility using `is_admin` alone.
- The backend-owned Capture rule is: `can_capture = is_admin OR ("Imaging" in roles)`.
- Imaging-only users may have `layer: null`; the bench must not block Secure Erase or Capture only because layer is missing.
- L1 and L2 users are allowed to select either L1 or L2 workflow; only Capture remains permission-gated.

### Box Picker Contract

The bench box picker must use:

```http
GET /api/imaging/my-boxes
Authorization: Bearer <imaging session token>
```

Required behavior:

- Populate the box picker only from `boxes[]` in `/api/imaging/my-boxes`.
- Do not show all boxes as a fallback.
- If `boxes: []`, show: `No boxes assigned to you. Ask your supervisor to allocate a box.`
- Re-fetch `/my-boxes` after every successful `/api/imaging/ingest`.
- Render the model using `model_label`, not by concatenating `brand + model`.
- Keep sending the selected `lot_no` and `box_no` on ingest.

### Report Requirements

All report sections must be sorted newest-to-oldest. This applies to:

- Restore
- QC
- Secure Erase
- Capture

Reports should include, wherever the payload provides it:

- Operation, status, date, time, UAE timestamp
- Technician level
- Logged-in `User`
- Bench ID
- Serial number
- SKU / product number
- MAC ID
- Model name
- CPU/GPU
- RAM module information
- Storage device information
- Battery designed capacity, full charged capacity, current capacity, and health
- BIOS and OS fields
- Restore / QC / Secure Erase / Capture elapsed time
- Secure erase certificate ID and method
- QC results, including fingerprint/audio/battery/stress/fan data
- Cosmetic Grade
- Parts Required
- Additional Remarks

Battery health should follow the BatteryInfoView-style calculation using designed capacity and full charged capacity. Whole percentage display is required: examples `79.3% -> 79%`, `79.9% -> 79%`.

Report timestamps should be in UAE time, `Asia/Dubai`, unless a specific UTC audit/debug field is intentionally shown.

### Secure Erase Policy

The user does not want weak clear methods used as secure erase fallback. Specifically, do not use these methods, even as fallback:

- `NVMe_SOFTWARE_ZERO_CLEAR`
- `NVMe_FORMAT_USER_DATA`

Preferred trustable purge-class methods are controller/firmware-supported sanitize or secure erase methods, for example:

- `NVMe_SANITIZE_BLOCK_ERASE`
- `NVMe_SANITIZE_CRYPTO_ERASE`
- `NVMe_FORMAT_CRYPTO`
- ATA Security Erase / Enhanced Security Erase where supported by the drive

If a laptop/SSD controller rejects all trusted purge methods, the bench should fail clearly and explain that the controller rejected the advertised native purge command. It should not silently downgrade to a weak clear method and mark it as trusted purge.

Capture readiness requires a certified secure erase record matching the laptop serial and storage device. If capture fails with “No certified secure-erase record exists for this laptop serial and storage device,” inspect the local/server secure erase history matching logic rather than bypassing the check.

### PXE / Networking Notes

Main network observed:

- Main server: `10.255.0.75`
- Testing server LAN: `10.255.0.45`
- Testing server Tailscale observed: `100.76.131.61`
- Omada controller observed on testing server: `https://100.76.131.61:8043`

Testing bench network:

- Bench/PXE test subnet has been used as `10.45.0.0/24`.
- Testing server PXE address has been used as `10.45.0.45` on the VSTL bridge/interface when active.
- Avoid running duplicate DHCP/PXE services on the same physical network as the main server unless the network is intentionally isolated.

Known Type-C Ethernet / PXE issue pattern:

- Built-in Ethernet usually boots into VSTL Bench correctly.
- Some Type-C Ethernet adapters boot iPXE but hang after initrd load or fail to find the live filesystem.
- The fix should be global, not per-laptop model: stable NIC handoff from iPXE to Linux, enough DHCP wait/retry time, correct boot interface selection, and driver/module support for common USB/Type-C NIC chipsets.

Switch/fiber guidance:

- Avoid physical switch loops unless STP/LACP is intentionally configured and verified.
- Prefer direct uplinks from the server room/core switch to each rack switch.
- HPE 10Gb SR SFP+ modules are multimode optics and should use multimode fiber, usually aqua OM3/OM4.
- Yellow single-mode fiber normally requires LR/single-mode optics on both ends.
- Single-mode and multimode do not change the negotiated speed by themselves. If both sides are 10G optics and the fiber/module type matches, the link is 10G. The difference is distance, fiber type, wavelength, and compatibility.

### Omada / Switch Management

Omada is installed on the testing server for TP-Link/Omada switch/AP management. Open it from a browser using the testing server address and Omada HTTPS port, for example:

```text
https://100.76.131.61:8043
```

Use Omada for supported TP-Link/Omada devices. Cisco/Aruba switch console or firmware work may require vendor-specific console/CLI access and is not the same as Omada management.

### GitHub Sync Rules For AI Tools

Before any AI tool starts editing:

1. Read this handoff file completely.
2. Pull the latest GitHub state for the correct branch.
3. Inspect `git status --short --branch`.
4. Do not overwrite or reset user/server changes without approval.
5. Make the change.
6. Test or verify what is practical.
7. Commit and push the correct repo/branch.
8. Report the commit hash and exactly what was changed.

Never commit:

- Passwords
- Tokens
- API keys
- `.env` files
- ISO files
- Raw disk images
- Large backups
- KVM screenshots/videos or temporary media unless the user explicitly asks for them

Credentials must be shared out-of-band by the owner. This handoff intentionally does not store live passwords.

## 1. GitHub Account And Repositories

GitHub organization/account:

- `VSTL-Imaging`

Login email / username:

- `ai@vstl.ae`

Important credential rule:

- Do not save the GitHub password in this file, in Git, or in any project document.
- GitHub normally does not accept account passwords for Git push. Use browser authentication, Git Credential Manager, or a GitHub Personal Access Token.
- If a password or token was pasted into chat or saved in a file, rotate it from GitHub immediately.

Repositories:

- Original/main server repo: `https://github.com/VSTL-Imaging/vstl-server-original.git`
- Testing server repo: `https://github.com/VSTL-Imaging/vstl-server-testing.git`

Branches:

- `main` tracks `original/main`
- `testing` tracks `testing-remote/testing`

## 2. Server Roles

Original/main server:

- IP: `10.255.0.75`
- Role: production server / original live setup.
- Policy: do not deploy changes here until testing is approved and the user says `deploy main`.

Testing server:

- IP: `10.255.0.45`
- Role: test server for validating changes before main deployment.
- Network note: this server is intended to use the fiber / 10G SFP+ testing setup where available.
- Policy: apply and validate risky server, PXE, bench, network, report, and QC changes here first.

Workflow rule:

- Test on `10.255.0.45` first.
- Deploy to `10.255.0.75` only after approval.

## 3. What This Project Contains

This repository is the handoff and source-control package for the VSTL imaging server work. It includes server setup notes, bench/PXE instructions, helper scripts, and documentation needed for Codex or another AI coding tool to understand the environment.

Common areas:

- `imaging_server/bench-client/`: VSTL Bench client logic, QC flow, restore/capture/secure erase related client code.
- `imaging_server/reporting/`: local report/export related logic.
- `imaging_server/tools/`: server helper scripts and operational tools.
- `imaging_server/*.md`: setup, recovery, deployment, PXE, and handoff documents.
- `GITHUB_SETUP.md`: short GitHub usage guide.
- `tools/push-to-github.ps1`: PowerShell push helper.
- `tools/push-main-to-github.cmd`: Windows launcher for pushing main/original changes.
- `tools/push-testing-to-github.cmd`: Windows launcher for pushing testing changes.

Ignored / not synced by GitHub:

- `.env` and live secrets
- API keys, passwords, tokens
- ISO files, `.img`, `.qcow2`, `.vhd`, `.vhdx`
- large backup/archive files
- local logs and reports unless intentionally added
- downloaded tools and raw media captures

## 4. First Setup On Another Laptop

Open PowerShell and run:

```powershell
mkdir D:\Projects -ErrorAction SilentlyContinue
cd D:\Projects
git clone https://github.com/VSTL-Imaging/vstl-server-original.git "VSTL Server"
cd "D:\Projects\VSTL Server"
git remote rename origin original
git remote add testing-remote https://github.com/VSTL-Imaging/vstl-server-testing.git
git fetch testing-remote
git checkout -B testing testing-remote/testing
git switch main
git status --short --branch
```

If Git asks for login, complete browser sign-in or use Git Credential Manager / a Personal Access Token.

## 5. Mandatory Codex Startup Rule

Before Codex starts any edit, change, deployment, or investigation on another laptop, it must pull the latest GitHub state first.

For main/original work:

```powershell
cd "D:\Projects\VSTL Server"
git switch main
git pull original main
git fetch testing-remote
git status --short --branch
```

For testing-server work:

```powershell
cd "D:\Projects\VSTL Server"
git switch testing
git pull testing-remote testing
git fetch original
git status --short --branch
```

If there are local uncommitted changes, Codex must inspect them before editing. Do not delete or reset user changes without explicit approval.

## 6. Push Changes After Every Approved Update

Use the `.cmd` launchers because some Windows systems block direct `.ps1` execution.

Push original/main server documentation or code:

```cmd
cd /d "D:\Projects\VSTL Server"
tools\push-main-to-github.cmd "Describe the change here"
```

Push testing server documentation or code:

```cmd
cd /d "D:\Projects\VSTL Server"
tools\push-testing-to-github.cmd "Describe the change here"
```

Alternative PowerShell command if needed:

```powershell
cd "D:\Projects\VSTL Server"
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\push-to-github.ps1 -MainServer -Message "Describe the change here"
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\push-to-github.ps1 -TestingServer -Message "Describe the change here"
```

After pushing, confirm with:

```powershell
git status --short --branch
git log -1 --oneline
```

## 7. Automatic Sync Expectation

GitHub sync is not a background automatic sync unless a separate automation is created later.

Current required behavior:

1. Pull from GitHub before work.
2. Make the change.
3. Commit with a clear message.
4. Push to the correct GitHub repo/branch.
5. Tell the user the commit hash and which repo was updated.

Do not create an automatic scheduled push that commits everything blindly. That can leak secrets, upload large images/ISOs, or publish unfinished work.

## 8. Testing Vs Main Deployment Logic

Testing logic:

- Use branch `testing`.
- Push to `testing-remote/testing`.
- Apply server-side changes to `10.255.0.45`.
- Validate PXE boot, VSTL Bench screen, restore, capture, secure erase, QC, reports, and network behavior.

Main/original logic:

- Use branch `main`.
- Push to `original/main`.
- Apply changes to `10.255.0.75` only after the user says `deploy main`.
- Main server must remain stable and should not receive experimental changes.

Recommended Codex instruction for every new task:

```text
First pull the latest GitHub state for the correct branch. If this is a testing change, work on testing and push to vstl-server-testing. If I say deploy main, push/apply only the approved change to main/original.
```

## 9. Handoff Checklist For Another Codex App

When opening this project on another laptop, Codex should:

1. Read this file completely.
2. Read `GITHUB_SETUP.md`.
3. Run `git remote -v`.
4. Run `git status --short --branch`.
5. Pull the latest branch before doing work.
6. Avoid committing secrets and large binary artifacts.
7. Push after every approved change.
8. Report the updated repo, branch, and commit hash.

## 10. Quick Troubleshooting

PowerShell script blocked:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\push-to-github.ps1 -MainServer -Message "Describe change"
```

Wrong branch:

```powershell
git branch --show-current
git switch main
git switch testing
```

Check remote setup:

```powershell
git remote -v
```

Pull latest safely:

```powershell
git status --short
git pull --ff-only
```

If `git pull --ff-only` fails, stop and ask before merging or rebasing.
