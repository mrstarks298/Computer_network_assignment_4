import json
import logging
import os
import socket
import threading
import time


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("router")


# Runtime config from environment
my_ip = os.getenv("MY_IP", "127.0.0.1")
neighbors = [ip for ip in os.getenv("NEIGHBORS", "").split(",") if ip]

port = 5000
infinity = 16
update_interval = 5
timeout_secs = 15


# route_table[subnet] = [distance, next_hop]
route_table: dict[str, list] = {}
route_lock = threading.Lock()

# neighbor_seen_at[neighbor_ip] = last update timestamp
neighbor_seen_at: dict[str, float] = {}
seen_lock = threading.Lock()


def ip_to_subnet(ip: str) -> str:
    parts = ip.split(".")
    return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"


def discover_local_subnets() -> list[str]:
    """
    Build the directly connected /24 subnets using MY_IP and NEIGHBORS.
    """
    subnets = {ip_to_subnet(my_ip)}
    for neighbor_ip in neighbors:
        subnets.add(ip_to_subnet(neighbor_ip))
    return list(subnets)


def init_routing_table() -> None:
    """
    Insert directly connected subnets with metric 0.
    """
    with route_lock:
        for subnet in discover_local_subnets():
            route_table[subnet] = [0, "0.0.0.0"]
    log.info("Initial routing table: %s", route_table)


def build_packet() -> bytes:
    """
    Build a DV-JSON packet with all currently reachable routes.
    """
    with route_lock:
        routes = [
            {"subnet": subnet, "distance": dist_hop[0]}
            for subnet, dist_hop in route_table.items()
            if dist_hop[0] < infinity
        ]

    packet = {
        "router_id": my_ip,
        "version": 1.0,
        "routes": routes,
    }
    return json.dumps(packet).encode()


def broadcast_updates() -> None:
    """
    Periodically send route updates to all neighbors.
    Uses split horizon with poison reverse.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    while True:
        for neighbor_ip in neighbors:
            try:
                with route_lock:
                    routes = []
                    for subnet, (distance, next_hop) in route_table.items():
                        # If we learned this route from this neighbor,
                        # send it back as infinity (poison reverse).
                        if next_hop == neighbor_ip:
                            routes.append({"subnet": subnet, "distance": infinity})
                        else:
                            routes.append({"subnet": subnet, "distance": distance})

                packet = json.dumps(
                    {
                        "router_id": my_ip,
                        "version": 1.0,
                        "routes": routes,
                    }
                ).encode()

                sock.sendto(packet, (neighbor_ip, port))
                log.debug("Sent update to %s", neighbor_ip)

            except OSError as exc:
                log.warning("Could not send to %s: %s", neighbor_ip, exc)

        time.sleep(update_interval)


def listen_for_updates() -> None:
    """
    Receive DV packets and run Bellman-Ford update logic.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    log.info("Listening on UDP :%d", port)

    while True:
        try:
            data, addr = sock.recvfrom(65535)
            neighbor_ip = addr[0]

            with seen_lock:
                neighbor_seen_at[neighbor_ip] = time.time()

            packet = json.loads(data.decode())
            if packet.get("version") != 1.0:
                log.warning("Unsupported packet version from %s", neighbor_ip)
                continue

            sender_id = packet.get("router_id", neighbor_ip)
            routes = packet.get("routes", [])

            log.info(
                "Received %d routes from %s (id=%s)",
                len(routes),
                neighbor_ip,
                sender_id,
            )
            update_logic(neighbor_ip, routes)

        except json.JSONDecodeError:
            log.warning("Malformed JSON from %s", addr[0])
        except Exception as exc:
            log.error("listen_for_updates error: %s", exc)


def update_logic(neighbor_ip: str, routes_from_neighbor: list) -> None:
    """
    Bellman-Ford step:
    new_cost = advertised_distance + 1
    """
    changed = False

    with route_lock:
        for route in routes_from_neighbor:
            subnet = route.get("subnet")
            adv_distance = route.get("distance")

            if subnet is None or adv_distance is None:
                continue

            new_cost = adv_distance + 1
            current = route_table.get(subnet)

            if current is None:
                if new_cost < infinity:
                    route_table[subnet] = [new_cost, neighbor_ip]
                    _apply_kernel_route(subnet, neighbor_ip)
                    log.info("NEW route  %-20s dist=%d via %s", subnet, new_cost, neighbor_ip)
                    changed = True
                continue

            current_dist, current_hop = current

            # Better path found.
            if new_cost < current_dist:
                route_table[subnet] = [new_cost, neighbor_ip]
                _apply_kernel_route(subnet, neighbor_ip)
                log.info(
                    "UPDATED    %-20s dist=%d via %s (was %d via %s)",
                    subnet,
                    new_cost,
                    neighbor_ip,
                    current_dist,
                    current_hop,
                )
                changed = True
                continue

            # Same next-hop changed cost (important for reconvergence).
            if current_hop == neighbor_ip and new_cost != current_dist:
                if new_cost >= infinity:
                    route_table[subnet] = [infinity, neighbor_ip]
                    _delete_kernel_route(subnet)
                    log.info("REMOVED    %-20s (unreachable via %s)", subnet, neighbor_ip)
                else:
                    route_table[subnet] = [new_cost, neighbor_ip]
                    _apply_kernel_route(subnet, neighbor_ip)
                    log.info("UPDATED    %-20s dist=%d via %s", subnet, new_cost, neighbor_ip)
                changed = True

    if changed:
        log.info("Routing table: %s", route_table)


def _apply_kernel_route(subnet: str, via: str) -> None:
    ret = os.system(f"ip route replace {subnet} via {via} 2>/dev/null")
    if ret != 0:
        log.debug("ip route replace %s via %s returned %d", subnet, via, ret)


def _delete_kernel_route(subnet: str) -> None:
    ret = os.system(f"ip route del {subnet} 2>/dev/null")
    if ret != 0:
        log.debug("ip route del %s returned %d", subnet, ret)


def monitor_neighbors() -> None:
    """
    If a neighbor stops sending updates for timeout_secs,
    mark its routes unreachable and remove kernel routes.
    """
    while True:
        time.sleep(update_interval)
        now = time.time()
        dead_neighbors = []

        with seen_lock:
            for neighbor_ip, last_seen in neighbor_seen_at.items():
                if now - last_seen > timeout_secs:
                    dead_neighbors.append(neighbor_ip)

        for neighbor_ip in dead_neighbors:
            log.warning("Neighbor %s timed out - purging its routes", neighbor_ip)
            with seen_lock:
                neighbor_seen_at.pop(neighbor_ip, None)

            with route_lock:
                for subnet, (_distance, next_hop) in list(route_table.items()):
                    if next_hop == neighbor_ip:
                        route_table[subnet] = [infinity, neighbor_ip]
                        _delete_kernel_route(subnet)
                        log.info("PURGED     %-20s (next-hop %s gone)", subnet, neighbor_ip)


if __name__ == "__main__":
    log.info("Starting router - MY_IP=%s  NEIGHBORS=%s", my_ip, neighbors)
    init_routing_table()

    threading.Thread(target=broadcast_updates, daemon=True, name="broadcaster").start()
    threading.Thread(target=monitor_neighbors, daemon=True, name="monitor").start()

    # Main thread keeps listening for UDP updates.
    listen_for_updates()
