#!/usr/bin/env bash
# First-time SSL certificate setup for brdg.tools
# Run on VPS: bash infra/nginx/init-ssl.sh
set -euo pipefail

DOMAIN="brdg.tools"
EMAIL="${CERTBOT_EMAIL:-admin@brdg.tools}"

echo "==> Getting SSL certificate for $DOMAIN"

# Ensure certbot is installed
if ! command -v certbot &> /dev/null; then
    echo "Installing certbot..."
    apt-get update && apt-get install -y certbot
fi

# Create webroot dir (shared with nginx via Docker volume)
WEBROOT="/var/lib/docker/volumes/bridge-v2_certbot_webroot/_data"
mkdir -p "$WEBROOT"

# SSL cert storage (shared with nginx via Docker volume)
CERT_DIR="/var/lib/docker/volumes/bridge-v2_ssl_certs/_data"
mkdir -p "$CERT_DIR"

# Get certificate using webroot (nginx must be running and serving port 80)
certbot certonly \
    --webroot \
    --webroot-path "$WEBROOT" \
    --config-dir "$CERT_DIR" \
    --work-dir /tmp/certbot-work \
    --logs-dir /tmp/certbot-logs \
    -d "$DOMAIN" \
    --email "$EMAIL" \
    --agree-tos \
    --non-interactive

echo "==> Certificate obtained! Restart nginx:"
echo "    docker compose restart nginx"

# Set up auto-renewal cron
CRON_CMD="0 3 * * * certbot renew --config-dir $CERT_DIR --work-dir /tmp/certbot-work --logs-dir /tmp/certbot-logs --post-hook 'cd $HOME/bridge-v2 && docker compose restart nginx' --quiet"
(crontab -l 2>/dev/null | grep -v certbot; echo "$CRON_CMD") | crontab -
echo "==> Auto-renewal cron added"
