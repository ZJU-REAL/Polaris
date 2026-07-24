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

The `api` and `worker` images build on a shared TeX base image (`docker/Dockerfile.texbase`:
tectonic + TeX Live + CJK fonts). Build it once before the first compose build — it is fully cached
afterwards and only rebuilds when `Dockerfile.texbase` changes:

```bash
make texbase
```

Production is then the base file plus the prod overlay:

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml up -d --build
```

This builds the images, starts everything detached, and restarts services unless stopped. Routine
updates only rebuild the pip/source layers of `api`/`worker`, which takes minutes.

## Deploy from published images (no build)

CI publishes `amd64` images to Docker Hub as `tricktreat/polaris-{api,worker,frontend}` on every
`v*` tag (see `.github/workflows/docker-publish.yml`). The `api`/`worker`/`frontend` services carry
an `image:` of `${POLARIS_IMAGE_PREFIX:-tricktreat}/polaris-<svc>:${POLARIS_IMAGE_TAG:-latest}`, which
doubles as the local build tag and the pull source. So the same compose file can pull instead of
build — no TeX base image or local build needed:

```bash
cp .env.example .env          # set POLARIS_ENV=prod, POLARIS_IMAGE_TAG, secrets, and an LLM key
docker compose --env-file .env -f docker/docker-compose.yml pull
docker compose --env-file .env -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml exec api alembic upgrade head   # first run
```

Add the prod overlay (`-f docker/docker-compose.prod.yml`) as well if you want the host-path bind
mounts described below instead of named volumes. Set `POLARIS_IMAGE_PREFIX` / `POLARIS_IMAGE_TAG` in
`.env` to select the registry namespace and version.

> [!IMPORTANT]
> The `--env-file .env` flag matters for `pull`/`up`: Compose resolves `${POLARIS_IMAGE_TAG}` from the
> interpolation env file, which defaults to `.env` next to the compose file (`docker/`), not the repo
> root. Without the flag it silently falls back to `:latest`. (Container runtime config still comes
> from `env_file: ../.env` regardless.)

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

Three build-time arguments help on networks that cannot reach the public internet directly (or
reach it slowly). They are passed as environment variables, not stored in `.env`.

For the TeX base image (GitHub asset downloads and the big apt install happen here):

```bash
GITHUB_PROXY=https://gh-proxy.com/ APT_MIRROR=repo.huaweicloud.com make texbase
```

For the compose build (pip layer of `api`/`worker`):

```bash
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  docker compose -f docker/docker-compose.yml -f docker/docker-compose.prod.yml up -d --build
```

- `GITHUB_PROXY`: a prefix for the base image's GitHub downloads (tectonic binary, CJK font pack)
  when GitHub is not directly reachable. Changing it only re-runs the small download stage, not the
  TeX Live layers. Leave empty if you have direct access.
- `APT_MIRROR`: a Debian mirror hostname (e.g. `repo.huaweicloud.com`,
  `mirrors.tuna.tsinghua.edu.cn`) used for the base image's apt installs. The substitution persists
  into the image, so images derived from the base (the worker's LibreOffice layer) use it too.
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
