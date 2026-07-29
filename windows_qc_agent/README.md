# VSTL Full Windows QC Agent

Experimental QC agent that runs inside a normal installed Windows environment.

Use this when WinPE cannot test audio, microphone, or fingerprint properly. A
full Windows boot has the normal audio stack, Windows Biometric Service, OEM
drivers, and firmware/driver services that WinPE usually does not provide.

This package does not modify the Linux VSTL server, PXE, or VSTL 360 app.

## How To Run On A Test Laptop

1. Copy the `vstl-full-windows-qc-agent` folder to a USB drive.
2. Boot the laptop into its normal Windows installation.
3. Open the USB folder.
4. Right-click `Run-VSTL-Windows-QC.cmd` and choose **Run as administrator**.
5. Run the menu tests.
6. Reports are saved to:

```text
<USB drive>\VSTL-Windows-QC-Reports\
```

## Optional Driver Install

Extract Dell/HP/Lenovo drivers into:

```text
Drivers\
```

Then choose menu option `1`. The agent will run `pnputil /add-driver *.inf
/subdirs /install`. Use extracted `.inf` driver folders, not vendor `.exe`
installers.

## Why This Exists

WinPE is useful for deployment/recovery, but it is not a full hardware QC
environment. On Dell Latitude 5530 and similar systems, WinPE may show the
helper menu but still fail audio playback, microphone capture, or fingerprint
touch because the full Windows services and OEM driver stack are missing.
