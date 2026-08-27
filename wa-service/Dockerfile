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

# Auth directory for WhatsApp sessions (EFS mount in production)
RUN mkdir -p .wwebjs_auth

EXPOSE 3000

CMD ["node", "src/index.js"]
