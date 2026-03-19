# Deploy Skill

Deploy Bridge-v2 services to production VPS via rsync + Docker Compose.

## Arguments
- `$ARGS` — optional: service name(s) to deploy (wa-service, processor, bot, analytics). If not provided, deploy ALL services.

## Steps

1. **Run tests before deploying.** Run all three test suites and abort if any fail:
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

3. Rsync project to VPS (no git repo on server):
   ```
   rsync -avz --exclude '.git' --exclude 'node_modules' --exclude '__pycache__' --exclude '.wwebjs_auth' --exclude '.env' --exclude '.venv' ./ bridge:~/bridge-v2/
   ```

4. If specific service(s) requested — build and restart only those. Otherwise build and restart all:
   ```
   ssh bridge "cd ~/bridge-v2 && docker compose build $SERVICE && docker compose up -d $SERVICE"
   ```

5. For wa-service: delete SingletonLock before restart to avoid Chromium lock issues:
   ```
   ssh bridge "cd ~/bridge-v2 && find .wwebjs_auth -name SingletonLock -delete 2>/dev/null; docker compose restart wa-service"
   ```

6. Always restart nginx after up -d to avoid 502 from stale upstream IPs:
   ```
   ssh bridge "cd ~/bridge-v2 && docker compose restart nginx"
   ```

7. Verify health of deployed services:
   ```
   ssh bridge "curl -sf http://localhost:3000/health"  # wa-service
   ssh bridge "curl -sf http://localhost:8000/health"  # processor
   ssh bridge "curl -sf http://localhost:8001/health"  # bot
   ```

8. Report success or failure with health check results.
