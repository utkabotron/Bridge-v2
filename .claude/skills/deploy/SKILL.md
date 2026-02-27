# Deploy Skill
1. Run `git add -A && git commit -m "$DESCRIPTION"` (ask user for description if not provided)
2. Run `git push origin main`
3. SSH to production server and restart the service: `sudo systemctl restart dashboard`
4. Verify the service is running: `curl -s -o /dev/null -w "%{http_code}" http://localhost:PORT/health`
5. Report success or failure
