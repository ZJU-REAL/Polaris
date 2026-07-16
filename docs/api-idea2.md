# Idea 生成 2.0 API 契约 — 方向发散 · 目标构建 · 方案深耕 · 评审修订

延续既有总则。所有路由挂 `/api`，JWT Bearer，权限=项目成员。
本契约重设计 idea 生成全过程，目标：**高质量、新颖、可行**，载体为完整 Research Proposal。
其中 §1 是对 docs/api-m3.md §1（forge）的升级（端点不变）；m3 §2-4（ideas/锦标赛/讨论）
继续有效，按本文 §7 扩展。

## 0. 设计原则：质量来自机制，不靠单次 prompt

**新颖性三道防线**

1. 发散阶段——gap 从多路**证据信号**挖出来（概念共现空白/论文局限抽取/时间趋势/综述 gap），
   不让 LLM 凭空脑补；
2. 深耕阶段——目标与方案必须 grounding 到具体文献，「已有工作 vs 本工作区别」显式成文；
3. 核查阶段——库内 + 外部（Semantic Scholar/OpenAlex）双重相似检索、逐条差异论证；
   不新颖**不是丢弃而是带对比证据回炉修改**。

**可行性三道防线**

1. 目标阶段——objectives 必须可检验、success_criteria 可量化（Sextant 机械验收）；
2. 方案阶段——实验设计对照项目**资源画像**（GPU/数据可得性/时间），含算力粗估与
   「最小验证实验」；
3. 评审阶段——可行性专职评审员出 must-fix 清单，作者 agent 修订后才入候选池。

**全程漏斗**

```text
阶段0 方向发散(广撒网,产出 sketch) → 人工选点/自动排序
  → 阶段1 目标构建(agent 带文献工具探索) → 目标确认闸门(人)
  → 阶段2 方案深耕 PEV 循环(设计/实验/新颖性核查/风险)
  → 阶段3 评审-修订循环(四专职评审员 × 作者修订)
  → 入候选池 → 锦标赛(既有) → 晋级闸门(既有)
```

阶段 1-3 为一个 Voyage（kind=`idea_proposal`），真实使用引擎 Sextant 验收 + 失败重规划；
同项目同时只允许一个 idea 类 voyage（与 forge/review 共用 409）。

## 1. 阶段 0 · 方向发散（forge 升级，端点/响应不变）

`POST /projects/{pid}/forge` knobs 新增 `"signals"`（默认全开）：

```json
{"knobs": {"num_ideas": 8, "signals": ["survey_gap", "concept_holes", "limitations", "trends"]}}
```

管线升级为：**信号采集（确定性优先，并行）→ 方向综合（LLM）→ 打分 → 去重 → 入库（depth=sketch）**

| 信号 | 实现 |
| --- | --- |
| `concept_holes` | paper_concepts 共现矩阵找「method×problem 等类别间各自高频但零共现」的概念对（纯 SQL/代码） |
| `limitations` | 有 full_text 的论文定位局限/未来工作段落（标题启发式截取）→ LLM 摘要为 gap（stage=`forge_signal`，路由便宜模型，逐篇隔离失败） |
| `trends` | 近 90 天入库论文的概念频次增速 top N（纯代码） |
| `survey_gap` | 现有 gap_analysis（compiled wiki 综述 → LLM） |

方向综合约束多样性：每个 sketch 必须声明依据的信号（写入 `evidence`），不同 sketch 强制
绑定不同信号组合。打分/去重/入库沿用现有动作，content 保持四段式（sketch 本就是草案）。

## 2. 发起深耕

- `POST /projects/{pid}/ideas/deep` body：

```json
{
  "seed": {"type": "text|concept|paper|idea", "value": "自由文本 或 concept/paper/idea 的 id"},
  "knobs": {"confirm_goal": true, "max_tool_calls": 15, "external_search": true,
            "revise_rounds": 2, "budget_tokens": null}
}
```

→ `VoyageRead`（kind=`idea_proposal`；并发冲突 409；seed 引用对象不存在 404）。
`seed.type=idea`（通常是 sketch）时继承其 evidence 作为探索起点；产出的新 idea 记
`seed_idea_id`。`budget_tokens` 写入 run.budget（引擎已支持超限自动暂停）。

- `GET /projects/{pid}/ideas/deep/state` → `{running_voyage_id|null, pending_gate_id|null, last_run}`

## 3. 阶段 1 · 目标构建（action `goal.explore`）

单个 Helm 步骤内跑**有界工具循环**（stage=`goal_explore`）：LLM 每轮输出 JSON 决策
`{"tool": "...", "args": {...}}` 或 `{"finish": {goal}}`，上限 `max_tool_calls` 轮，耗尽强制
finish。工具全部确定性代码（复用既有 services）：

| 工具 | 语义 |
| --- | --- |
| `search_papers {query, mode: keyword\|semantic, k}` | 库内检索（semantic 不可用自动降级 keyword） |
| `read_wiki {paper_id}` | 论文 wiki 页（截断 8000 字符） |
| `read_fulltext {paper_id, query?, page?}` | 全文分片：有 query 返回最相关段落窗口，否则按 page 分页 |
| `get_concept {name}` | 概念详情 + 共现相关概念 top10 + 关联论文 |
| `list_concepts {category?}` | 项目概念清单 |

system prompt 强制探索 checklist（Sextant 抽查轨迹）：①已有哪些工作（要检索证据）
②真正的 gap 是什么 ③我们的独特切入角 ④需要什么资源。
探索轨迹（逐轮 tool/args/结果摘要）记入 `checkpoint.goal_trace`，可审计回放。

**goal schema**（存 `checkpoint.goal`，最终随 Idea 落库）：

```json
{
  "research_type": "method|benchmark|analysis|survey|application|theory",
  "task": "研究任务（领域内的具体任务）",
  "question": "核心研究问题（一句话）",
  "objectives": ["具体、可检验的研究目标，1-5 条"],
  "scope": {"in_scope": [], "out_of_scope": []},
  "success_criteria": ["怎样算成功（可量化优先）"],
  "grounding": [{"paper_id": "uuid", "why": "该文献与目标的关系（支撑/空白/对比）"}],
  "key_concepts": ["概念名"],
  "resources_needed": {"compute": "算力需求描述", "data": ["数据集名（是否公开可得）"], "time_weeks": 8}
}
```

**Sextant 验收**（不过 → 带诊断重规划重跑）：必填字段齐全非空；research_type 在枚举内；
objectives 1-5 条；grounding ≥3 篇且均在库内（库内不足 3 篇放宽为全部现有论文）；
resources_needed.data 每项标注可得性。

## 4. 目标确认闸门（kind=`idea_goal`）

- `confirm_goal=true` 时创建 Gate：payload 含 `goal` + `trace_summary`（探索了哪些文献/概念，
  一段话）+ `voyage_id`，复用 M1 审批机制与 WS `gate.created`
- 批准 → 续跑；**comment 非空**先执行 `goal.refine`（并入审批意见，重过 Sextant）再进阶段 2
- 驳回 → voyage failed

## 5. 阶段 2 · 方案深耕 PEV 循环

固定骨架 + 步骤内 agentic（各步可继续调用 §3 工具；stage=`proposal`）。各步产出写
`checkpoint.proposal_sections.<key>`，Sextant 按验收标准判定，失败带诊断重规划
（MAX_REPLANS 耗尽 → paused_error）：

| 步骤 | action | 验收要点 |
| --- | --- | --- |
| 相关工作定位 | `proposal.related_work` | 覆盖 goal.grounding 全部论文；`external_search=true` 时另做外部检索（S2/OpenAlex，关键词两轮，走既有 cache/限速），外部文献记入 evidence（不自动入论文库，前端可一键导入）；含「本工作 vs 已有」逐条差异对比；引用 `[[paper:uuid]]`（外部文献用 `[标题](url)`） |
| 方案设计 | `proposal.design` | 按 research_type 模板：method→方法设计/创新点/理论依据；benchmark→任务定义/数据构建/评测协议/防污染；analysis→假设/分析框架/数据来源/统计方法；其余用通用模板。设计选择须给出依据（引文献或论证） |
| 实验与评估计划 | `proposal.experiments` | baselines/datasets/metrics（含主指标）/ablations/算力粗估，对照资源画像（project.definition.resources，无则通用假设并显式标注）；**必须含「最小验证实验」**：1-3 天可出信号的 smoke 设计，同时产出结构化 JSON 存 `checkpoint.smoke_plan`（后续可直接生成 M4 实验） |
| 新颖性核查 | `proposal.novelty_check` | 库内语义检索 + 外部检索双重相似 top-k → LLM 逐条差异论证 → verdict：`novel` 通过；`needs_differentiation` → fail 带对比证据 → 重规划回炉 design；`duplicate`（高度重合且无差异空间）→ 创建 `idea_pivot` 闸门：批准（带意见）→ goal.refine 后从 design 重跑，驳回 → voyage failed。外部检索失败降级仅库内，proposal 中标注「外部核查未完成」，不 fail |
| 风险与备选 | `proposal.risks` | ≥2 条风险各配缓解/备选方案；须覆盖 novelty_check 与资源画像暴露的问题 |
| 汇编入库 | `proposal.assemble` | 汇编 proposal markdown + 摘要 + 四维自评（临时分）→ Idea 入库（status=candidate, depth=proposal）并发 WS `idea.created`；随后进入 §6 评审-修订（终评覆盖自评与正文） |

实现注：goal.explore 与 related_work 为带文献工具的有界循环；design/experiments/risks
用预组装上下文单次调用（goal + grounding 摘录 + 前序章节 + 资源画像），成本更低且可控。
各步机械验收由动作自带（observation.self_check），Sextant 直接采信；重规划为**确定性规则**
（novelty 三档分支/失败步骤原位重试），不经 LLM。

## 6. 阶段 3 · 评审-修订循环（action `proposal.review_revise`）

四位**专职评审员**（stage=`proposal_review`）各按单一维度出结构化意见：

- 新颖性官（对照 evidence 里的相似文献）· 方法论官（设计漏洞/混淆变量/评测有效性）·
  可行性官（资源/数据/时间是否成立）· 影响力官（结论对谁有用、可推广性）
- 各输出 `{score: 0-10, must_fix: ["…"], suggestions: ["…"]}`

流程：评审 → must_fix 非空则作者 agent 修订对应章节 → 重评；至多 `revise_rounds`
（默认 2）轮修订。逐轮意见落库 `ReviewSession`（target_type=`idea_revision`，target_id=idea，
payload 含各轮分数与遗留清单）+ `ReviewMessage`（author_name=评审员人设，复用 WS
`review.message`）；`GET /ideas/{id}/sessions` 一并返回该类型会话。
终轮四维分数写入 `Idea.scores`（取代自评，维度映射：新颖性→novelty、方法论→operability、
可行性→feasibility、影响力→impact）；轮次耗尽仍有 must_fix → 照常入池，残留项写入
proposal「遗留问题」节（人工可见）。

**Proposal markdown 结构**（写入 `Idea.content`；`[[paper:uuid]]` 渲染为库内论文链接）：

```text
# 标题
## 研究目标        （goal 渲染：类型/任务/问题/目标/成功标准/资源需求）
## 背景与相关工作  （含差异对比）
## 研究方案设计
## 实验与评估计划  （含最小验证实验）
## 预期成果与产出
## 风险与备选方案
## 新颖性核查      （相似工作逐条差异表）
## 遗留问题        （评审未清零的 must_fix，可为空）
```

## 7. 数据模型与 Ideas API 扩展

Idea 新列（alembic，历史数据 depth 迁移为 `sketch`）：

- `depth String(16)`：`sketch`（阶段 0 草案）| `proposal`（深耕产物）
- `research_type String(32) NULL`、`goal JSON NULL`、`seed_idea_id UUID NULL`
- `evidence JSON NULL`：`[{paper_id|null, title, url|null, why, source: "library"|"external"|"signal"}]`

API：

- `IdeaRead` 增加 `depth`、`research_type`；`IdeaDetail` 增加 `goal`、`evidence`、
  `seed_idea: {id, title}|null`
- `GET /projects/{pid}/ideas?depth=&research_type=` 新增过滤
- **锦标赛只在同 depth 内配对**（sketch 对 sketch、proposal 对 proposal，厚薄不对称不同场）；
  proposal 场辩论上下文截断放宽到 4000 字符；leaderboard 项增加 `depth`
- 状态机、promote 闸门、讨论区、人类评论注入机制全部不变

## 8. LLM stages 与事件

- STAGES 新增：`forge_signal`（信号摘要，便宜模型）、`goal_explore`、`proposal`、
  `proposal_review`（前端 LLM_STAGES 同步；无路由走 default）；novelty 相似检索复用既有
  embed/rerank 路由
- SSE 复用 voyage 事件流；探索/检索每轮发 `log` 事件（如 `检索「…」→ 5 篇`），前端实时可见
- WS 复用 `gate.created`、`voyage.status`、`review.message`；新增 `idea.created`

## 9. 前端要点

- Ideas 页：列表增加 depth 徽标（「草案 / 研究方案」）与 research_type 徽标、对应过滤器；
  sketch 行内按钮「深化为研究方案」；主按钮「深度生成」抽屉选种子（自由文本/概念/论文/
  从草案深化）+「生成前确认研究目标」开关（默认开）
- 运行监控复用 voyage 详情页：步骤时间线 + 探索/检索日志流
- 审批中心新增两类卡片：`idea_goal`（goal 结构化展示，批准框提示「可填写修改意见，AI 将
  按意见调整目标后继续」）、`idea_pivot`（展示重合文献对比，选择「调整方向继续 / 终止」）
- Idea 详情页：目标卡片、proposal markdown 渲染（库内引用可点跳 wiki，外部文献带
  「导入文献库」按钮）、评审修订记录（逐轮意见时间线，复用讨论组件）、「依据文献」列表
- 文案大白话：「方向草案 / 深度生成 / 研究目标 / 研究方案 / 目标确认 / 方向调整确认」，
  不出现 voyage/gate/sketch 等英文或隐喻词
