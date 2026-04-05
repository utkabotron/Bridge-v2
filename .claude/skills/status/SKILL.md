---
description: Check Bridge v2 production health — containers, logs, errors, resources
model: haiku
---

# Status Skill

Check the overall health and state of Bridge v2 services on the production VPS.

## Arguments
- `$ARGS` — optional: specific check to run (logs, errors, resources, dlq). If not provided, run ALL checks.

## Steps

Run all checks in parallel where possible, then compile a single report.

### 1. Container status

```
ssh bridge "cd /home/deploy/bridge-v2 && docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'"
```

Flag any container that is NOT "Up" or NOT "(healthy)".

### 2. Health endpoints

Run in parallel:

```
ssh bridge "curl -sf http://localhost:8000/health"
ssh bridge "docker exec bridge-v2-wa-service-1 node -e \"fetch('http://localhost:3000/health').then(r=>r.text()).then(console.log)\""
```

- Processor: expect `{"status":"ok"}`
- wa-service: expect `{"status":"ok","activeClients":N,"redis":"connected"}`
- Bot: no health endpoint — verify via logs (step 3)

### 3. Recent logs (errors only)

Run in parallel — grab last 100 lines per service, filter for ERROR/CRITICAL/WARN:

```
ssh bridge "cd /home/deploy/bridge-v2 && docker compose logs --tail 100 processor 2>&1 | grep -iE 'error|critical|exception|traceback|failed' | tail -10"
ssh bridge "cd /home/deploy/bridge-v2 && docker compose logs --tail 100 bot 2>&1 | grep -iE 'error|critical|exception|traceback|failed' | tail -10"
ssh bridge "cd /home/deploy/bridge-v2 && docker compose logs --tail 100 wa-service 2>&1 | grep -iE 'error|critical|CRITICAL|failed|exhausted|lost' | tail -10"
```

Bot: also verify last `getUpdates` is recent (within 30s) — if the gap is >60s, flag as potentially stuck:
```
ssh bridge "cd /home/deploy/bridge-v2 && docker compose logs --tail 3 bot 2>&1 | tail -3"
```

### 4. DLQ and metrics

```
ssh bridge "curl -sf http://localhost:8000/metrics"
ssh bridge "curl -sf http://localhost:8000/api/dlq" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'DLQ messages: {len(d)}')"
```

Flag if DLQ > 0 or failed count is rising.

### 5. Daily stats

```
ssh bridge "curl -sf http://localhost:8000/api/daily-stats"
```

Show today's delivered/failed/skipped counts and avg translation time.

### 6. System resources

```
ssh bridge "free -h | head -2 && echo '---' && df -h / | tail -1 && echo '---' && docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' 2>/dev/null | head -10"
```

Flag if: RAM usage >90%, disk >85%, any container using >500MB.

## Report format

Compile results into a concise status report:

```
## Bridge v2 Status

**Overall: OK / WARNING / CRITICAL**

| Service | Status | Details |
|---------|--------|---------|
| processor | ... | ... |
| wa-service | ... | ... |
| bot | ... | ... |
| postgres | ... | ... |
| redis | ... | ... |
| nginx | ... | ... |

**Today:** X delivered, Y failed, Z skipped, avg Nms
**DLQ:** N messages
**Resources:** RAM X/Y, Disk X%

**Issues found:**
- (list any problems, or "None")
```

If issues are found, suggest specific fix commands.
