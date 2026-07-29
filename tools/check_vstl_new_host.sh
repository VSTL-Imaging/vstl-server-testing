#!/usr/bin/env bash
set -u

echo NEIGH
ip neigh show 10.255.0.45
echo PORTS
for port in 22 80 443 445 3389; do
  if timeout 2 bash -c "</dev/tcp/10.255.0.45/$port" 2>/dev/null; then
    echo "$port open"
  else
    echo "$port closed"
  fi
done
echo HTTP
curl -sSIk --max-time 4 http://10.255.0.45/ | head -12 || true
echo SSH
timeout 4 bash -c 'exec 3<>/dev/tcp/10.255.0.45/22; head -1 <&3' 2>/dev/null || true
echo ARP_DUPLICATE_CHECK
arping -I eno1 -c 5 10.255.0.45 2>/dev/null || true
