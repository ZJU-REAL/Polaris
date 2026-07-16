<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/polaris-logo-dark.svg">
    <img src="docs/assets/polaris-logo.svg" alt="Polaris" width="340">
  </picture>
</p>

<h1 align="center">Polaris · 自动 AI 科研平台</h1>

面向大模型 / 智能体领域的实验室级自动科研平台，覆盖完整科研流程：

> 文献调研 → Idea 生成 → Idea 评审 → 实验搭建 → 论文撰写 → 论文评审

核心特性：

- **Research Wiki**：持续沉淀的文献知识库（"compile, don't retrieve"），arXiv / Semantic Scholar / OpenAlex 增量抓取，LLM 精读编译为中文交叉链接 Wiki，可导出 Obsidian vault
- **Idea Forge**：基于知识库的 gap 分析与检索规划式 idea 生成，四维打分 + 语义去重 + 收敛漏斗
- **多 agent + 多人评审**：可配置人设的评审 agent 辩论（Elo 锦标赛），实验室成员实时讨论，人机同场
- **Experiment Lab**：agent 通过 SSH 连接实验室 GPU 服务器自动写代码、跑实验、回收日志与指标
- **Paper Writer**：在线 LaTeX 编辑（CodeMirror 6）+ 服务端 tectonic 编译预览，agent 分节起草
- **Paper Review**：引用逐条核验（存在性/正确性）+ 多视角顶会评审
- **长时程 Agent 核心（Voyage）**：每个复杂任务是一次可恢复的"航程"——Navigator（规划）· Helm（执行）· Sextant（自验证）三元组闭环 + 持久化任务状态机，支撑跨小时/跨天的 long-running 科研任务
- **人在环闸门**：idea 晋级、算力预算、远程写操作、论文投稿等关键节点需人工审批
- **多用户**：JWT 认证、邀请码注册、RBAC，面向实验室 ~20 人

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | React 18 + TypeScript + Vite + TanStack Query，浙大蓝设计系统 |
| 后端 | FastAPI (async) + SQLAlchemy 2 + Alembic + fastapi-users |
| 任务队列 | ARQ (Redis) |
| 数据 | PostgreSQL (+pgvector) + Redis |
| LLM | 多供应商抽象层（OpenAI 兼容 / Anthropic） |
| 部署 | Docker Compose |

## 快速开始（开发）

```bash
cp .env.example .env        # 按需修改
make dev                    # docker compose 起全栈（热重载）
# 前端 http://localhost:5173  后端 http://localhost:8000/docs
```

无 Docker 的本地开发：

```bash
make backend-dev            # venv + uvicorn（需本地 postgres/redis 或 SQLite 回退）
make frontend-dev           # npm install + vite dev
```

## 目录结构

```text
backend/    FastAPI 应用 + ARQ worker
frontend/   React SPA
deploy/     Dockerfile 与 compose
docs/       架构文档、原型参考源码
```

详见 [docs/architecture.md](docs/architecture.md)。
