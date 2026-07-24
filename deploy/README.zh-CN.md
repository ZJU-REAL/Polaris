<!-- 语言 / Language: **中文** · [English](./README.md) -->

# Polaris 拉镜像部署

从 Docker Hub（或任意 registry）拉预构建镜像部署，服务器上无需构建。
只需 `deploy/docker-compose.yml` + 一份 `.env` 两个文件。

## 一次性：推送镜像（在有镜像的机器上）

```bash
docker login
U=<你的DockerHub用户名>
for s in api worker frontend; do
  docker tag  polaris-$s:latest $U/polaris-$s:v1
  docker push $U/polaris-$s:v1
done
```

> `polaris-texbase` 不用推——它是 api/worker 的构建基础镜像，层已烤进 api/worker。
>
> ⚠️ **架构要匹配**：Mac 构建出的是 `arm64`，x86_64 服务器需要 `amd64` 镜像。
> 跨架构用 `docker buildx build --platform linux/amd64 --push ...` 出对应架构镜像。

## 服务器端

```bash
mkdir -p ~/polaris && cd ~/polaris
# 拷入 deploy/docker-compose.yml 和 deploy/.env.example
cp .env.example .env
# 编辑 .env：
#   - POLARIS_IMAGE_PREFIX 设为你的用户名，POLARIS_IMAGE_TAG 设为 v1
#   - POLARIS_SECRET_KEY / POLARIS_ENCRYPTION_KEY 按注释生成
#   - POLARIS_DATABASE_URL 里的密码与 POSTGRES_PASSWORD 一致
#   - 至少填一个 LLM key（也可先留空，之后在管理端配）

docker compose pull            # 拉全部镜像
docker compose up -d
docker compose exec api alembic upgrade head   # 首次必跑：postgres 不自动建表
docker compose ps              # 5 个服务 healthy/up
```

浏览器访问 `http://<服务器IP>:8080`，用邀请码注册。前端 nginx 已反代 `/api`、`/ws`、`/mcp` 到 api 容器，只需开放 8080。

## 常用运维

```bash
docker compose logs -f api worker                              # 看日志
docker compose pull && docker compose up -d \
  && docker compose exec api alembic upgrade head              # 更新到新版镜像
docker compose down                                            # 停（数据保留在 ./data）
```

数据全在 `~/polaris/data/`（pgdata / redisdata / appdata / tectonic-cache）。备份 tar 该目录即可，**切勿 `docker compose down -v`**（会删数据卷）。

## 要点

- **worker 容器不能省**：ARQ worker 跑所有长任务（文献抓取、AI 生成、实验、编译），只起 api 会让任务永久 pending。
- **迁移必跑一次**：`alembic upgrade head`；以后有新迁移，更新镜像后再跑一次。
- **LLM key 可事后配**：`.env` 留空也能起，进管理端配模型路由后 AI 功能即可用；`POLARIS_ENV=prod` 下 fake 回退被强制禁用，不会吐假内容。
- **代理**：`POLARIS_OUTBOUND_PROXY` 只作用于文献 API + GitHub；LLM 若需代理走标准 `HTTPS_PROXY`/`NO_PROXY`。
