# Building a Custom Distance-Vector Router
## Assignment 4 Report

**Name:** Saurabh  
**Roll Number:** [Add your roll number]  
**GitHub Link:** [Add repository URL]

---

## 1. Introduction

This assignment implements a Distance-Vector (DV) router in Python and runs each router as a Docker container.  
The objective is to reproduce core routing behavior in a realistic setup where user-space logic updates the Linux kernel routing table.

The router supports:
- Periodic route exchange with neighbors using UDP.
- Shortest-path computation using Bellman-Ford rules (hop count metric).
- Dynamic route installation/removal in the OS using `ip route`.
- Failure detection and automatic re-convergence.
- Loop-risk reduction using split horizon and poisoned metrics.

---

## 2. Router Design

### 2.1 Runtime Components

The daemon in `router.py` maintains shared state protected by a lock and runs concurrent routines:

- `listen`: receives DV updates on UDP port `5000`.
- `broadcast`: sends local routing knowledge every fixed interval.
- `monitor`: checks last-seen timestamps and marks neighbors dead after timeout.

### 2.2 Routing Table Model

Each route is stored as:

`subnet -> [distance, next_hop]`

Conventions:
- Directly connected subnet: `distance = 0`, `next_hop = 0.0.0.0`.
- Learned subnet: `distance = neighbor_distance + 1`.
- Unreachable subnet: distance set to protocol infinity.

### 2.3 Update Logic (Bellman-Ford Style)

For every received route:
- Add route if subnet is new and reachable.
- Replace route if a lower metric is discovered.
- Refresh route if the same next-hop reports changed metric (important during failures/recovery).

Kernel sync operations:
- Add/modify: `ip route replace <subnet> via <next_hop>`
- Remove stale path: `ip route del <subnet>`

---

## 3. Topology and Addressing

### 3.1 Logical Topology

Triangle topology was used to guarantee alternate paths.

```text
        Router A
       /        \
   net_ab      net_ac
    /            \
Router B ------ Router C
        net_bc
```

### 3.2 Example Interface Mapping

| Router | net_ab | net_bc | net_ac |
|---|---|---|---|
| A | 10.0.1.1 | - | 10.0.3.1 |
| B | 10.0.1.2 | 10.0.2.1 | - |
| C | - | 10.0.2.2 | 10.0.3.2 |

Gateway reservation:
- `10.0.1.254`, `10.0.2.254`, `10.0.3.254` were used as Docker bridge gateways.

### 3.3 Figure Placeholders

![Topology diagram](images/topology_new.png)
*Figure 1: Updated triangle topology used in this report.*

---

## 4. Convergence Behavior

At startup, each router knows only directly connected subnets.  
After periodic exchanges, indirect subnets are discovered and metrics stabilize.

### 4.1 Sample Convergence Snapshot

```text
[A] boot complete, neighbors=['10.0.1.2','10.0.3.2']
[A] learned 10.0.2.0/24 via 10.0.1.2, cost=2
[B] learned 10.0.3.0/24 via 10.0.1.1, cost=1
[C] learned 10.0.1.0/24 via 10.0.2.1, cost=1
```

Observation:
- All routers converged to valid end-to-end reachability.
- Alternate routes remained available for failure scenarios.

![Convergence logs](images/convergence_log_new.png)
*Figure 2: Initial convergence evidence.*

---

## 5. Failure and Recovery Testing

### 5.1 Node Failure Test

Test action:

```bash
docker stop router_c
```

Expected behavior:
- Neighbor timeout detects `router_c` as inactive.
- Routes using `10.0.3.2` are invalidated.
- Router A keeps connectivity to remote subnet through Router B if alternate path exists.

### 5.2 Recovery Test

```bash
docker start router_c
```

Expected behavior:
- Fresh updates restore liveness.
- Better/direct metric gets reinstalled.
- Network returns to stable shortest paths.

![Failure and recovery logs](images/failure_recovery_new.png)
*Figure 3: Timeout, reroute, and recovery timeline.*

---

## 6. Loop Prevention Strategy

To reduce count-to-infinity behavior:

- Split horizon: do not advertise a learned route back on the same incoming direction.
- Poisoned metric: advertise unreachable cost toward the origin neighbor for reverse-learned paths.
- Infinity cap: treat large metric as unreachable and avoid unbounded growth.
- Timeout cleanup: purge stale next-hop routes after neighbor inactivity.

These mechanisms improved stability during link/node failures and accelerated reconvergence.

---

## 7. Testing Commands

### 7.1 End-to-End Execution

```bash
bash deploy.sh
bash test.sh
bash teardown.sh
```

### 7.2 Compose-Based Execution

```bash
docker-compose up --build -d
bash test.sh
docker-compose down
```

Suggested result summary to include from your latest run:
- Total tests passed
- Any failed checks
- Time to convergence

---

## 8. Limitations and Improvements

Current limitations:
- Periodic-only updates (no immediate triggered updates on every metric change).
- Basic metric model (hop count only).
- Small topology validation compared to larger realistic networks.

Possible improvements:
- Triggered updates for faster convergence.
- Route aging/hold-down tuning.
- Per-interface cost support.
- Structured log export for automated report plots.

---

## 9. Conclusion

The implementation demonstrates a working custom DV router with real kernel route integration in containers.  
The system converges after startup, adapts to failures, and recovers when nodes return online.  
This assignment provides practical understanding of Bellman-Ford-based distributed routing and operational trade-offs in dynamic networks.
