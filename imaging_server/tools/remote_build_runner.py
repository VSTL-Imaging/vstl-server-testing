#!/usr/bin/env python3
"""Run the long VSTL server rebuild over SSH while the caller polls a log."""
from __future__ import annotations

import os
import sys

import paramiko


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 3:
        print("usage: remote_build_runner.py HOST USER", file=sys.stderr)
        return 2
    host, user = sys.argv[1:]
    password = os.environ.get("VSTL_SSH_PASSWORD", "")
    if not password:
        print("VSTL_SSH_PASSWORD is required", file=sys.stderr)
        return 2

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=15)
    command = (
        "sudo -S bash -lc 'set -e; cd /opt/vstl-imaging-phase1; "
        "./INSTALL.sh; ./04_build_usb_iso.sh'"
    )
    stdin, stdout, stderr = client.exec_command(command, get_pty=True, timeout=None)
    stdin.write(password + "\n")
    stdin.flush()
    for line in iter(stdout.readline, ""):
        print(line, end="", flush=True)
    error = stderr.read().decode("utf-8", "replace")
    if error:
        print(error, file=sys.stderr, flush=True)
    status = stdout.channel.recv_exit_status()
    client.close()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
