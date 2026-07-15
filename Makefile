# =============================================================================
# VoiceBot Platform — common dev tasks
# =============================================================================

.PHONY: help up down build logs ps clean \
        api-shell bridge-shell db-shell migrate makemigration seed \
        test test-bridge test-api lint typecheck format \
        frontend-dev frontend-build

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ---- Stack -----------------------------------------------------------------
up: ## Start all services
	docker compose up -d --build

down: ## Stop all services
	docker compose down

build: ## Rebuild all images
	docker compose build

logs: ## Tail logs from all services
	docker compose logs -f --tail=100

ps: ## List running services
	docker compose ps

clean: ## Stop and remove volumes (DESTROYS DB DATA)
	docker compose down -v

# ---- Shells ----------------------------------------------------------------
api-shell: ## Shell into the api container
	docker compose exec api /bin/sh

bridge-shell: ## Shell into the bridge container
	docker compose exec bridge /bin/sh

db-shell: ## psql into Postgres
	docker compose exec postgres psql -U $${POSTGRES_USER:-voicebot} -d $${POSTGRES_DB:-voicebot}

# ---- Migrations ------------------------------------------------------------
migrate: ## Apply pending migrations
	docker compose exec api alembic upgrade head

makemigration: ## Autogenerate a new migration. Usage: make makemigration MSG="add xyz"
	docker compose exec api alembic revision --autogenerate -m "$(MSG)"

seed: ## Seed demo data (admin user, sample contacts)
	docker compose exec api python -m app.scripts.seed

# ---- Tests -----------------------------------------------------------------
test: test-bridge test-api ## Run all tests

test-bridge:
	docker compose run --rm bridge pytest -q

test-api:
	docker compose run --rm --entrypoint pytest api -q

# ---- Quality ---------------------------------------------------------------
lint:
	docker compose run --rm api ruff check app
	docker compose run --rm bridge ruff check app

typecheck:
	docker compose run --rm api mypy app
	docker compose run --rm bridge mypy app

format:
	docker compose run --rm api ruff format app
	docker compose run --rm bridge ruff format app

# ---- Frontend (host-side dev) ----------------------------------------------
frontend-dev: ## Run the frontend dev server on the host
	cd frontend && npm install && npm run dev

frontend-build:
	cd frontend && npm install && npm run build
