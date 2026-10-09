BACKEND_PY   = src/backend/.venv/bin/python
BACKEND_PIP  = src/backend/.venv/bin/pip

# Polaris ships as the desktop app only; there is no server stack to run (#842).

.PHONY: help venv backend-dev frontend-dev migrate test lint texbase engine-image \
        desktop-deps desktop-dev desktop-shell desktop-dist golden-record

help:           ## List the targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-15s %s\n", $$1, $$2}'

## ---- Engine from source ----
venv:           ## Create the backend virtualenv and install dependencies
	python3 -m venv src/backend/.venv
	$(BACKEND_PIP) install -e "src/backend[dev]"

backend-dev:    ## Run the engine from source on :8000 (SQLite, in-process queue and scheduler)
	cd src/backend && .venv/bin/uvicorn app.main:app --reload --port 8000

frontend-dev:   ## Run the frontend dev server on :5173
	pnpm install && pnpm --dir src/frontend run dev

## ---- Desktop (Electron shell) ----
DESKTOP = pnpm --dir src/desktop

desktop-deps:   ## Install workspace dependencies (single pnpm lockfile at the repo root)
	pnpm install

desktop-dev:    ## Run the shell against the built frontend (app:// protocol, the real path)
	pnpm --dir src/frontend run build
	$(DESKTOP) run dev

desktop-shell:  ## Run the shell without rebuilding the frontend
	$(DESKTOP) run dev

desktop-dist:   ## Package an installer for the current platform (unsigned)
	pnpm --dir src/frontend run build
	$(DESKTOP) run dist:$(shell uname | tr '[:upper:]' '[:lower:]' | sed 's/darwin/mac/')

## ---- Quality ----
migrate:        ## Apply database migrations (alembic upgrade head)
	cd src/backend && .venv/bin/alembic upgrade head

test:           ## Backend tests + frontend unit tests + frontend build
	cd src/backend && .venv/bin/pytest -q
	pnpm --dir src/frontend test
	pnpm --dir src/frontend run build

lint:           ## ruff + tsc (frontend and desktop)
	# 范围与 python-ci.yml 的 ruff job 保持一致（#746），本地和 CI 判定不分家
	cd src/backend && .venv/bin/ruff check app worker tests alembic
	cd src/frontend && npx tsc --noEmit
	cd src/desktop && npx tsc --noEmit

golden-record:  ## Re-record golden transcripts (intentional wire changes only — read docs/golden-policy.md first)
	# 一次性容器内录制（与 CI 同一套件形状；宿主机不需要 venv）。镜像用 make engine-image 构建。
	# 挂载可写：录制直接改写 src/backend/tests/golden/data/ 下的文件，之后人工审查 diff。
	docker run --rm \
	  -v "$(CURDIR)/src/backend:/srv/backend" -w /srv/backend \
	  -e POLARIS_GOLDEN=record \
	  polaris-api-test:local \
	  sh -c "pip install -q -e '.[dev]' && python -m pytest tests/golden -q -p no:cacheprovider"
	git diff --stat -- src/backend/tests/golden/data

## ---- Dev/test engine image (not a deployment) ----
texbase:        ## Build the shared TeX base image the engine image starts from
	docker build -f docker/Dockerfile.texbase \
	  --build-arg GITHUB_PROXY="$${GITHUB_PROXY:-}" \
	  --build-arg APT_MIRROR="$${APT_MIRROR:-}" \
	  -t polaris-texbase:latest docker/

engine-image: texbase  ## Build polaris-api-test:local (desktop smoke/e2e engine-in-docker mode, golden-record)
	docker build -f docker/Dockerfile.api \
	  --build-arg PIP_INDEX_URL="$${PIP_INDEX_URL:-}" \
	  -t polaris-api-test:local .
