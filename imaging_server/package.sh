#!/usr/bin/env bash
# Quick helper: package the imaging_server bundle into a single .tar.gz
# for scp'ing onto the imaging server.
set -euo pipefail

cd "$(dirname "$0")/.."  # /app/backend
OUT="/tmp/vstl-imaging-server.tar.gz"

tar --exclude='*/build/*' --exclude='*/.env' \
    -czf "$OUT" imaging_server/

ls -lh "$OUT"
echo
echo "On the imaging server:"
echo "  scp $OUT vstl@10.255.0.75:/tmp/"
echo "  ssh vstl@10.255.0.75"
echo "  sudo tar -xzf /tmp/vstl-imaging-server.tar.gz -C /opt/"
echo "  sudo chown -R vstl:vstl /opt/imaging_server"
echo "  sudo mv /opt/imaging_server /opt/vstl-imaging"
echo "  cd /opt/vstl-imaging && cp .env.example .env && nano .env"
echo "  chmod 600 .env"
echo "  sudo ./01_install_fog.sh"
