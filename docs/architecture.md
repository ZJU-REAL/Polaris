# Polaris 架构文档

> 面向大模型/智能体领域的实验室级自动 AI 科研平台。多用户、Docker 部署。
> 流程：文献调研 → Idea 生成 → Idea 评审 → 实验搭建 → 论文撰写 → 论文评审。

## 1. 总体架构

```text
┌─────────────────────────────────────────────────────┐
│  frontend  React 18 + TS + Vite + TanStack Query    │
│  浙大蓝设计系统 · CodeMirror6(LaTeX/MD) · 图表        │
└──────────────┬──────────────────────────────────────┘
        REST(OpenAPI) + SSE(agent流式) + WebSocket(讨论/审批)
┌──────────────┴──────────────────────────────────────┐
│  backend  FastAPI (async) · SQLAlchemy 2 + Alembic  │
│  ├─ auth: fastapi-users (JWT) + RBAC + 邀请码注册    │
│  ├─ api → services → models 三层                     │
│  └─ core: llm 抽象层 · gate 闸门 · security(Fernet)  │
├─────────────────────────────────────────────────────┤
│  worker  ARQ (asyncio 任务队列, Redis broker)        │
│  ├─ Voyage agent 核心（见 §3）                       │
│  ├─ pipelines: 文献 ingest · idea forge · 评审辩论   │
│  ├─ executor: asyncssh → 实验室 GPU 服务器           │
│  └─ latex: tectonic 编译                             │
├─────────────────────────────────────────────────────┤
│  PostgreSQL(+pgvector) · Redis · 文件卷(PDF/产物)    │
└─────────────────────────────────────────────────────┘
外部: arXiv · Semantic Scholar · OpenAlex · LLM APIs · GPU服务器(SSH)
```

分层铁律（见 CLAUDE.md）：路由薄；业务在 services；LLM 只经 `core/llm` 抽象层；
确定性逻辑（抓取/解析/去重/水位线）用代码，判断性任务（打分/综合/生成）才交给 LLM。

## 2. LLM 抽象层

`app/core/llm/`：

- `base.py`：`LLMProvider.complete()/stream()` 统一接口
- Provider 实现：`openai_compat`（DeepSeek/Qwen/GPT 等）、`anthropic`
- `router.py`：**模型路由表**（科研环节 → provider+model），存 DB、管理端可改。
  例：文献打分用便宜模型，idea 辩论/论文起草用强模型
- 用量与成本记账：每次调用记录 tokens/费用，归属到 user + project + voyage

## 3. Voyage — 长时程 Agent 核心（平台创新点）

科研任务天然是 long-running（冷启动回填数小时、实验跑数天）。Polaris 的核心创新是：
**每个复杂任务是一次可恢复、可审计的"航程"（Voyage），由三元组闭环驱动，全程状态持久化**。

命名沿用北极星导航隐喻：

| 组件 | 职责 |
| --- | --- |
| **Navigator**（领航 · planning） | 把目标分解为步骤图（子目标/依赖/预算），执行中依据 Sextant 反馈**动态重规划** |
| **Helm**（掌舵 · executor） | 执行单个步骤：LLM 调用、工具调用、SSH 远程操作、文献 API……产出 observation |
| **Sextant**（六分仪 · self-verification） | 每步完成后对照目标"对星定位"：产出是否满足验收标准？未通过 → 带诊断回传 Navigator 重规划；连续失败 → 暂停并升级为人工闸门 |

### 状态机

```text
planning → executing → verifying ─┬→ (下一步) executing
   ↑            │                 ├→ replanning → planning
   │            ▼                 ├→ paused_gate（人在环审批，批准后恢复）
   └──── paused_error ←───────────┤
                                  └→ done / failed
```

### 持久化（M1 落库）

- `VoyageRun`：goal、kind（ingest/forge/experiment/writing…）、plan JSON、当前 step 游标、
  status、checkpoint JSON、budget/usage、project_id、created_by
- `VoyageStep`：run_id、序号、action 类型与输入、observation、Sextant 判定（pass/fail + 理由）、tokens/耗时

价值：

1. **断点恢复**——worker 重启/崩溃后从 checkpoint 续跑（水位线思想的泛化）
2. **人在环**——步骤可声明需要闸门（算力预算/远程写/晋级），状态机原生支持暂停/恢复
3. **可审计**——每一步的计划、动作、验证结论全留痕，UI 上可回放
4. **成本可控**——预算挂在 Run 上，超限自动暂停

## 4. 数据模型（核心实体）

```text
User ─┬─ ProjectMember ─ Project(研究方向, 含结构化访谈定义)
      │                    ├─ Paper ─ PaperConcept ─ Concept   （知识库，wiki markdown + 双链）
      │                    ├─ Idea（四维打分 + Elo + 状态机）
      │                    ├─ ReviewSession ─ ReviewMessage    （agent/human 同场讨论）
      │                    ├─ Experiment ─ ExperimentRun       （SSH 实验）
      │                    ├─ Manuscript ─ ManuscriptFile      （LaTeX 多文件）
      │                    ├─ Gate（人在环闸门）
      │                    ├─ Activity（活动流）
      │                    └─ VoyageRun ─ VoyageStep           （agent 核心, M1）
```

## 5. 各环节模块设计（里程碑 M2–M5）

### 文献调研 Research Wiki（M2）

- 检索源：OpenAlex（引用图谱/批量元数据，免 key）+ Semantic Scholar（语义检索/TLDR，服务端缓存+令牌桶）+ arXiv（最新预印本+PDF）
- 冷启动：锚点论文引文雪球 → LLM 相关性打分（rubric 来自项目访谈）→ PDF 全文抽取（PyMuPDF）→ Librarian agent 编译中文 wiki 页（TL;DR/方法/可借鉴点/概念双链）
- 增量：每日定时 ingest，水位线断点续跑，成本旋钮（阈值/top-N/模型档位）
- pgvector 语义检索；Obsidian 导出（zip / Git 同步，`[[wikilink]]` + frontmatter）

### Idea 生成与评审（M3）

- Forge：知识库 gap 分析 + 检索规划式生成 → 四维打分（新颖/可行/可操作/影响力）→ 语义去重 → 漏斗收敛
- Review：N 个可配置人设的评审 agent 两两辩论 + 裁判 → Elo 排行；
  **人类评论通过 WebSocket 实时进入讨论，并作为一等输入注入 agent 上下文**；晋级走闸门

### 实验搭建 Experiment Lab（M4）

- 每用户 SSH 凭据（Fernet 加密入库），asyncssh 连接
- 实验 Voyage：读入晋级 idea + wiki 上下文 → Navigator 出实验计划（假设/复现策略/预算）→
  闸门审批 → Helm 建 `~/polaris_runs/<exp_id>/` 写代码 → 冒烟测试（Sextant 验证）→ 提交运行 →
  日志流式回传 + 指标曲线 → 报告
- 安全：远程写操作过闸门、命令黑白名单、全程审计、预算三重上限（总额/单次/并发）

### 论文撰写与评审（M5）

- Writer：LaTeX 多文件项目 + CodeMirror6 + tectonic 服务端编译 → PDF 预览；
  agent 分节起草，硬约束：实验数字只能来自 ExperimentRun.metrics，引用必须对应知识库真实条目
- Review 两阶段：①形式检查 + 逐条引用核验（存在性/正确性）；②多视角顶会评审 agent + 多人讨论 → 投稿闸门

## 6. 实时通信

- **SSE**：agent 流式输出、Voyage 进度（单向，15s 心跳防代理断流）
- **WebSocket**：评审讨论、审批通知、实验日志跟踪（双向）

## 7. 部署

`deploy/docker-compose.yml`：postgres(pgvector) + redis + api + worker + frontend(nginx)。
开发用 `docker-compose.dev.yml` 覆盖（源码挂载 + uvicorn --reload + vite dev）。
nginx 反代 `/api`（SSE 关缓冲）与 `/ws`（Upgrade）。

## 8. 里程碑

| 里程碑 | 内容 |
| --- | --- |
| M0 | monorepo 骨架、auth、浙大蓝 App shell、Docker（已完成） |
| M1 | 项目管理（结构化访谈）、LLM 抽象层+路由表、Voyage 核心落库、SSE/WS、闸门机制 |
| M2 | 文献调研全流程 + Obsidian 导出 |
| M3 | Idea Forge + 多agent/多人评审 |
| M4 | Experiment Lab（SSH） |
| M5 | Paper Writer + Paper Review |
| M6 | 管理端、通知、备份、上线试用 |
