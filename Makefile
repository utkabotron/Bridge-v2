.PHONY: up down logs test lint build deploy restart

# ── Local Dev ─────────────────────────────────────────────
up:
	docker compose up -d
	@echo "Services started. Logs: make logs"

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f --tail=100

logs-wa:
	docker compose logs -f --tail=100 wa-service

logs-processor:
	docker compose logs -f --tail=100 processor

logs-bot:
	docker compose logs -f --tail=100 bot

logs-analytics:
	docker compose logs -f --tail=100 analytics

# ── Build ─────────────────────────────────────────────────
build:
	docker compose build

build-no-cache:
	docker compose build --no-cache

# ── Testing ───────────────────────────────────────────────
test:
	cd processor && python -m pytest tests/ -v
	cd bot && python -m pytest tests/ -v

lint:
	cd processor && ruff check src/
	cd bot && ruff check src/
	cd analytics && ruff check flows/

format:
	cd processor && ruff format src/
	cd bot && ruff format src/
	cd analytics && ruff format flows/

# ── DB ────────────────────────────────────────────────────
migrate:
	docker compose exec postgres psql -U bridge -d bridge -f /docker-entrypoint-initdb.d/001_initial_schema.sql

db-shell:
	docker compose exec postgres psql -U bridge -d bridge

# ── Production deploy helpers ─────────────────────────────
deploy-processor:
	aws ecs update-service --cluster bridge-v2 --service processor --force-new-deployment

deploy-bot:
	aws ecs update-service --cluster bridge-v2 --service bot --force-new-deployment

deploy-analytics:
	aws ecs update-service --cluster bridge-v2 --service analytics --force-new-deployment

# ── Health ────────────────────────────────────────────────
health:
	@curl -s http://localhost:3000/health | python3 -m json.tool
	@curl -s http://localhost:8000/health | python3 -m json.tool
	@curl -s http://localhost:8001/health | python3 -m json.tool
