#!/usr/bin/env python3
"""Run a command over SSH using a password supplied in the environment."""
from __future__ import annotations

import os
import sys
import base64

import paramiko


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: ssh_exec.py HOST USER COMMAND [--sudo]", file=sys.stderr)
        return 2

    host, user, command, *options = sys.argv[1:]
    if command.startswith("@"):
        command = open(command[1:], encoding="utf-8").read()
    password = os.environ.get("VSTL_SSH_PASSWORD", "")
    if not password:
        print("VSTL_SSH_PASSWORD is required", file=sys.stderr)
        return 2

    use_sudo = "--sudo" in options
    if use_sudo:
        encoded = base64.b64encode(command.encode()).decode()
        command = f"sudo -S -p '' bash -c 'echo {encoded} | base64 -d | bash'"

    jump_host = os.environ.get("VSTL_JUMP_HOST", "")
    jump_client = None
    sock = None
    if jump_host:
        jump_client = paramiko.SSHClient()
        jump_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        jump_client.connect(jump_host, username=user, password=password, timeout=20)
        transport = jump_client.get_transport()
        if transport is None:
            raise RuntimeError("jump-host SSH transport is unavailable")
        sock = transport.open_channel(
            "direct-tcpip", (host, 22), ("127.0.0.1", 0)
        )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=20, sock=sock)
    # Avoid requesting a PTY for sudo. With a PTY, some servers echo stdin and
    # can leak the password into captured command output.
    stdin, stdout, stderr = client.exec_command(command, get_pty=False, timeout=None)
    if use_sudo:
        stdin.write(password + "\n")
        stdin.flush()

    sys.stdout.buffer.write(stdout.read())
    sys.stderr.buffer.write(stderr.read())
    status = stdout.channel.recv_exit_status()
    client.close()
    if jump_client is not None:
        jump_client.close()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
