#!/usr/bin/env bash
# Integration tests for the distance-vector router topology.

set -e

pass_count=0
fail_count=0
wait_secs=18

mark_pass() { echo "  [PASS] $1"; pass_count=$((pass_count + 1)); }
mark_fail() { echo "  [FAIL] $1"; fail_count=$((fail_count + 1)); }

wait_converge() {
  echo "  ... waiting ${wait_secs}s for convergence ..."
  sleep "$wait_secs"
}

# Show routing table from a container.
routing_table() {
  docker exec "$1" ip route show 2>/dev/null || true
}

# Check if a route exists in a container.
has_route() {          # has_route <container> <subnet>
  routing_table "$1" | grep -q "$2"
}

echo "================================================="
echo " Distance-Vector Router – Integration Tests"
echo "================================================="

echo ""
echo "[TEST 1] Checking convergence on all routers..."
wait_converge

for router in router_a router_b router_c; do
  for subnet in 10.0.1.0/24 10.0.2.0/24 10.0.3.0/24; do
    if has_route "$router" "$subnet"; then
      mark_pass "$router knows $subnet"
    else
      mark_fail "$router does NOT know $subnet"
    fi
  done
done

echo ""
echo "[TEST 2] Ping Router A → Router C (10.0.3.2)..."
if docker exec router_a ping -c 3 -W 2 10.0.3.2 > /dev/null 2>&1; then
  mark_pass "Router A can ping Router C at 10.0.3.2"
else
  mark_fail "Router A CANNOT ping Router C at 10.0.3.2"
fi

echo ""
echo "[TEST 3] Ping Router A → Router B (10.0.1.2)..."
if docker exec router_a ping -c 3 -W 2 10.0.1.2 > /dev/null 2>&1; then
  mark_pass "Router A can ping Router B at 10.0.1.2"
else
  mark_fail "Router A CANNOT ping Router B at 10.0.1.2"
fi

echo ""
echo "[TEST 4] Stopping Router C – testing failover convergence..."
docker stop router_c > /dev/null
echo "  Router C stopped."
wait_converge

# After C stops, A should still reach 10.0.2.0/24 via B.
if has_route router_a 10.0.2.0/24; then
  mark_pass "Router A still has a route to 10.0.2.0/24 after Router C went down"
  next_hop=$(routing_table router_a | grep 10.0.2.0/24 | awk '{print $3}')
  echo "     -> next-hop is now: $next_hop"
else
  mark_fail "Router A LOST route to 10.0.2.0/24 (no failover path)"
fi

if has_route router_b 10.0.2.0/24; then
  mark_pass "Router B still knows 10.0.2.0/24"
else
  mark_fail "Router B lost 10.0.2.0/24"
fi

echo ""
echo "[TEST 5] Restarting Router C – testing re-convergence..."
docker start router_c > /dev/null
wait_converge

for router in router_a router_b router_c; do
  if has_route "$router" 10.0.2.0/24; then
    mark_pass "$router re-learned 10.0.2.0/24 after Router C came back"
  else
    mark_fail "$router did NOT re-learn 10.0.2.0/24"
  fi
done

echo ""
echo "================================================="
echo " Results: $pass_count passed, $fail_count failed"
echo "================================================="

echo ""
echo "=== Final routing tables ==="
for r in router_a router_b router_c; do
  echo "--- $r ---"
  routing_table "$r"
done

[[ $fail_count -eq 0 ]] && exit 0 || exit 1
