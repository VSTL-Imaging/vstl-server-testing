#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import socket

mac = bytes.fromhex("5ced8cecd570")
packet = b"\xff" * 6 + mac * 16
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.sendto(packet, ("10.255.0.255", 9))
print("Wake-on-LAN magic packet sent")
PY
sleep 15
ping -c 3 -W 2 10.255.0.45
