COMPOSE      = docker compose -f docker/docker-compose.yml
COMPOSE_DEV  = docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml
BACKEND_PY   = src/backend/.venv/bin/python
BACKEND_PIP  = src/backend/.venv/bin/pip

.PHONY: dev up down logs backend-dev frontend-dev venv migrate test lint build texbase \
        desktop-deps desktop-dev desktop-shell desktop-dist

## ---- Docker ----
texbase:        ## Build the shared TeX base image (api/worker FROM it; cached unless Dockerfile.texbase changed)
	docker build -f docker/Dockerfile.texbase \
	  --build-arg GITHUB_PROXY="$${GITHUB_PROXY:-}" \
	  --build-arg APT_MIRROR="$${APT_MIRROR:-}" \
	  -t polaris-texbase:latest docker/

dev: texbase    ## Full stack for development (hot reload)
	$(COMPOSE_DEV) up --build

up: texbase     ## Production mode
	$(COMPOSE) up -d --build

down:
	$(COMPOSE_DEV) down

logs:
	$(COMPOSE_DEV) logs -f

## ---- Local (no docker) ----
venv:           ## Create the backend virtualenv and install dependencies
	python3 -m venv src/backend/.venv
	$(BACKEND_PIP) install -e "src/backend[dev]"

backend-dev:    ## Run the backend locally (SQLite fallback, see config.py)
	cd src/backend && .venv/bin/uvicorn app.main:app --reload --port 8000

frontend-dev:   ## Run the frontend locally
	cd src/frontend && npm install && npm run dev

## ---- Desktop (Electron shell) ----
DESKTOP = npm --prefix src/desktop

desktop-deps:   ## Install desktop shell dependencies
	$(DESKTOP) install

desktop-dev:    ## Run the shell against the built frontend (app:// protocol, the real path)
	cd src/frontend && npm run build
	$(DESKTOP) run dev

desktop-shell:  ## Run the shell without rebuilding the frontend
	$(DESKTOP) run dev

desktop-dist:   ## Package an installer for the current platform (unsigned)
	cd src/frontend && npm run build
	$(DESKTOP) run dist:$(shell uname | tr '[:upper:]' '[:lower:]' | sed 's/darwin/mac/')

## ---- Quality ----
migrate:
	cd src/backend && .venv/bin/alembic upgrade head

test:           ## Backend tests + frontend build
	cd src/backend && .venv/bin/pytest -q
	cd src/frontend && npm run build

lint:
	cd src/backend && .venv/bin/ruff check app worker tests
	cd src/frontend && npx tsc --noEmit
	cd src/desktop && npx tsc --noEmit

build: texbase  ## Build production images
	$(COMPOSE) build
