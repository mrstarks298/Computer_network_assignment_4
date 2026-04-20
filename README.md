# Custom Distance-Vector Router

This project implements a simplified Distance-Vector routing daemon in Python, running inside Docker containers as virtual routers.  
Routers exchange DV-JSON updates over UDP, compute shortest paths using Bellman-Ford logic, and update Linux kernel routes dynamically.

## Project Files

- `router.py` - routing daemon implementation
- `Dockerfile` - router container image definition
- `docker-compose.yml` - triangle topology orchestration
- `deploy.sh` - startup helper script
- `test.sh` - integration test suite
- `teardown.sh` - cleanup script
- `REPORT.md` - assignment report

## Prerequisites

- Docker
- Docker Compose
- Bash shell

## How To Run

### Option 1: Using helper scripts

```bash
bash deploy.sh
bash test.sh
bash teardown.sh
```

### Option 2: Using Docker Compose directly

```bash
docker-compose up --build -d
bash test.sh
docker-compose down
```

## Expected Outcome

On a successful run, tests should report:

`16 passed, 0 failed`
