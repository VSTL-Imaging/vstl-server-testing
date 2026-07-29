# VSTL e1000e I219-LM4 recovery module

This directory contains an external `e1000e` module for the Debian
`6.12.32-amd64` kernel used by the VSTL Clonezilla live image.

The module adds a disabled-by-default `allow_bad_nvm` parameter. When enabled,
it bypasses a failed NVM checksum only for Intel PCI device `8086:15d7`
(I219-LM4). It does not write or repair the NIC NVM. Every other device and
every valid checksum follows the upstream driver path.

Build provenance:

- Source: Debian `linux_6.12.32.orig.tar.xz`
- Source SHA1: `5aa4fc8da50a9c3f16bc1209c5d2e4060bab101b`
- Debian kernel package ABI: `6.12.32-1`
- Compiler: GCC 14
- Module SHA256: `214b2285026f16ece07e568d0d589fff298b3bedb1db801900c7fbb9b7fa36bd`

The PXE build verifies that SHA256 before replacing the stock module in the
generated initrd. The initramfs loads it with `allow_bad_nvm=1`; the code still
limits the bypass to `8086:15d7`.
