# Bridge v2 — Architecture

```mermaid
graph TB
    WA["📱 WhatsApp Groups"]
    TG["✈️ Telegram API"]
    USER["👤 User in Telegram"]

    subgraph wa_service["wa-service · Node.js 20 · port 3000"]
        WA_CLIENT["whatsapp-client.js"]
        WA_MEDIA["media-handler.js"]
        WA_ROUTES["routes · QR · connect · Mini App"]
    end

    subgraph processor["processor · Python FastAPI · port 8000"]
        CONSUMER["BRPOP consumer loop"]
        GRAPH["LangGraph pipeline<br/>validate → translate → format → deliver"]
        SENDER["telegram_sender.py"]
        DASHBOARD["Dashboard · /dashboard"]
    end

    subgraph bot["bot · python-telegram-bot · port 8001"]
        HANDLERS["Handlers · /start · /chats · /add"]
        WIZARD["Onboarding Wizard<br/>idle → qr_pending → wa_connected → done"]
        REDIS_SUB["redis_sub.py · PSUBSCRIBE daemon"]
    end

    subgraph analytics["analytics · Prefect · port 4200"]
        FLOWS["nightly-problems · 4:00<br/>translation-quality · 4:30<br/>weekly-report · Mon 5:00<br/>wa-health · every 15 min"]
    end

    REDIS[("Redis 7")]
    PG[("PostgreSQL 16")]
    MINIO[("MinIO S3")]
    NGINX["nginx · 80 / 443"]

    WA -->|incoming message| WA_CLIENT
    WA_CLIENT -->|upload media| WA_MEDIA
    WA_MEDIA -->|PUT object| MINIO
    WA_CLIENT -->|LPUSH messages:in| REDIS
    REDIS -->|BRPOP messages:in| CONSUMER
    CONSUMER --> GRAPH
    GRAPH -->|GPT-4.1-mini translate| SENDER
    SENDER -->|sendMessage · sendPhoto| TG

    USER -->|/start| NGINX
    NGINX --> bot
    WIZARD -->|GET /status · POST /connect| WA_ROUTES
    WA_CLIENT -->|PUBLISH onboarding:qr_scanned| REDIS
    REDIS -->|PSUBSCRIBE| REDIS_SUB
    REDIS_SUB -->|asyncio event| WIZARD

    WA_CLIENT --> PG
    GRAPH --> PG
    HANDLERS --> PG
    FLOWS --> PG

    FLOWS -->|GET /health| WA_ROUTES
    FLOWS -->|alerts| TG

    NGINX -->|port 3000| wa_service
    NGINX -->|port 8000| processor
    NGINX -->|port 8001| bot
```

## Message flow

| Step | From | To | Transport |
|------|------|----|-----------|
| 1 | WhatsApp | wa-service | whatsapp-web.js |
| 2 | wa-service | MinIO | AWS SDK v3 |
| 3 | wa-service | Redis | LPUSH messages:in |
| 4 | Redis | processor | BRPOP |
| 5 | processor | OpenAI | httpx |
| 6 | processor | Telegram API | httpx |
| 7 | wa-service | bot | Redis pub/sub |
| 8 | bot | wa-service | HTTP REST |

## Stack

| Service | Key libs |
|---------|----------|
| wa-service | whatsapp-web.js, ioredis, pg |
| processor | FastAPI, LangGraph, asyncpg, httpx |
| bot | python-telegram-bot, asyncpg |
| analytics | Prefect, OpenAI, asyncpg |
