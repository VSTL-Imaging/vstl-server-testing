# QC AX201 and USB Parity Checkpoint - 2026-06-13

## Changes

- Intel AX201 support now includes `firmware-iwlwifi` plus explicit loading of
  `iwlwifi` and `iwlmvm`.
- The live image builder uses Debian 13 `trixie`, forces apt over IPv4, cleans
  chroot mounts reliably, and refuses to publish partial dependency installs.
- PXE and USB images contain the same speaker, microphone, wireless, ports,
  and keyboard QC runtime.
- Microphone capture rejects silent candidates and selects the strongest valid
  ALSA recording path.
- Keyboard completion displays PASS or FAIL for at least three seconds. Press
  `R` on that screen to retest.
- USB connector counts prefer kernel port topology. The technician confirms
  the physical connector inventory before individual plug/replug testing,
  because passive HDMI, audio, optional RJ45, and some Type-C connectors cannot
  be identified reliably from firmware data alone.

## Verification

- Local tests: `142 passed`.
- Deployed PXE SquashFS SHA256:
  `9aae7dd8e21bb58a5be9ab9a300c08747b161a0492947a90934cfad0b9e9257b`
- USB ISO SHA256:
  `09165d17232b774074cf312bd2c038385ba08b7b822a3c9de6ffb7cb765a718c`
- The validated PXE kernel, initrd, and iPXE boot-chain hashes were unchanged.
