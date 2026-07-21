# Deployment

Docker Compose is the only supported production path. This guide covers the compose overlays, the
data directory convention, restricted-network build arguments, migrations, ports, and backups. For
local development, see [Development](development.md); for the variables referenced here, see
[Configuration](configuration.md).

## Compose files

Polaris ships three compose files in `docker/`:

- `docker-compose.yml`: the base stack (`postgres`, `redis`, `api`, `worker`, `frontend`).
- `docker-compose.dev.yml`: the development overlay (hot reload, source bind-mounts, Vite dev server).
- `docker-compose.prod.yml`: the production overlay (persistent bind-mounts and restricted-network
  build args).

Production is the base file plus the prod overlay:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml up -d --build
```

This builds the images, starts everything detached, and restarts services unless stopped.

## Data directory convention

The prod overlay persists all state to bind-mounted host directories instead of anonymous volumes, so
data survives container rebuilds and is easy to back up. The convention is:

```text
~/polaris/app     the code (kept in sync on the server, e.g. via rsync or git pull)
~/polaris/data    persistent data
  ~/polaris/data/pgdata           PostgreSQL data
  ~/polaris/data/redisdata        Redis data
  ~/polaris/data/appdata          PDFs and generated artifacts (mounted at /srv/data)
  ~/polaris/data/tectonic-cache   tectonic macro-package cache
```

Paths in the prod overlay are relative to the `docker/` directory, so `../../data` resolves to
`~/polaris/data` when you run compose from `~/polaris/app`. The `api` and `worker` services share both
`appdata` and `tectonic-cache`.

> [!NOTE]
> The `tectonic-cache` volume holds LaTeX macro packages that tectonic downloads on first
> compilation. Sharing it between `api` and `worker` and persisting it across rebuilds avoids
> re-downloading on every build.

## Environment configuration

Compose reads runtime configuration from `../.env` (relative to `docker/`). Copy and edit it on the
server:

```bash
cp .env.example .env
```

Set the production essentials: a strong `POLARIS_SECRET_KEY`, a real `POLARIS_ENCRYPTION_KEY`, the
`POLARIS_INVITE_CODE`, a Postgres `POLARIS_DATABASE_URL`, `POLARIS_REDIS_URL`, and at least one LLM
provider key. See [Configuration](configuration.md) for the full table.

### Restricted networks

Two build-time arguments help on networks that cannot reach the public internet directly. They are
passed as environment variables when you invoke compose, not stored in `.env`:

```bash
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple GITHUB_PROXY=https://gh-proxy.com/ \
  docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml up -d --build
```

- `GITHUB_PROXY`: a prefix that accelerates tectonic's GitHub downloads when GitHub is not directly
  reachable. Leave empty if you have direct access.
- `PIP_INDEX_URL`: an alternate PyPI mirror for the image build when the official index is slow or
  blocked. Leave empty to use the official index.

## Migrations

After the stack is up, apply database migrations by running Alembic inside the `api` container:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml \
  exec api alembic upgrade head
```

Run this on the first deployment and after any deploy that adds migrations.

## Ports and the nginx frontend

- The `frontend` service serves the built React app through nginx on host port `8080` (container
  port 80).
- nginx reverse-proxies `/api` to the backend (with response buffering disabled so SSE streams flow)
  and `/ws` with the WebSocket upgrade.
- The `api` service also publishes port `8000` directly, which exposes the OpenAPI docs at
  `/docs`. In a locked-down deployment you may choose to expose only nginx.

## Backups

Back up the two stores under `~/polaris/data`:

- **PostgreSQL** is the system of record (users, projects, papers, ideas, experiments, manuscripts,
  Voyage runs). Back it up with a logical dump, which is safe while the stack is running:

  ```bash
  docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml \
    exec postgres pg_dump -U polaris polaris > polaris-$(date +%F).sql
  ```

  Alternatively, snapshot the `~/polaris/data/pgdata` directory while the database is stopped.
- **appdata** (`~/polaris/data/appdata`) holds PDFs and generated artifacts; copy the directory as
  needed. Redis (`redisdata`) is a broker and cache and generally does not need backing up.

## Upgrading

1. Update the code under `~/polaris/app`.
2. Rebuild and restart: `docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml up -d --build`.
3. Apply migrations: `... exec api alembic upgrade head`.

> [!IMPORTANT]
> Deploy production only from `origin/main`, never from a local branch. See the Git workflow in
> [Development](development.md).
