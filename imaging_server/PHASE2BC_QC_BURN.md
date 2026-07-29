# Phase 2B + 2C — Interactive QC + Burn/Stress

**Status**: ✅ Shipped 2026-05-08 (after Phase 2A Lock & MDM/BIOS Audit).

These two phases run **after Phase 2A passes (or is overridden)** and **before** the bench attempts erase/image/restore in Phase 3. Per the spec, Phase 2B catches functional defects an operator can spot in seconds; Phase 2C catches latent thermal/RAM/disk issues that only surface under sustained load.

## Phase 2B — Interactive QC Tests

### Layer-gated test set (per design choice 1b)

| Layer | Tests | Approx. duration |
|-------|-------|------------------|
| **L1** | Display, Keyboard, Camera, Speaker | ~3 min |
| **L2** | Display, Keyboard, Camera, Fingerprint, Speaker, Microphone, Touchscreen, Ports | ~6 min |

Tests not in the layer's set are not even attempted. Tests whose hardware isn't physically present (no fingerprint reader, no touchscreen) auto-skip with `result="NA"`.

### Per-test screen flow

| Test | Probe | Screen |
|------|-------|--------|
| **Display** | always applicable | Full-screen R/G/B/W color cycle (6s total, ENTER advances) → operator answers PASS/FAIL |
| **Keyboard** | always applicable | Guided profile wizard asks for numpad, US ANSI/UK ISO geometry, QWERTY/AZERTY/QWERTZ arrangement, printed language/format, pointing stick, and backlight. The evdev map then displays only the selected physical keys and records the normalized profile name in the QC payload. |
| **Camera** | `/dev/video*` exists | Visual prompt — operator confirms LED/response with PASS/FAIL |
| **Fingerprint** | `lsusb` matches known reader VID/PID OR description contains "fingerprint"/"biometric" | Operator places finger on reader and confirms response |
| **Speaker** | `aplay -l` returns ≥ 1 card | TUI plays a 2-sec test tone via `speaker-test` → operator confirms hearing it |
| **Microphone** | `arecord -l` returns ≥ 1 card | TUI records 3 sec then plays it back → operator confirms hearing their voice |
| **Touchscreen** | `/proc/bus/input/devices` Name contains "touch" | Operator taps each corner + drags diagonally → PASS/FAIL |
| **Ports** | always (presence is operator-driven) | Operator plugs USB stick into every port + headphone/HDMI → PASS/FAIL |

Every FAIL prompts a single-line remarks dialog. `ESC` leaves remarks blank.

### Failure policy (per design choice 2a)

| Layer | Behavior on FAIL |
|-------|-----------------|
| **L1** | Recorded with operator-typed remarks; **unit continues** through Phase 2C and Phase 3. |
| **L2** | Backend auto-flips `asset_master.Status` to `L2_Rework` with `imaging_QC_Routed_Back=True` and `imaging_QC_Routed_Back_Reason="Phase 2B L2 QC failed: <test_keys>"`. Phase 2C is auto-skipped (`result="SKIP"`, `remarks="L2 QC failed, routing back"`). The unit appears in the L2 Rework queue on the next backend refresh. |

## Phase 2C — Burn / Stress Test

Always 5 minutes (configurable via `VSTL_BURN_DURATION_SEC` in `/opt/vstl/config.env`). Per design choice 4c, four stressors run with this concurrency pattern:

```
0s ─── 30s ─── 60s ─── 90s ── 120s ─── … ── 300s
─────────────────────────────────────────────────  CPU stress (full 5 min)
─────────────────────────────────────────────────  Thermal monitor (1Hz)
[ RAM ][ DISK ][ RAM ][ DISK ][ RAM ][ DISK ]…     RAM/disk alternate slices
```

* **CPU**: `stress-ng --cpu 0 --cpu-method matrixprod` for the full duration, concurrent in a daemon thread.
* **Thermal monitor**: 1-second-interval reads from every `/sys/class/thermal/thermal_zone*/temp` file. Max value sampled.
* **RAM**: `memtester 256M 1` per 30-sec slice, alternating with disk.
* **Disk**: `fio --rw=randread --size=64M` against a tmpfs-backed file (the SUT's actual disks are about to be wiped — we don't touch them here).

### Pass / fail thresholds (per design choice 5b)

| Metric | Threshold | Action |
|--------|-----------|--------|
| Max CPU temperature | ≥ 100 °C (configurable via `VSTL_BURN_THROTTLE_C`) | **FAIL** |
| memtester FAILUREs | ≥ 1 | **FAIL** |
| fio I/O errors | ≥ 1 | **FAIL** |
| stress-ng warnings | any count | **WARN only** — recorded for forensics, not a fail trigger (consumer laptops produce noisy stress-ng warnings under high load — they're informational) |

### Skip policy (per design choice 3c)

* **L1 operators** see a `[S] SKIP` option on the burn-intro screen. Pressing S opens a remarks dialog ("Why are you skipping?") and the result is recorded as `result="SKIP"` with the operator's reason, but the unit continues forward normally.
* **L2 operators** cannot skip. Burn always runs.
* Auto-skip path: if Phase 2B already failed for L2, Phase 2C auto-skips with `remarks="L2 QC failed, routing back"` so we don't waste 5 minutes on a unit that's already going to rework.

### Failure policy (mirrors Phase 2B)

| Layer | Behavior on burn FAIL |
|-------|-----------------------|
| **L1** | Recorded with auto-derived remarks (`thermal throttle at 102°C`, `memtester 3 error(s)`, etc.); unit continues. |
| **L2** | Backend appends `+ Phase 2C burn FAIL (<remarks>)` to existing `imaging_QC_Routed_Back_Reason` and ensures `Status=L2_Rework`. Both QC and burn reasons are joined with `+` so the L2 Rework queue shows the full picture. |

## Backend impact

### `/api/imaging/ingest` accepts two new top-level fields

```jsonc
{
  "qc_tests": {
    "tests": { "<key>": { result, remarks, evidence, applicable, ran, label, key }, ... },
    "passed": [...], "failed": [...], "skipped": [...], "na": [...],
    "all_passed": bool, "summary": "N PASS, M FAIL"
  },
  "burn_test": {
    "skipped": bool, "duration_sec": int, "actual_duration_sec": int,
    "max_temp_c": int|null, "throttled": bool,
    "errors": { memtester, fio, stress_ng_warnings },
    "result": "PASS" | "FAIL" | "SKIP", "remarks": str,
    "raw": { thermal_samples_c, stress_ng, memtester_last, fio_last }
  }
}
```

### asset_master fields written

* `imaging_QC_Summary`, `imaging_QC_Failed_Tests[]`, `imaging_QC_All_Passed`, `imaging_QC_Layer`, `imaging_QC_At`
* `imaging_Burn_Result`, `imaging_Burn_Skipped`, `imaging_Burn_Max_Temp_C`, `imaging_Burn_Throttled`, `imaging_Burn_Errors`, `imaging_Burn_Duration_Sec`, `imaging_Burn_At`
* On L2 fail (QC or burn): `Status="L2_Rework"`, `imaging_QC_Routed_Back=True`, `imaging_QC_Routed_Back_Reason=<combined>`

### Response body

```jsonc
{
  "success": true, "record_id": "...", "asset_matched": true,
  "lock_audit_recorded": true,
  "qc_tests_recorded": true,
  "burn_test_recorded": true,
  ...
}
```

## Build / deploy

The two new helper modules ship at `/opt/vstl/vstl_qc_tests.py` and `/opt/vstl/vstl_burn_stress.py`. The build script `04_build_live_iso_clonezilla.sh` step 5 was updated to install both alongside the existing `vstl_lock_audit.py`.

To rebuild the ISO and republish PXE assets:

```bash
cd /path/to/vstl-imaging-phase1/imaging_server
sudo ./INSTALL.sh
```

The legacy bench-client deps (`stress-ng`, `memtester`, `fio`, `alsa-utils`, `nvme-cli`, `hdparm`) all ship with Clonezilla Live by default — no extra apt-install step needed.

### Optional config overrides (`/opt/vstl/config.env`)

```
VSTL_BURN_DURATION_SEC=300        # default 300 (5 min). Min 30s.
VSTL_BURN_THROTTLE_C=100          # default 100 °C. Drop to 95 for stricter QC.
```

## Testing

* **21 unit tests** in `backend/tests/test_vstl_qc_tests.py` — every probe (present/absent/keyword-only paths) + result-struct factories + summarize() with all-pass / single-fail / mixed PASS/FAIL/SKIP/NA scenarios + sanity that PROBES dict covers every TEST_ORDER key.
* **11 unit tests** in `backend/tests/test_vstl_burn_stress.py` — burn-pass / thermal-fail / memtester-fail / fio-fail / stress-ng-warning-only / thermal-zone-missing / max-temp picks highest / skipped result shape + default remarks / progress callback shape / cfg overrides for duration + throttle.
* **8 integration tests** in `backend/tests/test_imaging_phase2bc.py` — round-trips qc_tests + burn_test through `/imaging/ingest`, verifies asset_master annotations, exercises L2 QC fail → L2_Rework, L1 QC fail → no routing, L2 burn fail → L2_Rework, both-fail combined reason, L1 burn skip preserved, unmatched-serial still logs.

Run the full Phase 2B/2C suite:

```bash
cd /app/backend
python -m pytest tests/test_vstl_qc_tests.py tests/test_vstl_burn_stress.py tests/test_imaging_phase2bc.py -v
```

Expected: **40 / 40 passing**.

### Wi-Fi QC behavior

Wi-Fi QC checks nearby radio visibility rather than internet access. The live
client unblocks and raises each physical wireless interface, retries scanning
up to three times, and accepts either a named SSID or a hidden BSS as proof that
the adapter can scan. P2P helper interfaces are excluded. Bluetooth remains a
separate adapter-presence check, and both checks must pass for the combined
Wi-Fi / Bluetooth QC result.

The FULL imaging stack (Phase 1 + 2A + 2B + 2C) test count is **96 / 96**:

```bash
cd /app/backend
python -m pytest tests/test_imaging_phase1.py tests/test_imaging_phase2a.py \
                 tests/test_imaging_phase2bc.py tests/test_vstl_hw_detect.py \
                 tests/test_vstl_lock_audit.py tests/test_vstl_qc_tests.py \
                 tests/test_vstl_burn_stress.py imaging_server/tests/test_menu_patch.py
```

## Operator flow (full Phase 1 + 2A + 2B + 2C)

```
Technician selection (L1 / L2)
        ↓
Main menu (auto-select Option 1 in 5s)
        ↓
Phase 2A — Lock & MDM/BIOS Audit  ← 7 detectors + manual confirm
        ├── HALT → Admin Override  → continue with LOCKED-OVERRIDE tag
        └── Clear → continue
        ↓
Phase 1 — Hardware-detect screens (Brand → SKU → CPU → GPU → RAM → Storage → Battery)
        ↓
Phase 2B — Interactive QC tests  ← 4 (L1) or 8 (L2) tests
        ├── L2 fail → skip Phase 2C, route back to L2_Rework
        └── Pass / L1 fail → continue
        ↓
Phase 2C — Burn / Stress test  ← 5-min concurrent burn (L1 may skip)
        ├── L2 fail → route back to L2_Rework
        └── Pass / L1 fail or skip → continue
        ↓
Submit to /api/imaging/ingest + completion screen + auto-poweroff
```

## What's next

* **Phase 3** — NIST 800-88 Purge erase + partclone capture/restore. Image-storage policy = always overwrite (per spec).
* **Phase 4** — Polish + audit dashboard. Live "Bench Status" page in the React frontend showing every active bench, current phase, and any HALT/ROUTED-BACK alerts.
