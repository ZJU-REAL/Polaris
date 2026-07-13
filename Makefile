COMPOSE      = docker compose -f deploy/docker-compose.yml
COMPOSE_DEV  = docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml
BACKEND_PY   = backend/.venv/bin/python
BACKEND_PIP  = backend/.venv/bin/pip

.PHONY: dev up down logs backend-dev frontend-dev venv migrate test lint build

## ---- Docker ----
dev:            ## 开发环境全栈（热重载）
	$(COMPOSE_DEV) up --build

up:             ## 生产模式启动
	$(COMPOSE) up -d --build

down:
	$(COMPOSE_DEV) down

logs:
	$(COMPOSE_DEV) logs -f

## ---- 本地（无 docker）----
venv:           ## 创建后端虚拟环境并装依赖
	python3 -m venv backend/.venv
	$(BACKEND_PIP) install -e "backend[dev]"

backend-dev:    ## 本地起后端（默认 SQLite 回退，见 config.py）
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

frontend-dev:   ## 本地起前端
	cd frontend && npm install && npm run dev

## ---- 质量 ----
migrate:
	cd backend && .venv/bin/alembic upgrade head

test:           ## 后端测试 + 前端构建
	cd backend && .venv/bin/pytest -q
	cd frontend && npm run build

lint:
	cd backend && .venv/bin/ruff check app worker tests
	cd frontend && npx tsc --noEmit

build:          ## 构建生产镜像
	$(COMPOSE) build
