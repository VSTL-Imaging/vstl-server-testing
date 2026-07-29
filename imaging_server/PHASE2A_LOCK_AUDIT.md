# Phase 2A — Lock & MDM/BIOS Audit

**Status**: ✅ Shipped 2026-05-08 (after Phase 1 production validation on the HP ProBook 640 G8 bench).

Phase 2A runs **immediately after the technician selects the menu option** and **before any hardware-detect screens** in the bench TUI. Its only job is to halt the workflow if the unit is locked or managed in a way that would make wipe / image / QC unsafe.

## What is checked (in order)

| # | Check                       | Tool / Method                              | Trust   |
|---|-----------------------------|--------------------------------------------|---------|
| 1 | BIOS Admin Password         | `dmidecode -t 24` (Hardware Security)      | Auto + manual confirm |
| 2 | ATA / NVMe Drive Password   | `hdparm -I` (SATA), `nvme id-ctrl` (NVMe)  | Auto + manual confirm |
| 3 | Computrace / Absolute LoJack| `dmidecode -t 11` (OEM Strings)            | Auto-only — flag if **active/armed** keyword present |
| 4 | Bitlocker Encryption        | `blkid TYPE="BitLocker"` + `-FVE-FS-` signature | Auto-only |
| 5 | Microsoft Intune            | File-path probe: `Windows/System32/Tasks/Microsoft/Windows/EnterpriseMgmt` | Auto-only |
| 6 | Azure AD / Entra Join       | File-path probe: `Windows/System32/Tasks/Microsoft/Windows/Workplace Join` | Auto-only |
| 7 | Vendor MDM                  | File-path probe: Workspace ONE / AirWatch / MobileIron / Hexnode / ManageEngine / IBM MaaS360 / Citrix Secure Hub / SOTI MobiControl / BlackBerry UEM | Auto-only |

Software locks (4–7) are detected by mounting the largest NTFS partition read-only at `/mnt/winprobe` via `ntfs-3g`. The mount is unmounted automatically after the audit.

## Operator flow

```
Technician selection (L1 / L2)
        │
Main menu (auto-select Option 1 in 5s)
        │
[Phase 2A] Running pre-flight lock checks…  ← screen with live ✓ ticks
        │
[Phase 2A] Manual confirm — BIOS Admin Password    ← Y / N (always asked)
        │
[Phase 2A] Manual confirm — Drive Password         ← Y / N (always asked)
        │
        ├─── No locks detected ──→  Lock Audit Complete (3-sec splash)
        │                          ↓
        │                  Hardware-detect screens (Phase 1)
        │                          ↓
        │                    Submit + Power off
        │
        └─── ≥1 lock detected ──→  ⚠ HALT screen
                                   ├─ [O] Admin Override
                                   │      ├─ email + password prompt
                                   │      ├─ POST /imaging/lock-override-verify
                                   │      └─ if ok → Phase 1 + LOCKED-OVERRIDE tag
                                   ├─ [S] Submit halt to server, set unit aside
                                   └─ [Q] Drop to shell (technical bypass — no audit log)
```

## Halt policy (per design choice 1b — "Hard halt + Admin Override")

When ANY of the 7 checks reports `present=True`, the bench refuses to continue silently. The operator gets three explicit choices:

* **O — Admin Override** (Admin or Supervisor only). Their personal email + password are verified server-side via `POST /api/imaging/lock-override-verify`. On success, the unit is tagged `imaging_Lock_Status="LOCKED-OVERRIDE"` on `asset_master` and the override user's name is permanently recorded. The TUI then continues into the hardware-detect screens normally.
* **S — Submit halt and power off**. The audit is POSTed with `test_type="phase2a_halt"` and `status="halted"`, the asset is tagged `imaging_Lock_Status="LOCKED"`, and the laptop powers off so the operator can set it aside for vendor/customer follow-up.
* **Q — Drop to shell**. Technical bypass for emergencies. **No audit log is written.** Used only by the imaging team to debug a misbehaving detector.

## Detection trust per design choice 2c

* **Software locks (4–7)** are taken at face value. They look at on-disk state which can't be faked at the bench level.
* **Hardware locks (1–2)** trigger a manual-confirm follow-up prompt **regardless of the auto-detect result**. This catches false negatives on enterprise BIOSes that don't expose admin-password state via SMBIOS Type 24 — common on older Dell Latitude / HP EliteBook fleets. The operator just answers Y / N: "Did the laptop prompt you for a BIOS password when you tried F10/DEL setup?".

## Computrace policy (per design choice 4b)

We only flag Computrace when the SMBIOS OEM string contains BOTH a brand keyword (`absolute`, `computrace`, `lojack`, `persistence`) AND an active keyword (`active`, `armed`, `enabled`, `persistent`, `enrolled`, `installed`).

Many HP and Lenovo enterprise BIOSes ship with `Absolute Software CompuTrace, available` listed as a capability even when the agent has never been enrolled at the customer site — that string DOES NOT trigger a halt. The status reported on those clean-but-capable units is `AVAILABLE_NOT_ACTIVE`.

## Backend impact

### `POST /api/imaging/ingest`

Accepts a new top-level `lock_audit` field with this schema:

```jsonc
{
  "halted": true,
  "checks": {
    "bios_password":  { "present": false, "status": "DISABLED",       "evidence": "...", "manual_confirm_required": true,  "manually_confirmed": true },
    "ata_security":   { "present": false, "status": "DISABLED",       "evidence": "...", "manual_confirm_required": true,  "manually_confirmed": true },
    "computrace":     { "present": true,  "status": "ACTIVE",         "evidence": "...", "manual_confirm_required": false },
    "bitlocker":      { "present": false, "status": "NOT_ENCRYPTED",  "evidence": "...", "manual_confirm_required": false },
    "intune":         { "present": false, "status": "NOT_ENROLLED",   "evidence": "...", "manual_confirm_required": false },
    "azure_ad":       { "present": false, "status": "NOT_JOINED",     "evidence": "...", "manual_confirm_required": false },
    "vendor_mdm":     { "present": false, "status": "NOT_ENROLLED",   "evidence": "...", "manual_confirm_required": false }
  },
  "detected_locks": ["computrace"],
  "summary": "Computrace / Absolute LoJack",
  "override": {
    "user_id": "...", "user_name": "Override User", "role": "Admin",
    "timestamp": "2026-05-08T22:46:32+00:00"
  },
  "decision": "override"  // "clear" | "submit_halt" | "override"
}
```

Whenever `lock_audit` is present and the serial matches a row in `asset_master`, the following fields are written:

* `imaging_Lock_Status` — `"CLEAR"` | `"LOCKED"` | `"LOCKED-OVERRIDE"`
* `imaging_Lock_Summary` — human-readable one-liner
* `imaging_Lock_Detected` — array of detected lock keys
* `imaging_Lock_Audit_At` — ISO timestamp
* `imaging_Lock_Override_By_Name` — present only when an override was used

### `POST /api/imaging/lock-override-verify` (NEW)

Bench-only endpoint (X-API-Key required). Body `{email, password}`. Returns `{ok:true, user_id, user_name, role}` if the user exists, password matches (bcrypt), and they hold the **Admin** or **Supervisor** role. Otherwise:
* `400` — email or password missing
* `401` — invalid email / password
* `403` — role check failed (e.g. QA, Imaging, Production users cannot authorize override)

## Build / deploy

The new helper module ships at `/opt/vstl/vstl_lock_audit.py` inside the squashfs. The build script (`04_build_live_iso_clonezilla.sh` step 5) installs it alongside the existing `vstl-imaging-tui.py` and `vstl_hw_detect.py`.

To rebuild and republish PXE assets:

```bash
cd /path/to/vstl-imaging-phase1/imaging_server
sudo ./INSTALL.sh    # placeholder-guard already in place from Phase 1
```

The installer will:
1. Validate `.env` (no placeholder API keys, must start with `vstl_img_`)
2. Run `04_build_live_iso_clonezilla.sh` → produces a fresh ISO with the new lock-audit module baked in
3. Re-publish kernel + initrd + squashfs to `/var/www/html/vstl-pxe/` for FOG netboot

No FOG menu changes needed — the same `vstl.live.netboot` entry serves the new ISO.

## Testing

* **26 unit tests** in `backend/tests/test_vstl_lock_audit.py` exercise every detector with mocked subprocess output (positive, negative, and "tool missing" paths) plus 3 `run_full_audit()` composition scenarios.
* **9 integration tests** in `backend/tests/test_imaging_phase2a.py` round-trip the full `lock_audit` payload, verify the new asset_master fields are populated, and exercise the override-verify endpoint with valid Admin credentials, bad password, unknown email, non-Admin role rejection, missing API key, and missing body fields.

Run the full suite:

```bash
cd /app/backend
python -m pytest tests/test_vstl_lock_audit.py tests/test_imaging_phase2a.py -v
```

Expected: **35 / 35 passing**.

## What's next

* **Phase 2B** — Interactive QC tests (Keyboard, Camera, Display, Touchscreen, Ports, Sound) gated by technician level.
* **Phase 2C** — Default 5-minute Burn/Stress test (stress-ng + memtester + fio + thermal monitoring).
* **Phase 3** — NIST 800-88 Purge erase + partclone capture/restore.
* **Phase 4** — Polish + audit dashboard.
