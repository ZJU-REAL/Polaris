<!-- 语言 / Language: **English** · [中文](./README.zh-CN.md) -->

# Polaris — Deploy from Pre-built Images

Deploy Polaris by pulling pre-built images from Docker Hub (or any registry) —
no build on the server. You only need two files: `deploy/docker-compose.yml`
and a `.env`.

## One-time: push the images (on a machine that has them)

```bash
docker login
U=<your-dockerhub-username>
for s in api worker frontend; do
  docker tag  polaris-$s:latest $U/polaris-$s:v1
  docker push $U/polaris-$s:v1
done
```

> `polaris-texbase` does not need to be pushed — it is the build-base for
> api/worker, and its layers are already baked into those two images.
>
> ⚠️ **Match the architecture**: images built on a Mac are `arm64`; an x86_64
> server needs `amd64` images. For cross-arch, build with
> `docker buildx build --platform linux/amd64 --push ...`.

## On the server

```bash
mkdir -p ~/polaris && cd ~/polaris
# Copy in deploy/docker-compose.yml and deploy/.env.example
cp .env.example .env
# Edit .env:
#   - Set POLARIS_IMAGE_PREFIX to your username, POLARIS_IMAGE_TAG to v1
#   - Generate POLARIS_SECRET_KEY / POLARIS_ENCRYPTION_KEY (see inline notes)
#   - The password in POLARIS_DATABASE_URL must match POSTGRES_PASSWORD
#   - Set at least one LLM key (or leave blank and configure later in the admin UI)

docker compose pull            # pull all images
docker compose up -d
docker compose exec api alembic upgrade head   # required on first run: postgres tables aren't auto-created
docker compose ps              # all 5 services healthy/up
```

Open `http://<server-ip>:8080` and register with the invite code. The frontend
nginx already reverse-proxies `/api`, `/ws`, and `/mcp` to the api container, so
only port 8080 needs to be exposed.

## Everyday operations

```bash
docker compose logs -f api worker                             # tail logs
docker compose pull && docker compose up -d \
  && docker compose exec api alembic upgrade head             # update to a newer image
docker compose down                                           # stop (data kept in ./data)
```

All data lives under `~/polaris/data/` (pgdata / redisdata / appdata /
tectonic-cache). Back it up by tarring that directory. **Never run
`docker compose down -v`** — it deletes the data volumes.

## Notes

- **The worker container is not optional**: the ARQ worker runs every
  long-running task (literature fetch, AI generation, experiments, compilation).
  Running only the api leaves those tasks pending forever.
- **Run the migration once**: `alembic upgrade head`; run it again after each
  image update that ships a new migration.
- **LLM keys can be configured later**: the stack boots with them blank; once
  you add a model route in the admin UI, AI features become available. Under
  `POLARIS_ENV=prod` the fake fallback is force-disabled, so it never serves
  fake content.
- **Proxy**: `POLARIS_OUTBOUND_PROXY` only affects the literature APIs and
  GitHub; if the LLM needs a proxy, use the standard `HTTPS_PROXY`/`NO_PROXY`.
