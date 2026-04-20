#!/usr/bin/env bash
# teardown.sh  –  Remove all containers and Docker networks created by deploy.sh

echo "[*] Stopping and removing containers..."
docker rm -f router_a router_b router_c 2>/dev/null || true

echo "[*] Removing Docker networks..."
docker network rm net_ab net_bc net_ac 2>/dev/null || true

echo "[✓] Teardown complete."
