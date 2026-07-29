# VSTL Windows QC Helper

Experimental Windows-based QC helper for driver-sensitive tests.

This does not replace or modify the Linux VSTL bench server. It builds a
separate WinPE USB/ISO used only for testing microphone, headphone/speaker,
fingerprint detection, camera/device inventory, and driver loading.

## Output

The build script creates:

- `D:\win\vstl-windows-qc-helper.iso`
- `D:\win\vstl-windows-qc-helper.iso.sha256`
- `D:\win\vstl-windows-qc-helper-build\`

## Build Requirement

Run from an Administrator PowerShell window:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
cd "D:\Projects\VSTL Server\windows_qc_helper"
.\build-windows-qc-helper.ps1
```

## Optional Driver Packs

Put extracted Windows drivers here before building:

```text
D:\Projects\VSTL Server\windows_qc_helper\drivers\
```

Use extracted `.inf` driver folders, not vendor `.exe` installers. The build
script injects those drivers into WinPE, and the helper also attempts `drvload`
at runtime for anything under `X:\VSTL-QC\Drivers`.

Recommended first test driver types:

- Dell/HP/Lenovo chipset/serial IO
- Intel Smart Sound / Realtek audio
- Camera
- Fingerprint sensor drivers
- USB-C / Thunderbolt / Ethernet adapter drivers

## How To Use

1. Build the ISO.
2. Flash `D:\win\vstl-windows-qc-helper.iso` to a USB using Rufus.
3. Boot a test laptop from that USB.
4. The VSTL Windows QC Helper starts automatically.
5. Run tests from the menu.
6. Reports are saved to a removable writable USB drive if found, otherwise to
   `X:\VSTL-QC\Reports` for that boot session.

## Notes

This is a test-only helper. It does not run restore, capture, secure erase, PXE
boot, or production VSTL 360 ingest. Keep the current Linux bench as production
until this helper proves useful on real Dell/HP/Lenovo models.
