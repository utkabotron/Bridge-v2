FROM node:20-slim

# Puppeteer dependencies
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-sandbox \
    fonts-ipafont-gothic \
    fonts-wqy-zenhei \
    fonts-thai-tlwg \
    fonts-kacst \
    fonts-freefont-ttf \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

WORKDIR /app

COPY package*.json ./
# npm ci, not npm install: the lockfile is the whole point here. whatsapp-web.js rides
# WhatsApp's minified internals, so which exact version ships must never drift silently
# between rebuilds.
RUN npm ci --omit=dev

COPY src/ ./src/
COPY public/ ./public/

# Auth directory for WhatsApp sessions (volume-mounted in production)
RUN mkdir -p .wwebjs_auth

# Drop root. Chromium renders whatsapp-web.js's minified internals — third-party content
# in a browser — so a renderer escape should not land as uid 0. The node:20-slim image
# ships a `node` user at uid 1000; the sessions volume must be owned by it too, which the
# deploy does once with:
#   docker run --rm -v bridge-v2_wa_sessions:/data alpine chown -R 1000:1000 /data
RUN chown -R node:node /app
USER node

EXPOSE 3000

CMD ["node", "src/index.js"]
