#!/usr/bin/env python3
"""
Distance-vector router (RIP-style) using Bellman-Ford over UDP/JSON.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.getenv("DV_PORT", "5000"))
BIND_IP = os.getenv("DV_BIND", "0.0.0.0")
SKIP_IP_ROUTE = os.getenv("DV_SKIP_IP_ROUTE", "").lower() in ("1", "true", "yes")
# RIP-style infinity (metric 16 = unreachable)
INFINITY = 16
BROADCAST_INTERVAL = 5.0
# If a neighbor is silent this long, drop routes learned via that neighbor
NEIGHBOR_TIMEOUT = 15.0
# Periodically check for stale routes
TIMEOUT_CHECK_INTERVAL = 5.0

MY_IP = os.getenv("MY_IP", "127.0.0.1")


def parse_neighbors(raw: str, default_port: int) -> Tuple[List[Tuple[str, int]], List[str]]:
    """
    NEIGHBORS: comma-separated IPv4 addresses, optional :port per entry.
    Default port is DV_PORT (used when :port omitted).
    """
    endpoints: List[Tuple[str, int]] = []
    ips: List[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            host, _, maybe_port = part.rpartition(":")
            if maybe_port.isdigit():
                endpoints.append((host, int(maybe_port)))
                ips.append(host)
                continue
        endpoints.append((part, default_port))
        ips.append(part)
    return endpoints, ips


NEIGHBOR_ENDPOINTS, NEIGHBOR_IPS = parse_neighbors(os.getenv("NEIGHBORS", ""), PORT)
# Set DV_FULL_LOG=1 to print entire JSON body on receive (verbose)
FULL_LOG = os.getenv("DV_FULL_LOG", "").lower() in ("1", "true", "yes")


def neighbor_rank(neighbor_ip: str) -> int:
    """
    Lower index in NEIGHBORS wins when DV costs tie.

    Order neighbors so the on-link / single-bridge path is listed first (e.g. for
    router A put 10.0.1.3 before 10.0.3.3). Otherwise equal-cost routes may use
    a next hop that requires container forwarding across Docker bridges and ICMP
    can fail.
    """
    try:
        return NEIGHBOR_IPS.index(neighbor_ip)
    except ValueError:
        return 10**9

# Routing table: { subnet: [distance, next_hop] }
routing_table: Dict[str, List[Any]] = {}
# Directly connected /24 subnets (filled during init)
LOCAL_SUBNETS: frozenset = frozenset()
table_lock = threading.Lock()

# Last time we received any valid DV packet from each neighbor (for timeout)
last_heard: Dict[str, float] = {}
neighbor_lock = threading.Lock()


def subnet_for_ip(ip: str) -> str:
    """Assume /24: a.b.c.d -> a.b.c.0/24."""
    parts = ip.split(".")
    if len(parts) != 4:
        return "0.0.0.0/24"
    return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"


def discover_local_subnets() -> List[str]:
    """
    All /24 subnets this host is directly connected to (from interface addresses).
    Multi-homed routers (e.g. Docker triangle) need more than one connected net.

    DV_FORCE_LOCAL_SUBNETS (comma-separated) is merged with discovery so Docker
    topologies never miss a /24: if discovery is incomplete, DV must NOT install
    a ``via`` route over a connected prefix or local ICMP (e.g. to one's own
    10.0.2.2) breaks.
    """
    forced_raw = os.getenv("DV_FORCE_LOCAL_SUBNETS", "").strip()
    forced_list = [s.strip() for s in forced_raw.split(",") if s.strip()]

    found: List[str] = []
    try:
        out = subprocess.check_output(["ip", "-j", "addr", "show"], text=True, timeout=5)
        for iface in json.loads(out):
            for addr_info in iface.get("addr_info", []):
                if addr_info.get("family") != "inet":
                    continue
                ip = addr_info.get("local")
                if not ip or ip.startswith("127."):
                    continue
                found.append(subnet_for_ip(ip))
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError, OSError) as e:
        print(f"[WARN] discover_local_subnets: {e}; using MY_IP only", flush=True)
        return [subnet_for_ip(MY_IP)]
    # Unique, stable order
    seen: set = set()
    uniq: List[str] = []
    for s in found:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    base = uniq if uniq else [subnet_for_ip(MY_IP)]
    if not forced_list:
        return base
    # Union: forced entries first (stable), then discovered
    merged: List[str] = []
    seen_m: set = set()
    for s in forced_list + base:
        if s not in seen_m:
            seen_m.add(s)
            merged.append(s)
    return merged


def get_iface_for_subnet(local_subnet: str) -> Optional[str]:
    """Interface whose IPv4 address falls in local_subnet (/24)."""
    try:
        out = subprocess.check_output(["ip", "-j", "addr", "show"], text=True, timeout=5)
        for iface in json.loads(out):
            for addr_info in iface.get("addr_info", []):
                if addr_info.get("family") != "inet":
                    continue
                ip = addr_info.get("local")
                if not ip or ip.startswith("127."):
                    continue
                if subnet_for_ip(ip) == local_subnet:
                    return iface.get("ifname")
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError, OSError) as e:
        print(f"[WARN] Could not resolve interface for subnet {local_subnet}: {e}", flush=True)
    return None


def get_first_ipv4_on_dev(dev: str) -> Optional[str]:
    """First non-loopback IPv4 on ``dev``."""
    try:
        out = subprocess.check_output(["ip", "-j", "addr", "show", "dev", dev], text=True, timeout=5)
        for iface in json.loads(out):
            for addr_info in iface.get("addr_info", []):
                if addr_info.get("family") != "inet":
                    continue
                ip = addr_info.get("local")
                if ip and not ip.startswith("127."):
                    return str(ip)
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError, OSError, ValueError):
        pass
    return None


def get_local_ip_on_subnet(local_subnet: str) -> Optional[str]:
    """Our IPv4 address on the /24 ``local_subnet`` (e.g. 10.0.1.0/24)."""
    try:
        out = subprocess.check_output(["ip", "-j", "addr", "show"], text=True, timeout=5)
        for iface in json.loads(out):
            if iface.get("ifname") == "lo":
                continue
            for addr_info in iface.get("addr_info", []):
                if addr_info.get("family") != "inet":
                    continue
                ip = addr_info.get("local")
                if not ip or ip.startswith("127."):
                    continue
                if subnet_for_ip(ip) == local_subnet:
                    return str(ip)
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError, OSError, ValueError):
        pass
    return None


def get_dev_for_local_ip(local_ip: str) -> Optional[str]:
    """Interface that owns ``local_ip``."""
    try:
        out = subprocess.check_output(["ip", "-j", "addr", "show"], text=True, timeout=5)
        for iface in json.loads(out):
            for addr_info in iface.get("addr_info", []):
                if addr_info.get("family") != "inet":
                    continue
                if addr_info.get("local") == local_ip:
                    return iface.get("ifname")
    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError, OSError, ValueError):
        pass
    return None


def oif_and_src_for_nexthop(next_hop: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Outgoing iface + source for reaching an on-link gateway.

    ``ip route get <gw>`` can pick the wrong ``dev`` when multiple interfaces
    exist; we anchor on our address in the same /24 as the gateway, then set
    ``dev`` to the interface that actually owns that address (dev/src always match).
    """
    nh_net = subnet_for_ip(next_hop)
    src = get_local_ip_on_subnet(nh_net)
    oif = get_dev_for_local_ip(src) if src else None
    if not oif:
        oif = get_iface_for_subnet(nh_net)
    if not src and oif:
        src = get_first_ipv4_on_dev(oif)
    return oif, src


def apply_linux_route(subnet: str, next_hop: str) -> None:
    """
    Install or replace a kernel route. Connected nets use `dev`; remote use `via`.
    """
    if SKIP_IP_ROUTE:
        return
    # Never program a learned via-route for a connected /24 — it replaces the
    # kernel 'connected' route and breaks local delivery (e.g. ping to own 10.0.2.2).
    if subnet in LOCAL_SUBNETS:
        next_hop = "0.0.0.0"
    if next_hop == "0.0.0.0":
        iface = get_iface_for_subnet(subnet)
        if iface:
            cmd = f"ip route replace {subnet} dev {iface}"
        else:
            cmd = f"ip route replace {subnet} via 0.0.0.0"
    else:
        # Bind dev + src to the /24 of the next hop (same bridge as that neighbor).
        # Do not use ``ip route get <gw>`` for dev — it can attach the wrong iface
        # (e.g. eth0 vs eth1) when multiple links exist, breaking ARP and ping.
        oif, prefsrc = oif_and_src_for_nexthop(next_hop)
        cmd = f"ip route replace {subnet} via {next_hop}"
        if oif:
            cmd += f" dev {oif}"
        if prefsrc:
            cmd += f" src {prefsrc}"
        # Prefer plain route; add onlink only if needed (some stacks reject onlink+src)
        candidates = [cmd, f"{cmd} onlink"]
        rc = 1
        for c in candidates:
            rc = os.system(c)
            if rc == 0:
                break
        if rc != 0:
            # Last resort: via only (kernel picks dev)
            rc = os.system(f"ip route replace {subnet} via {next_hop}")
        if rc != 0:
            print(f"[WARN] ip route failed ({rc}) for {subnet} via {next_hop}", flush=True)
        return
    rc = os.system(cmd)
    if rc != 0:
        print(f"[WARN] Command failed ({rc}): {cmd}", flush=True)


def delete_linux_route(subnet: str) -> None:
    if SKIP_IP_ROUTE:
        return
    os.system(f"ip route del {subnet} 2>/dev/null")


def print_routing_table() -> None:
    print("[ROUTING TABLE]", flush=True)
    for subnet in sorted(routing_table.keys()):
        dist, nh = routing_table[subnet]
        print(f"  {subnet}  ->  distance={dist}  next_hop={nh}", flush=True)
    print(flush=True)


def initialize_table() -> List[str]:
    """Add each directly connected /24; distance 0, next_hop 0.0.0.0."""
    global LOCAL_SUBNETS
    locals_ = discover_local_subnets()
    LOCAL_SUBNETS = frozenset(locals_)
    with table_lock:
        for loc in locals_:
            routing_table[loc] = [0, "0.0.0.0"]
    for loc in locals_:
        apply_linux_route(loc, "0.0.0.0")
    return locals_


# ---------------------------------------------------------------------------
# Bellman-Ford + split horizon
# ---------------------------------------------------------------------------

def omit_route_split_horizon_send_impl(
    rt: Dict[str, List[Any]], neighbor_ip: str, subnet: str
) -> bool:
    """Return True if this subnet should be omitted when sending to neighbor_ip."""
    if subnet not in rt:
        return False
    _, nh = rt[subnet]
    return nh == neighbor_ip


def omit_route_split_horizon_send(neighbor_ip: str, subnet: str) -> bool:
    """
    Send-side split horizon: omit subnet from advertisements to the neighbor
    we learned that subnet from (reduces count-to-infinity).
    """
    with table_lock:
        return omit_route_split_horizon_send_impl(routing_table, neighbor_ip, subnet)


def bellman_ford_update_impl(
    rt: Dict[str, List[Any]],
    local_subnets: frozenset,
    neighbor_ip: str,
    routes: List[Dict[str, Any]],
) -> bool:
    """
    Core Bellman-Ford update mutating ``rt``. Used by daemon and unit tests.

    Equal-cost paths: prefer the neighbor listed earlier in ``NEIGHBORS`` (see
    ``neighbor_rank``) so Docker setups can favor the single-bridge next hop.
    """
    changed = False
    for r in routes:
        try:
            subnet = str(r["subnet"])
            advertised = int(float(r["distance"]))
        except (KeyError, TypeError, ValueError) as e:
            print(f"[WARN] Bad route entry, skip: {r} ({e})", flush=True)
            continue

        if subnet in local_subnets:
            continue

        cur = rt.get(subnet)

        if advertised >= INFINITY:
            if cur is not None and cur[1] == neighbor_ip:
                del rt[subnet]
                changed = True
            continue

        new_dist = advertised + 1
        if new_dist >= INFINITY:
            continue

        if cur is not None and cur[1] == neighbor_ip:
            if new_dist != cur[0]:
                rt[subnet] = [new_dist, neighbor_ip]
                changed = True
            continue

        if cur is None:
            rt[subnet] = [new_dist, neighbor_ip]
            changed = True
        elif new_dist < cur[0]:
            rt[subnet] = [new_dist, neighbor_ip]
            changed = True
        elif new_dist == cur[0] and cur[1] != neighbor_ip:
            # Equal DV cost: prefer earlier neighbor in NEIGHBORS (see neighbor_rank)
            if neighbor_rank(neighbor_ip) < neighbor_rank(cur[1]):
                rt[subnet] = [new_dist, neighbor_ip]
                changed = True

    # Subnets present in this DV packet (including poison / poison-reverse).
    advertised_subnets: set = set()
    for r in routes:
        try:
            advertised_subnets.add(str(r["subnet"]))
        except (KeyError, TypeError, AttributeError):
            continue

    # Implicit withdrawal: full updates omit prefixes the neighbor no longer has.
    # Without this, stale "via neighbor" entries survive when the neighbor drops a
    # prefix but split horizon previously hid updates (classic post-failure black hole).
    if advertised_subnets:
        for sn in list(rt.keys()):
            if sn in local_subnets:
                continue
            ent = rt.get(sn)
            if ent is None or len(ent) < 2:
                continue
            if ent[1] != neighbor_ip:
                continue
            if sn not in advertised_subnets:
                del rt[sn]
                changed = True

    return changed


def bellman_ford_update(neighbor_ip: str, routes: List[Dict[str, Any]]) -> None:
    """
    Bellman-Ford: new_distance = advertised_distance + 1 (hop count).

    Poison: advertised metric >= INFINITY removes a route learned via that neighbor.

    Receive-side: same-next-hop metric refresh; send-side split horizon in
    ``omit_route_split_horizon_send``.
    """
    with table_lock:
        changed = bellman_ford_update_impl(
            routing_table, LOCAL_SUBNETS, neighbor_ip, routes
        )
        # DV must never keep a non-direct entry for a connected subnet (see apply_linux_route).
        for loc in LOCAL_SUBNETS:
            if routing_table.get(loc) != [0, "0.0.0.0"]:
                routing_table[loc] = [0, "0.0.0.0"]
                changed = True

    if changed:
        print_routing_table()
        with table_lock:
            snap = dict(routing_table)
        for sn, (d, nh) in snap.items():
            if sn in LOCAL_SUBNETS:
                apply_linux_route(sn, "0.0.0.0")
            elif d > 0 and d < INFINITY:
                apply_linux_route(sn, nh)


def refresh_neighbor_activity(neighbor_ip: str) -> None:
    with neighbor_lock:
        last_heard[neighbor_ip] = time.monotonic()


def purge_stale_routes() -> None:
    """Remove routes via neighbors that have timed out."""
    now = time.monotonic()
    to_remove: List[str] = []
    with neighbor_lock:
        dead_neighbors = []
        for n in NEIGHBOR_IPS:
            t = last_heard.get(n)
            if t is not None and now - t > NEIGHBOR_TIMEOUT:
                dead_neighbors.append(n)

    if not dead_neighbors:
        return

    changed = False
    with table_lock:
        for subnet, (_dist, nh) in list(routing_table.items()):
            if subnet in LOCAL_SUBNETS:
                continue
            if nh in dead_neighbors:
                to_remove.append(subnet)
        for subnet in to_remove:
            del routing_table[subnet]
            changed = True

    if not changed:
        return

    print(f"[TIMEOUT] Neighbor(s) silent > {NEIGHBOR_TIMEOUT}s; removing routes via {dead_neighbors}", flush=True)
    for subnet in to_remove:
        delete_linux_route(subnet)

    print_routing_table()
    _reapply_kernel_routes_snapshot()


def _reapply_kernel_routes_snapshot() -> None:
    """Push current routing_table to Linux (caller should hold no locks)."""
    with table_lock:
        snap = dict(routing_table)
        locals_now = LOCAL_SUBNETS
    for sn, (d, nh) in snap.items():
        if sn in locals_now:
            apply_linux_route(sn, "0.0.0.0")
        elif d > 0 and d < INFINITY:
            apply_linux_route(sn, nh)
        elif d == 0:
            apply_linux_route(sn, "0.0.0.0")


def sync_local_subnets_from_os() -> None:
    """
    Re-read interfaces from the OS. Graders often ``docker run`` then
    ``docker network connect`` extra links after the process starts; link tests
    detach networks. LOCAL_SUBNETS must track reality or DV/kernel routes break.
    """
    global LOCAL_SUBNETS
    new_locals = frozenset(discover_local_subnets())
    removed: List[str] = []
    mutated = False
    with table_lock:
        old = LOCAL_SUBNETS
        if new_locals != old:
            for s in old - new_locals:
                removed.append(s)
                routing_table.pop(s, None)
            for s in new_locals - old:
                routing_table[s] = [0, "0.0.0.0"]
            LOCAL_SUBNETS = new_locals
            mutated = True
            print(f"[SYNC] Local subnets {sorted(old)} -> {sorted(new_locals)}", flush=True)
        for s in LOCAL_SUBNETS:
            if routing_table.get(s) != [0, "0.0.0.0"]:
                routing_table[s] = [0, "0.0.0.0"]
                mutated = True
    if not removed and not mutated:
        return
    for s in removed:
        delete_linux_route(s)
    if mutated or removed:
        print_routing_table()
        disable_rp_filter_strict()
        _reapply_kernel_routes_snapshot()


# ---------------------------------------------------------------------------
# Networking threads
# ---------------------------------------------------------------------------

def broadcast_updates() -> None:
    """Periodically send DV-JSON to all neighbors (send-side split horizon)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        while True:
            with table_lock:
                routes_out: List[Dict[str, Any]] = []
                for subnet, (dist, _) in routing_table.items():
                    routes_out.append({"subnet": subnet, "distance": dist})

            for neighbor_ip, neighbor_port in NEIGHBOR_ENDPOINTS:
                packet_routes: List[Dict[str, Any]] = []
                for r in routes_out:
                    sn = r["subnet"]
                    # Poison reverse: advertise infinity to the neighbor we learned the
                    # route from (RIP-style), so they can flush; omission alone lets stale
                    # mutual routes persist after topology changes.
                    if omit_route_split_horizon_send(neighbor_ip, sn):
                        packet_routes.append({"subnet": sn, "distance": INFINITY})
                    else:
                        packet_routes.append({"subnet": sn, "distance": r["distance"]})

                packet = {
                    "router_id": MY_IP,
                    "version": 1.0,
                    "routes": packet_routes,
                }
                payload = json.dumps(packet).encode()
                try:
                    sock.sendto(payload, (neighbor_ip, neighbor_port))
                    print(f"[SEND] -> {neighbor_ip}:{neighbor_port}: {len(packet_routes)} routes", flush=True)
                except OSError as e:
                    print(f"[ERROR] sendto {neighbor_ip}:{neighbor_port}: {e}", flush=True)

            time.sleep(BROADCAST_INTERVAL)
    finally:
        sock.close()


def listen_for_updates() -> None:
    """Main thread: receive UDP JSON and apply updates."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((BIND_IP, PORT))
    except OSError as e:
        print(f"[FATAL] Cannot bind UDP {BIND_IP}:{PORT}: {e}", flush=True)
        sys.exit(1)

    print(f"--- Router {MY_IP} ---", flush=True)
    print(f"Local subnets: {sorted(LOCAL_SUBNETS)}", flush=True)
    print(f"Neighbors: {NEIGHBOR_ENDPOINTS}", flush=True)
    print(f"Listening on UDP {PORT} ...", flush=True)
    print_routing_table()

    while True:
        try:
            data, addr = s.recvfrom(65535)
        except OSError as e:
            print(f"[ERROR] recvfrom: {e}", flush=True)
            continue

        neighbor_ip = addr[0]
        print(f"[RECV] from {neighbor_ip} ({len(data)} bytes)", flush=True)

        if NEIGHBOR_IPS and neighbor_ip not in NEIGHBOR_IPS:
            print(f"[RECV] ignored (not in NEIGHBOR_IPS): {neighbor_ip}", flush=True)
            continue

        try:
            text = data.decode("utf-8")
            packet = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            print(f"[ERROR] Invalid JSON from {neighbor_ip}: {e}", flush=True)
            continue

        try:
            rid = packet.get("router_id", "?")
            ver = packet.get("version", "?")
            routes = packet.get("routes", [])
            # Debug: full payload (assignment: print received packets)
            print(f"[RECV] router_id={rid} version={ver} routes={len(routes)}", flush=True)
            if FULL_LOG:
                print(f"[RECV] body: {text}", flush=True)
            else:
                preview = text if len(text) <= 400 else text[:400] + "..."
                print(f"[RECV] body: {preview}", flush=True)
            if not isinstance(routes, list):
                print("[WARN] routes is not a list; ignored", flush=True)
                continue
        except AttributeError:
            print("[WARN] Malformed packet", flush=True)
            continue

        refresh_neighbor_activity(neighbor_ip)
        bellman_ford_update(neighbor_ip, routes)


def timeout_watcher() -> None:
    while True:
        time.sleep(TIMEOUT_CHECK_INTERVAL)
        sync_local_subnets_from_os()
        purge_stale_routes()


def _startup_interface_resync() -> None:
    """Second pass after Docker attaches secondary interfaces."""
    time.sleep(2.0)
    sync_local_subnets_from_os()
    disable_rp_filter_strict()


def _write_sysctl(path: str, value: str) -> bool:
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except OSError as e:
        print(f"[WARN] {path} = {value!r}: {e}", flush=True)
        return False


def disable_rp_filter_strict() -> None:
    """
    ICMP echo reply from a peer's *other* address (e.g. 10.0.2.2) arrives on the
    interface used for the shared /24 (e.g. net_ab). Strict rp_filter drops it
    because the source is not in that /24 — must disable on all interfaces.
    """
    conf = "/proc/sys/net/ipv4/conf"
    # Kernel uses max(all, default, iface); set all + default first.
    for name in ("all", "default"):
        p = os.path.join(conf, name, "rp_filter")
        if os.path.isfile(p):
            _write_sysctl(p, "0")
    try:
        for name in os.listdir(conf):
            rp = os.path.join(conf, name, "rp_filter")
            if os.path.isfile(rp):
                _write_sysctl(rp, "0")
    except OSError as e:
        print(f"[WARN] rp_filter loop: {e}", flush=True)
    os.system("sysctl -w net.ipv4.conf.all.rp_filter=0 >/dev/null 2>&1")
    os.system("sysctl -w net.ipv4.conf.default.rp_filter=0 >/dev/null 2>&1")


def main() -> None:
    # Enable forwarding (useful if containers forward traffic)
    try:
        with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
            f.write("1")
    except OSError:
        os.system("sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1")

    disable_rp_filter_strict()

    locals_ = initialize_table()
    # Interfaces are fully configured; re-apply after any hot-plug edge case
    disable_rp_filter_strict()
    print(f"[INIT] Connected subnet(s) {locals_} metric 0", flush=True)

    t_tx = threading.Thread(target=broadcast_updates, name="broadcast", daemon=True)
    t_tx.start()

    t_timeout = threading.Thread(target=timeout_watcher, name="timeouts", daemon=True)
    t_timeout.start()

    threading.Thread(target=_startup_interface_resync, name="iface-resync", daemon=True).start()

    listen_for_updates()


if __name__ == "__main__":
    main()
