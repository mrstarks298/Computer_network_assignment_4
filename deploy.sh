#!/usr/bin/env bash
# Build the router image and launch the 3-router triangle topology.
# Usage: bash deploy.sh

set -e

image_name="my-router"

# Create Docker networks.
echo "[*] Creating Docker networks..."
docker network create --subnet=10.0.1.0/24 --gateway=10.0.1.254 net_ab  2>/dev/null || echo "  net_ab already exists"
docker network create --subnet=10.0.2.0/24 --gateway=10.0.2.254 net_bc  2>/dev/null || echo "  net_bc already exists"
docker network create --subnet=10.0.3.0/24 --gateway=10.0.3.254 net_ac  2>/dev/null || echo "  net_ac already exists"

# Build image.
echo "[*] Building router image..."
docker build -t "$image_name" .

# Start router A.
echo "[*] Starting Router A..."
docker rm -f router_a 2>/dev/null || true
docker run -d --name router_a --privileged \
  --network net_ab --ip 10.0.1.1 \
  -e MY_IP=10.0.1.1 \
  -e NEIGHBORS=10.0.1.2,10.0.3.2 \
  "$image_name"
docker network connect --ip 10.0.3.1 net_ac router_a

# Start router B.
echo "[*] Starting Router B..."
docker rm -f router_b 2>/dev/null || true
docker run -d --name router_b --privileged \
  --network net_ab --ip 10.0.1.2 \
  -e MY_IP=10.0.1.2 \
  -e NEIGHBORS=10.0.1.1,10.0.2.2 \
  "$image_name"
docker network connect --ip 10.0.2.1 net_bc router_b

# Start router C.
echo "[*] Starting Router C..."
docker rm -f router_c 2>/dev/null || true
docker run -d --name router_c --privileged \
  --network net_bc --ip 10.0.2.2 \
  -e MY_IP=10.0.2.2 \
  -e NEIGHBORS=10.0.2.1,10.0.3.1 \
  "$image_name"
docker network connect --ip 10.0.3.2 net_ac router_c

echo
echo "[✓] Topology deployed. Run: bash test.sh"
