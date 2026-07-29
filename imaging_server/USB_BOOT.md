# VSTL Bootable USB

The VSTL USB ISO boots directly into the same L1/L2 technician workflow as
PXE. It does not show the Clonezilla boot menu.

## Network order

1. Existing reachable network connection.
2. Every physical wired interface, including USB-A and USB-C Ethernet.
3. Interactive Wi-Fi network and password selection.
4. Offline mode when the operator chooses it or no network is available.

Wi-Fi must be on a network that can route to the VSTL server at
`10.255.0.75`. A USB Ethernet adapter is recommended for capture and restore
because it is faster and less likely to disconnect.

Wi-Fi passwords are entered at boot and are not stored in the ISO.

## Build

Run on the VSTL server:

```bash
cd /opt/vstl-imaging-phase1
sudo ./04_build_usb_iso.sh
```

Output:

```text
/opt/vstl-imaging-phase1/build/vstl-usb-live-amd64.iso
/opt/vstl-imaging-phase1/build/vstl-usb-live-amd64.iso.sha256
```

## Write to a USB stick

Use Rufus or balenaEtcher on Windows and select the ISO. When Rufus asks,
choose DD mode.

Writing the ISO erases the selected USB stick. Check the USB drive carefully
before starting.

## Boot

1. Insert the USB stick.
2. Open the laptop boot menu.
3. Select the UEFI USB device.
4. Connect a USB Ethernet adapter, or select Wi-Fi when prompted.
5. The standard VSTL L1/L2 screen opens after network setup.
