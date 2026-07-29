# PXE Direct HTTP Handoff Fix - 2026-06-10

## Symptom

The connected laptop received a PXE lease and downloaded the UEFI iPXE loader,
but never requested `/vstl-pxe/boot.ipxe` from Apache. It stopped before the
Linux kernel and initrd downloads.

## Cause

The iPXE second DHCP pass was directed to a TFTP `default.ipxe` shim. The
affected firmware downloaded that shim but did not complete its HTTP chain.

## Fix

`03_dnsmasq_proxydhcp.conf` now sends the final HTTP boot URL to clients
tagged as iPXE, while still including the boot-server metadata requested by
this iPXE build:

```text
dhcp-boot=tag:ipxe,http://10.255.0.75/vstl-pxe/boot.ipxe,vstl-imaging,10.255.0.75
dhcp-option=tag:ipxe,option:bootfile-name,http://10.255.0.75/vstl-pxe/boot.ipxe
dhcp-option=tag:ipxe,option:tftp-server,10.255.0.75
dhcp-option=tag:ipxe,option:tftp-server-address,10.255.0.75
```

TFTP remains enabled for the first firmware-to-iPXE loader transfer. FOG's
`snponly.efi` has an embedded chain to `tftp://${next-server}/default.ipxe`, so
the live `/tftpboot/default.ipxe` and `/tftpboot/autoexec.ipxe` shims now reuse
the firmware PXE network lease and chain straight to HTTP without running a
second DHCP request.

## Verification

- `dnsmasq --test --conf-dir=/etc/dnsmasq.d` passed.
- `dnsmasq`, `apache2`, and `tftpd-hpa` are active.
- `http://10.255.0.75/vstl-pxe/boot.ipxe` returns HTTP 200.
- PXE regression tests: 10 passed.
- Full suite: 114 passed, with one pre-existing unrelated Windows-only Intune
  fixture failure.

## Rollback

The pre-change server configuration is stored under:

```text
/opt/vstl-backups/pxe-direct-http-20260610_210925/
```
