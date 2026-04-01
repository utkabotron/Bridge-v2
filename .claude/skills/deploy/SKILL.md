# Deploy Skill

Deploy Bridge-v2 services to production VPS via rsync + Docker Compose.

## Arguments
- `$ARGS` — optional: service name(s) to deploy (wa-service, processor, bot, analytics). If not provided, deploy ALL services.

## Steps

1. **Run tests before deploying.** Run all three test suites in parallel and abort if any fail:
   ```
   cd wa-service && npx jest --forceExit 2>&1
   cd processor && python3 -m pytest tests/ -v 2>&1
   cd bot && python3 -m pytest tests/ -v 2>&1
   ```
   If tests fail — stop and report which tests failed. Do NOT proceed to rsync.

2. Ask user for commit description if not provided. Run:
   ```
   git add -A && git commit -m "$DESCRIPTION"
   ```

3. Rsync project to VPS (no git repo on server). IMPORTANT: target is `/home/deploy/bridge-v2/`, NOT `~/bridge-v2/` (root home):
   ```
   rsync -avz --exclude '.git' --exclude 'node_modules' --exclude '__pycache__' --exclude '.wwebjs_auth' --exclude '.env' --exclude '.venv' ./ bridge:/home/deploy/bridge-v2/
   ```

4. **Apply new migrations** (if any new SQL files in `infra/migrations/`). Pipe each new migration into postgres container:
   ```
   ssh bridge "cd /home/deploy/bridge-v2 && cat infra/migrations/NEW_MIGRATION.sql | docker compose exec -T postgres psql -U bridge -d bridge"
   ```
   Skip this step if no new migrations were added.

5. If specific service(s) requested — build and restart only those. Otherwise build and restart all:
   ```
   ssh bridge "cd /home/deploy/bridge-v2 && docker compose build $SERVICE && docker compose up -d $SERVICE"
   ```

6. ALWAYS restart nginx after `docker compose up -d` — container gets new IP and nginx caches old upstream → 502:
   ```
   ssh bridge "cd /home/deploy/bridge-v2 && docker compose restart nginx"
   ```

7. For wa-service: delete SingletonLock before restart to avoid Chromium lock issues:
   ```
   ssh bridge "cd /home/deploy/bridge-v2 && find .wwebjs_auth -name SingletonLock -delete 2>/dev/null; docker compose restart wa-service"
   ```

8. **Verify health** of deployed services. Wait 15-20s after restart (wa-service needs time for Chromium + WA session restore).

   **IMPORTANT:** wa-service port 3000 is NOT published to host — it's `expose`-only inside docker network. Check health from inside the container:
   ```
   ssh bridge "docker exec bridge-v2-wa-service-1 node -e \"fetch('http://localhost:3000/health').then(r=>r.text()).then(console.log)\""
   ```

   Processor is published on port 8000 — check from host:
   ```
   ssh bridge "curl -sf http://localhost:8000/health"
   ```

   Bot has NO health endpoint (polling-based, not webhook). Verify it's running by checking logs:
   ```
   ssh bridge "cd /home/deploy/bridge-v2 && docker compose logs bot --tail 5"
   ```
   It should show regular `getUpdates` HTTP 200 responses.

9. Report success or failure with health check results.
