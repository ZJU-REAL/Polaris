# 快速上手

这篇指南带你把 Polaris 在自己的机器上跑起来，并完成第一次登录。推荐走 Docker 路线，除 Docker 外无需安装任何东西；如果只做后端或前端开发，也有免 Docker 的本地路线。生产部署见 [Deployment](../../docs/deployment.md)（英文），完整环境变量参考见 [Configuration](../../docs/configuration.md)（英文）。

## 环境自检

Docker 路线（推荐）需要：

- **Docker Engine 24+** 和 **Docker Compose v2**（`docker compose` 子命令，不是旧的 `docker-compose` 二进制）。用 `docker --version` 和 `docker compose version` 确认。
- **空闲端口**：`5173`（Vite 开发服务器）、`8000`（API，可用 `POLARIS_API_PORT` 改）、`8080`（生产模式的 nginx 前端，可用 `POLARIS_FRONTEND_PORT` 改）。
- **磁盘**：镜像比较大，仅共享的 TeX 基础镜像（tectonic + TeX Live + 中日韩字体）就有数 GB。镜像加数据卷预留约 20 GB。
- **内存**：完整栈要跑五个服务（Postgres、Redis、API、worker、前端），8 GB 起步比较从容。
- **网络**：首次构建要从 GitHub 下载 tectonic 二进制和字体包，还要装 apt 包和 pip 依赖。如果网络访问这些源不畅，见 [Deployment](../../docs/deployment.md#restricted-networks) 的镜像构建参数，以及下方[常见首跑报错](#常见首跑报错)第一行。

免 Docker 路线还需要：

- Python 3.12 及以上
- Node.js 18 及以上（含 npm）

> [!TIP]
> Docker 路线不需要本地装 Python、Node、PostgreSQL 或 Redis，数据库和缓存都在容器里，且支持热重载。

## 1. 克隆仓库

```bash
git clone https://github.com/ZJU-REAL/Polaris.git polaris
cd polaris
```

## 2. 配置 `.env`

复制示例文件，改你需要的值：

```bash
cp .env.example .env
```

第一次正式运行前，至少设置这几项：

| 键 | 作用 |
| --- | --- |
| `POLARIS_SECRET_KEY` | 签发 JWT 登录令牌。用 `openssl rand -hex 32` 生成一个。 |
| `POLARIS_ENCRYPTION_KEY` | 加密 SSH 凭据的 Fernet 密钥。用 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成。开发环境可以留**空**（会从 secret key 派生），但不要保留模板里的占位符：它不是合法的 Fernet 密钥，保存 SSH 凭据会失败。 |
| `POLARIS_INVITE_CODE` | 注册用的静态兜底邀请码，默认 `polaris-lab`。管理员之后可以在应用里创建受管注册码。 |
| `POLARIS_OPENAI_COMPAT_API_KEY` 和/或 `POLARIS_ANTHROPIC_API_KEY` | 至少一个模型服务商的密钥。OpenAI 兼容接口的 base URL 默认指向 DeepSeek，可以把 `POLARIS_OPENAI_COMPAT_BASE_URL` 指到你用的任何 OpenAI 兼容端点。 |

常见的可选项：`POLARIS_S2_API_KEY`（Semantic Scholar，更高的速率限额）和 `POLARIS_OUTBOUND_PROXY`（直连 arXiv / Semantic Scholar / OpenAlex 不稳定时使用；在 Docker 内用 `http://host.docker.internal:<端口>`）。完整参考见 [Configuration](../../docs/configuration.md)。

> [!NOTE]
> 模型密钥和模型路由表之后也可以在运行中的应用里通过管理面板修改，`.env` 里的值只是初始种子。

## 3. 启动完整栈（Docker）

```bash
make dev
```

首次运行会做两件重活：先构建共享的 TeX 基础镜像（`make texbase`，之后有缓存，只有 `docker/Dockerfile.texbase` 变了才重建），然后构建并启动所有服务：带 pgvector 的 PostgreSQL、Redis、API、ARQ worker 和前端，源码以 bind-mount 挂载并支持热重载。

**然后执行数据库迁移**（必需：Postgres 的表不会自动创建，只有免 Docker 的 SQLite 路线才会在启动时建表）：

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml \
  exec api alembic upgrade head
```

启动完成后：

- 前端：<http://localhost:5173>
- 后端 API 文档（OpenAPI / Swagger UI）：<http://localhost:8000/docs>

看日志、停服务：

```bash
make logs    # 跟踪所有服务日志
make down    # 停止并移除容器
```

## 4. 第一次登录

1. 打开前端 <http://localhost:5173>。
2. 用 `POLARIS_INVITE_CODE` 里的邀请码注册（默认 `polaris-lab`）。没有配置 SMTP 时，注册不需要邮箱验证码。
3. **第一个注册的账号会自动成为平台管理员。**

## 5. 登录后做什么

按顺序做，每一步为下一步铺路。

1. **配置模型服务商和路由**：进入 **Admin → LLM admin**（`/admin?tab=llm`）。`.env` 里的密钥会生成初始的服务商条目；在这里可以添加服务商、列出模型，并编辑模型路由表（把每个研究阶段映射到服务商、模型和可选的推理力度）。用户可以在 **Settings → My LLM** 里覆盖自己的路由。在至少一条路由可用之前，AI 功能会返回 `LLM_NOT_CONFIGURED`。
2. **为实验室创建注册码**：**Admin → Codes**（`/admin?tab=codes`）。受管注册码可以带有效期、使用次数上限和预设研究方向（会自动为新用户建一个课题）。静态的 `POLARIS_INVITE_CODE` 一直作为兜底有效，不会把自己锁在外面。
3. **连接 SSH 服务器**（实验阶段需要）：**Settings → SSH credentials**（`/settings?tab=ssh`）。添加主机和密钥，然后点 **Test connection**；凭据用 Fernet 密钥加密存储。管理员侧的实验策略（命令允许/拒绝列表、预算）在 **Admin → Experiments**。
4. **创建第一个方向文献库**：进入 **Libraries**（`/libraries`）创建。结构化的 AI 访谈会帮你写好收录配置（陈述、目标、范围、排除项），运行收录后会构建语料：候选检索、引文滚雪球、相关性打分、全文抽取和 wiki 编译，整个过程是一个可恢复的任务。
5. **创建课题并关联文献库**：新建课题（`/projects/new`），再关联一个或多个方向文献库。课题本身不持有论文，语料是所关联文献库的并集。之后就可以按阶段推进流水线，见 [The Voyage agent core](../../docs/concepts.md)。
6. **可选：启用 PolarisBuddy**：应用内助手的多轮工具循环默认关闭（每轮都重发历史和工具定义，比单轮聊天费钱）。在 `.env` 里设 `POLARIS_CHAT_AGENT_ENABLED=1` 并重启即可启用。
7. **可选：配置每日 arXiv 订阅**：**Admin → Daily papers** 设置订阅的 arXiv 分类和每日抓取时间。

<!-- screenshot: Admin → LLM admin，模型路由表 -->

## 常见首跑报错

| 现象 | 原因与解决 |
| --- | --- |
| `make dev` 在构建 `polaris-texbase` 时失败（GitHub 下载卡住或 apt 很慢） | TeX 基础镜像要从 GitHub 下载 tectonic 二进制和中日韩字体包，还有一次大的 apt 安装。受限网络下传镜像参数：`GITHUB_PROXY=https://gh-proxy.com/ APT_MIRROR=repo.huaweicloud.com make texbase`，然后重跑 `make dev`。PyPI 慢就给 compose 构建传 `PIP_INDEX_URL`。见 [Deployment](../../docs/deployment.md#restricted-networks)。 |
| 启动时报 `port is already allocated` | 5173、8000 或 8080 被占用。设置 `POLARIS_API_PORT` / `POLARIS_FRONTEND_PORT` 并传给 compose（export 出来，或用 `--env-file .env` 跑 compose：Makefile 不会传它，而 compose 的变量插值读的是 compose 文件旁边的 `.env`，不是仓库根目录的）。开发覆盖层里 5173 是固定的。 |
| API 起来了但每个页面都报错，日志里有 `relation "..." does not exist` | 没跑迁移。执行第 3 步里的 `alembic upgrade head`。之后每次更新带来新迁移时也要再跑。 |
| AI 功能返回 `LLM_NOT_CONFIGURED`（HTTP 503） | 没有设置模型密钥，或路由表里没有可用路由。在 `.env` 设密钥后重启，或在 **Admin → LLM admin** 里配置服务商和路由。 |
| 保存 SSH 凭据时报和 Fernet 有关的服务器错误 | `POLARIS_ENCRYPTION_KEY` 还是 `.env.example` 里的占位符，不是合法的 Fernet 密钥。设一个真密钥（见第 2 步），或在开发环境留空。注意之后再换密钥会导致已存的凭据无法解密。 |
| 文献收录什么都搜不到 / arXiv、Semantic Scholar、OpenAlex 超时 | 你的网络直连这些文献 API 不通或不稳。设置 `POLARIS_OUTBOUND_PROXY`（例如 Docker 宿主机上的代理 `http://host.docker.internal:7897`）并重启。 |
| 改了 worker 代码但行为没变 | 开发覆盖层里 worker 的 `arq --watch` 只会重载配置模块。执行 `docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml restart worker`。 |

## 免 Docker 本地路线

只做后端或前端开发时可以分别直接跑。`POLARIS_DATABASE_URL` 不指向 Postgres 时，后端会退回本地 SQLite 数据库（并在启动时建表），快速上手不需要外部数据库。

后端（首次 `make venv` 会创建虚拟环境，然后在 8000 端口跑 uvicorn）：

```bash
make venv          # 一次性：创建 src/backend/.venv 并安装依赖
make backend-dev   # uvicorn app.main:app --reload --port 8000
```

前端（安装依赖并在 5173 端口跑 Vite 开发服务器）：

```bash
make frontend-dev  # npm install && npm run dev
```

> [!WARNING]
> Experiment Lab 会通过 SSH 连接真实的 GPU 服务器并在上面运行生成的代码。远程写入有人工审批和命令允许/拒绝列表把关，但仍建议只把 Polaris 指向你自己掌控的机器，并留意审计日志。

## 下一步

- [中文导览](index.md)：各阶段能做什么，以及各篇指南的入口。
- [Introduction](../../docs/index.md)（英文）：完整的平台介绍。
- [Configuration](../../docs/configuration.md)（英文）：完整环境变量参考。
- [Development](../../docs/development.md)（英文）：本地开发流程、迁移、测试与 Git 约定。
- [Deployment](../../docs/deployment.md)（英文）：生产部署。
