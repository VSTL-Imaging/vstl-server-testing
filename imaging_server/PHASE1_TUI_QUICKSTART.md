# VSTL Bench TUI — Phase 1 Quickstart

## What changed

The bench Live ISO now boots into an interactive **Python TUI** instead of the
fully-automatic shell client. This is **Phase 1** of the VSTL imaging
spec rebuild — only menu navigation + hardware detection + ingest.

Phases 2-4 (Lock & MDM audit, interactive QC tests, Burn/Stress, Certified
Secure Erase, Image Capture/Restore) ship in subsequent releases.

---

## Bench operator flow (what the technician sees)

1. **Bench laptop PXE-boots** the VSTL Live ISO (already configured —
   no USB / no operator action required).
2. **Technician selection screen** — operator picks Layer 1 or Layer 2
   with the arrow keys + ENTER (or just press `1` / `2`).
3. **Main menu** — 4 options, **auto-selects Option 1 in 5 seconds** if
   the operator does nothing. ENTER confirms whatever is highlighted.
4. **Hardware detection screens** (auto-advance every 3 seconds, ENTER
   skips immediately):
   - 1/7 — Brand & Model
   - 2/7 — SKU / Part Number + Serial Number
   - 3/7 — CPU
   - 4/7 — GPU
   - 5/7 — RAM
   - 6/7 — Storage Health (per drive)
   - 7/7 — Battery Health
5. **Submission screen** — payload is POSTed to `/api/imaging/ingest`
   with the operator's technician level recorded.
6. **Completion screen** — shows ✅ + record_id + linked internal_id.
   Press ENTER → laptop powers off in 10 s. Press Q → drop to a root
   shell for debugging (`VSTL_AUTO_SHUTDOWN=0` also keeps it up).

---

## Files installed on the Live ISO

```
/opt/vstl/
├── vstl-bench-entry.sh        # systemd entrypoint (network → TUI)
├── vstl-imaging-tui.py        # Python curses TUI (Phase 1)
├── vstl_hw_detect.py          # Hardware detection helpers
├── vstl-imaging-client.sh     # Legacy shell client (fallback)
└── config.env                 # VSTL_API_BASE / VSTL_API_KEY / BENCH_ID
```

`vstl-bench-entry.sh` is what the kernel `ocs_live_run=` parameter now points
at. If the TUI is missing for any reason it execs the legacy shell client so
existing benches never get bricked by a partial upgrade.

---

## Backend fields added in Phase 1

These flow through `/api/imaging/ingest` into both `imaging_logs` and
`asset_master` (when the serial matches):

| Payload field             | Asset master column           |
|---------------------------|-------------------------------|
| `technician_level`        | `imaging_Technician_Level`    |
| `sku`                     | `imaging_SKU`                 |
| `selected_option`         | (logs only)                   |
| `selected_option_label`   | (logs only)                   |
| `session_started_at`      | (logs only)                   |

Existing fields (`brand`, `model`, `cpu`, `gpu`, `ram`, `ssd`, `ssd_health`,
`battery_status`, `battery_health`, `mac_id`, …) work unchanged.

---

## Rebuild + redeploy

After a change to any file in `bench-client/`:

```bash
cd /app/backend/imaging_server
sudo ./04_build_live_iso_clonezilla.sh    # ~3 min, produces build/vstl-live-amd64.iso
sudo ./06_setup_pxe_netboot.sh            # publishes ISO assets to FOG / dnsmasq
```

Bench laptops will pick up the new TUI on their next PXE boot — no changes
needed on the bench side.

---

## Phase 2-4 preview

The TUI's `screen_main_menu()` already returns the chosen option index
(0-3); Phase 2 will branch here:

| Option | Phase-1 behaviour | Phase-2/3 target |
|--------|-------------------|------------------|
| 1. Restore Approved System Image | Detect + ingest only | Lock audit → QC → Burn/Stress → Erase → Restore |
| 2. QC Test Only | Detect + ingest only | Lock audit → full interactive QC |
| 3. Certified Secure Erase | Detect + ingest only | Lock audit → erase only |
| 4. Capture Full System Image | Detect + ingest only | Lock audit → image capture |

The **Lock & MDM audit** (BIOS admin / ATA security / Computrace / Intune
/ etc.) runs as the first sub-step of every workflow per the user's spec
extension.
