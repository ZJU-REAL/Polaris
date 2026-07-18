# Voyage 任务循环设计 — Plan-Execute-Verify Loop

> 目标：把 plan-execute-verify 从「单次 pipeline + 异常式重规划」升级为
> **可迭代、有状态、可检验的循环**——plan 产出带结构化验收准则的任务节点，
> execute/verify 更新节点状态，结果回灌 plan 驱动计划演化；同时承认并显式化
> 「确定性工作流不需要认知循环」：Voyage 拆成**外壳（Runtime）与大脑（Brain）两层**，
> 所有 kind 共用外壳，只有判断性任务启用大脑。
>
> 外部依据见 `docs/anthropic-agent-design.md`（Anthropic 官方 long-running agent 文档调研）。
> 现状实现见 `agents/voyage/engine.py`、`docs/architecture.md` §3。

## 0. 设计原则

1. **最简方案优先**（[BEA]）：步骤可预测的任务用固定计划 + 机械校验，不进认知循环；
   只有"无法预先预测步骤数、无法硬编码路径"的任务（按 proposal 做实验、自由研究）
   才启用完整 loop。简单任务被过度编排 = 白花延迟与 token。
2. **判断性工作留给模型，流程/状态/验收全部确定性化**（[Harness] + 平台既有铁律）：
   计划是机器可校验的 JSON 节点清单（带状态位），不是自由文本；
   验收准则由 plan 预先制定且尽量可机械判定。
3. **重规划是常态转移，不是异常**：循环的缰绳是**预算与节点尝试上限**，
   不是全局重规划次数（现状 `MAX_REPLANS=2` 只对档 0/1 成立）。
4. **End-state 验证**（[MultiAgent]）：验证器断言"正确的最终状态/产物是否达成"，
   允许等效替代路径，不重放过程。
5. **防四种失败模式**（[Harness]）机制化：节点 `passed` 状态位防过早宣告完成；
   voyage 级完成标准防"提前走完清单"；单节点小步增量防 one-shot；
   恢复时健康检查防环境失续。
6. **界面文案大白话**：用户可见叫「任务计划 / 任务步骤 / 自动校验 / 计划调整 /
   完成标准 / 审批」，不出现 node/graph/replan/acceptance 等术语（代码标识符不受限）。

## 1. 现状诊断（为什么要改）

| # | 错位 | 现状证据 |
| --- | --- | --- |
| 1 | **plan 是一次性编译产物，不是活的任务板** | `run.plan` 线性列表 + 整数 `cursor`；Navigator 只在开局 `plan()` 和失败 `replan()` 被调用；`MAX_REPLANS=2` 把重规划当错误恢复；Navigator 无状态，replan 只拿到失败步骤 + 一条诊断字符串 |
| 2 | **verify 准则退化** | `acceptance` 是一行自然语言，判定靠 LLM 主观裁决（官方评价 LLM-as-judge "not robust"）；所有 `wiki./forge./experiment./writing.` 前缀动作被 Sextant 前缀白名单直接放行，verify 名存实亡 |
| 3 | **真正的动态循环逃逸出框架** | `experiment` kind 反而是最僵硬的固定六步（全 `on_failure="fail"`、不重规划）；设计→实现→运行→分析→调整的循环整个埋在 `experiment.iterate` 单个 action 内部：轮次不是 VoyageStep，审计/回放/UI 看不见，循环内插不进闸门，方法级 pivot 无法上升到计划层 |

同时现状有大量该保留的资产：checkpoint 恢复、协作式取消（条件 UPDATE 防覆盖
cancelled）、Gate 机制、预算暂停、usage 台账、SSE 事件流、技能快照——全部原样进入外壳层。

## 2. 分层：Runtime 外壳 vs Brain 认知循环

| 层 | 内容 | 谁用 |
| --- | --- | --- |
| **Voyage Runtime（外壳）** | 持久化状态机、节点落库、checkpoint/断点恢复、Gate、预算、协作式取消、SSE 事件、usage 台账、技能快照 | 所有 kind |
| **Voyage Brain（认知循环）** | Navigator 维护任务计划（增量编辑）、Sextant 按结构化准则判定、结果回灌驱动计划演化 | 仅 `mode=loop` 的 kind |

落地方式：`VoyageRun` 增加 `mode` 字段（由 kind 决定，不需要用户感知）：

```text
mode = pipeline  档0：固定计划模板 + 机械校验；失败即 failed；零 Navigator/Sextant LLM 调用
mode = template  档1：固定骨架 + 确定性重规划分支表；LLM 仅在分支表未覆盖时兜底
mode = loop      档2：完整 plan-execute-verify 循环，Navigator 全程在环
```

## 3. kind 三档分类

| 档 | kind | 理由 |
| --- | --- | --- |
| 0 pipeline | `wiki_bootstrap` / `wiki_ingest` / `idea_forge` / `idea_review` / `paper_review` / `presentation` / `paper_writing` | 步骤完全可预测；现状事实上已绕过认知循环（固定模板 + Sextant 白名单放行），本设计只是承认它并去掉伪装 |
| 1 template | `idea_proposal` | 骨架固定但有明确评估准则与迭代价值（evaluator-optimizer 形态）；确定性重规划分支（DUPLICATE→pivot 闸门、NEEDS_DIFFERENTIATION→回炉设计）已存在，保留 |
| 2 loop | `experiment`（重构后）、未来的自由研究 kind | 步骤数不可预测（跑几轮、要不要换方法、补哪些消融都取决于中间结果）；高价值撑得起循环成本 |

`demo` kind 保留为 loop 模式的最小演示。

## 4. 任务节点模型

`VoyageStep` 演进为节点（表名不变，迁移加列；对用户展示叫「任务步骤」）：

```text
VoyageStep（节点）:
  id, run_id, seq                  # seq = 创建序，落库后不可变（审计与引用锚点）
  rank: numeric                    # 清单序 = 执行序；gap 编号（100/200/…），插入取间隙值，seq 不动
  title, action, params            # action 仍必须在 actions.py 注册表内
  acceptance: {checks: [...]}      # 结构化验收，见 §6；由 plan 生成节点时一并制定
  requires_gate: str | null        # 闸门类型（沿用机制；checkpoint["gates"] 改按 step_id 键控）
  budget: {max_attempts?: int, max_tokens?: int, max_gpu_hours?: float}
                                   # max_attempts 默认：pipeline=1（副作用步骤不隐式重试，
                                   #   需重试的步骤显式声明并逐一核查幂等）；template/loop=2
                                   # max_gpu_hours 由动作自查上报、引擎只记账（§7），不是引擎强制
  status: pending → running → verifying → passed
                               ↘ failed | obsolete
  attempt: int                     # 当前尝试次数
  attempts: [...]                  # 每次尝试的完整归档（observation/verdict/tokens/起止时间）
  observation, verdict, tokens, started_at, finished_at   # 最近一次尝试（便于查询的冗余）
  provenance: {plan_iteration: int, reason: str}          # 哪次计划迭代创建/修改了它
```

`VoyageRun` 增量：

```text
mode: pipeline | template | loop
plan_iteration: int                    # 计划演化版本号，每次计划编辑 +1
done_criteria: {checks: [...]}         # voyage 级完成标准（§5.4），plan 预置
# run.plan 降级为派生快照（兼容现有 API/前端）：节点表是唯一真源，
# 每次计划变更后由节点表单向重新生成；任何代码不得再直接写 run.plan
```

状态语义：`obsolete` = 被计划编辑作废（留痕不删除，替代现状 checkpoint["replaced_steps"]
的档案做法）；失败重试不新建行——同一节点累加 `attempt`，每次尝试完整归档进 `attempts`
列。注意 SSE 事件是 Redis pub/sub、无人订阅即丢：事件流只做实时展示，**审计留痕一律落库**。

## 5. 循环引擎

### 5.1 主循环（替代 cursor 推进）

```text
loop:
  1. 协作式取消检查（沿用：查 DB status）
  2. run 级预算检查（沿用：超限 → paused_error）
  3. 按清单顺序取第一个非终态节点；清单已走完 →
     跑 done_criteria（§5.4）→ 通过 done；不通过且 mode=loop → 回灌 Navigator（§5.3）
  4. 节点 requires_gate → 创建/检查 Gate（沿用现逻辑，粒度从 cursor 改为 step_id）
  5. Helm 执行 → observation（沿用）
  6. Sextant 跑该节点 acceptance.checks → verdict（§6）
  7. passed → 更新状态，回到 3
     failed → 分两类（0x00 事故的教训：重试只对暂时性故障有意义）：
       执行类错误（observation.error：工具/网络/代码异常）→ attempt < max_attempts 时
         带诊断原节点重试（诊断注入 params.diagnosis；pipeline 默认 max_attempts=1
         即不隐式重试，见 §4）；
       判断类失败（校验未过）→ 不原地重试，直接分派：
                pipeline → 显式 on_failure="fail" 才 failed；否则 paused_error
                           （人工修复代码后断点重试，前面步骤成果不作废）
                template → 查确定性重规划分支表；未覆盖 → LLM 兜底一次 → 仍失败 paused_error
                loop     → 回灌 Navigator（§5.3）
```

计划载体刻意选**带状态位的扁平清单**而非依赖图（DAG）——这是 [Harness] 验证过的
形态（feature list + `passes` 状态位 + 每次只领一个最高优先级项）：清单顺序即执行
顺序，一次只执行一个节点，调度器零决策成本，计划编辑的校验也随之极简。并行执行
（多组消融同时跑）留作后续扩展，届时给节点加 `parallel_group` 批次标记比引入依赖图
更轻；且多 agent 并行 ≈ 15x token 成本（[MultiAgent]），需按任务价值单独论证。

### 5.2 断点恢复协议（[Harness] 会话协议的移植）

`resume` 时在选节点前增加**定向 + 健康检查**段：

1. 定向：从节点表 + checkpoint 恢复现场（现状已有）；
2. 健康检查：kind 可注册 `health_check` 动作（如 experiment 检查远端 workdir/venv 仍在、
   上轮 run 进程状态），坏环境先修复或将相关节点标 failed 走正常失败路径，再领新节点。

### 5.3 Navigator：从「重规划」到「计划编辑」

接口从 `plan() / replan()` 演进为：

```python
async def plan(run, context) -> PlanProposal            # 初始计划：节点列表 + done_criteria
async def on_result(run, node, verdict, history) -> PlanEdit   # 仅 mode=loop；history=各节点终态摘要
```

`PlanEdit` 是受限操作集（LLM 输出经严格 schema 校验，非法重试 2 次后 paused_error）：

```text
add_nodes:      [node_def] + 插入位置（append 或 insert_after=step_id；rank 取间隙值，seq 只增不改）
update_node:    step_id + params/acceptance 补丁     # 仅非终态节点
obsolete_nodes: [step_id] + reason                   # 仅非终态节点
request_gate:   在某节点上追加 requires_gate         # 方法级 pivot 等重大变更强制过闸门
noop / finish:  无需调整 / 建议按当前结果收束（仍须过 done_criteria）
```

硬校验不变量：action 在注册表内、每个新节点必须带 acceptance、插入位置必须在
当前执行点之后、不得修改/作废终态节点、单次编辑节点数上限（如 8）。
**能写成规则的决策优先写成规则**：档 1 的确定性分支表机制对档 2 同样开放
（如 experiment 的 metric 达标 → 直接进收尾，不问 LLM）。

`history` 不可无界增长（context rot，[CtxEng]）：只带最近 K 轮的完整终态摘要，
更早轮次压缩为结构化笔记存 checkpoint（note-taking 模式），每次计划迭代后增量更新。

档 0/1 的固定计划模板（`wiki_plan()` 等）保持函数形式不变，输出补上结构化
acceptance 与 done_criteria 即可。

### 5.4 终止条件

循环终止不再是"列表走完"：

1. `done_criteria` 全部通过 → `done`；
2. 预算耗尽 → **降级收尾（已实现，`_budget_wrapup`）**：步骤可声明 `wrapup=True`（把已完成
   工作变成产出的廉价终步，如 review.summarize / experiment.figures+report / writing 终编译）；
   预算超限时收尾步骤仍放行、其余未执行步骤作废并记 `plan_history`（source=budget），
   确保烧完预算前有产出，不再一刀切 `paused_error` 白费已完成工作；**无收尾步骤可救才
   `paused_error`**。（注：设计里的"90% 提前触发"未实现,当前是 100% 耗尽才收尾;因 usage
   仅在每步后累加、单体动作可能已超支，靠此兜底而非提前预判。）
3. Gate 驳回**按闸门 kind 分派**：入口类（compute_budget 等）驳回 = `failed`（沿用）；
   过程类（experiment_pivot 等）驳回 = 该分支节点 `obsolete` + 驳回意见作为诊断回灌
   Navigator，继续当前路线或收束——**驳回 ≠ 整个任务作废**；
4. 用户取消 → `cancelled`（沿用）。

两条**无进展硬停**规则（防死循环闭环：终检不过 → 问 Navigator → noop → 终检不过…）：

- Navigator 返回 `noop`/`finish` 而 done_criteria 未过 → 直接 `paused_error` 等人工；
- 连续 N 次（默认 3）计划迭代没有任何新节点 `passed` → `paused_error`。
  `plan_iteration` 超阈值（如 20）额外告警进活动流。

`done_criteria` 防御 [Harness] 的「过早宣告完成」，但**以过程性判据为主**（如
"stop 判定已落库 + report 存在"）；metric 阈值可选——研究性实验事先常定不出合理
阈值，定错会把循环烧到预算耗尽。终检不过就回灌继续（loop 模式）或如实 failed
（pipeline 模式）。

## 6. 结构化验收：Sextant 从裁判变成检查执行器

`acceptance.checks` 为检查列表，全部通过才 passed；检查注册表（可扩展）：

| kind | 判定 | 成本 |
| --- | --- | --- |
| `no_error` | observation 无 `error` 字段（现状默认行为，显式化） | 零 |
| `exit_code` | `observation.exit_code == value`（现 experiment.smoke 硬编码逻辑通用化） | 零 |
| `artifact_exists` | checkpoint/DB 中指定产物存在（如 `Experiment.report` 非空） | 零 |
| `schema_valid` | observation 指定字段通过给定 JSON Schema | 零 |
| `metric` | `observation.metrics[name] <op> value`（op: >=/<=/>/</==） | 零 |
| `min_count` | 集合类产出数量下限（如候选 idea ≥ N、失败率 ≤ x%） | 零 |
| `output_contract` | 技能输出约定校验（现 `skillset.check_output_contract`，纳入注册表） | 零 |
| `llm_rubric` | LLM 对照 rubric 判定，输出 `{passed, reason}` schema | LLM |

规则：

1. **确定性检查先跑**，任一失败直接 fail（不花 LLM）；`llm_rubric` 最后跑且仅在声明时；
2. `observation.error` 与动作自带 `self_check` 的现有短路逻辑保留；
3. **删除 Sextant 的动作前缀白名单**：档 0 各固定模板为每步显式声明 checks
   （多数就是 `no_error` + 一两条 `min_count`/`artifact_exists`），行为等价但诚实可审计；
4. 失败时 verdict.reason 必须 actionable（哪条 check、期望什么、实际什么）——
   好的错误信息就是免费的 rules-based feedback（[Tools]），直接作为重试/计划编辑的诊断输入。

## 7. `experiment.iterate` 拆解

现状：六步固定管线，动态循环埋在 `experiment.iterate`（内部多轮 launch → 轮询 →
reflection → improve/debug/stop）。重构为 `mode=loop`，初始计划：

```text
design      experiment.design     acceptance: schema_valid(plan JSON 含 primary_metric)
implement   experiment.implement  acceptance: no_error + artifact_exists(exp_files)   requires_gate=compute_budget
smoke       experiment.smoke      acceptance: exit_code==0
run#1       experiment.run        acceptance: no_error + metric 已解析                budget: {max_gpu_hours}
analyze#1   experiment.analyze    acceptance: schema_valid(reflection JSON)
done_criteria: 显式 stop 判定已落库 + artifact_exists(report)；metric 目标可选（§5.4）
```

`experiment.analyze` 输出结构化 reflection（沿用 `validate_reflection`），其
`decision` 优先走**确定性分支表**，未覆盖才问 Navigator：

| decision | 计划编辑 |
| --- | --- |
| `improve`（调参重跑） | add_nodes: run#k+1(新参数) → analyze#k+1 |
| `debug`（修代码） | add_nodes: implement(fix) → smoke → run → analyze |
| `pivot`（方法调整） | add_nodes: design(新方法) **requires_gate="experiment_pivot"** → implement → …（人不批就停在闸门） |
| `ablate`（补消融） | add_nodes: 若干 run(变体) + 汇总 analyze（按清单顺序串行执行） |
| `stop`（达标/无望） | add_nodes: figures → report 收尾节点 |

收益：每轮迭代是可见、可审计、可回放的节点；方法级 pivot 有人在环；GPU 预算挂
run 节点（由 experiment.run 轮询时自查时长并上报，引擎记账并按 §5.4 触发收束）；
断点恢复粒度从"整个 iterate"细化到轮内阶段；resume 健康检查覆盖远端环境失续。
幂等约定：experiment.run 以 step_id 命名远端 run 目录，重入先探测既有进程/产物
再决定续跑还是重启——防 worker 重投导致重复训练。

## 8. 迁移路径

进度：**A-F 全部实现**（2026-07-16）。
- A/B/C：alembic `c1d2e3f4a5b6_voyage_loop_v1`、`agents/voyage/checks.py`、引擎重写；
- D/E：`agents/voyage/plan_edit.py`（操作集校验 + 确定性分支表）、`Navigator.on_result`、
  `experiment.run`/`experiment.analyze` 替代 `experiment.iterate`；
- F：任务详情页按清单序（rank）渲染 + 尝试徽标 + 计划调整计数 +
  已作废步骤开关（`include_obsolete` 查询参数）。

实现相对本文的偏差（有意为之）：

1. ~~experiment 保持 pipeline 模式~~ **已撤销（2026-07-17 用户定调）**：experiment 归入
   mode=loop（migration `e3f4a5b6c7d8` 回填存量）。失败语义按节点分派——
   plan/setup/analyze/figures/report 失败走 loop 回灌（原地重试 → AI 计划调整，
   MAX_REPLANS 封顶）；**run 与 smoke 保留 on_failure="fail" + max_attempts=1 硬停**：
   运行级失败 = 预算超时/基础设施故障，盲目重跑或交 AI 重排都在烧算力；smoke 动作
   内部已有 LLM 修复循环，修完仍失败说明代码根本性不可用。正常轮次推进仍由
   plan_signal 确定性分支表驱动（规则优先，AI 只兜失败的底）。
2. **done_criteria 终检未达时 loop 模式暂不回灌 Navigator**，一律 paused_error 等人工
   （防过早宣告完成的保守实现；回灌留待有真实需求时再开）。
3. **reflection 的 decision 集仍为 improve/debug/stop**：§7 表中 pivot（方法调整闸门）与
   ablate（补消融）分支表已预留结构，待 reflection prompt 升级后开放。

| 阶段 | 内容 | 兼容性 |
| --- | --- | --- |
| A. schema | VoyageStep 加 `rank/acceptance/budget/attempt/attempts/provenance` 列 + status 枚举扩展（seq 冻结为创建序）；VoyageRun 加 `mode/plan_iteration/done_criteria`；alembic 迁移：存量回填 `mode`（按 kind 映射）、`rank = seq*100`、acceptance = `[{kind:"no_error"}]`；在途 paused_gate 的 `checkpoint["gates"]` 键由 cursor 迁到 step_id | 纯加列，老数据可读 |
| B. Sextant | 检查注册表 + 各固定模板显式声明 checks；删前缀白名单 | 档 0/1 行为等价（用现有 pytest 套件回归：test_wiki_*、test_idea_forge 等） |
| C. 引擎 | cursor 推进改为「按 rank 取第一个非终态节点」；失败路径按 mode 分派；done_criteria 终检；run.plan 改为节点表派生快照 | 成功路径行为不变；**失败路径是显式行为变化**：wiki/forge/review 失败从「LLM 自由重规划 ×2」改为「原地重试/直接 failed」（对暂时性 API 故障更安全，回归测试按新语义更新）；`run.plan`/SSE 事件结构保持，前端无感 |
| D. Navigator | `PlanEdit` 操作集 + 硬校验；仅对 loop kind 生效 | 新增路径 |
| E. experiment | §7 拆解，删 `experiment.iterate` 巨型 action | 破坏性重构，需新 e2e 测试（fake LLM 驱动多轮循环） |
| F. 前端 | 任务详情页从线性步骤列表升级为状态化任务板（步骤状态/依赖/第几轮调整），文案走大白话 | 增强 |

A–C 是无行为变化的地基（可先行合入），D–E 才引入新能力。

## 9. 风险与护栏

1. **LLM 编辑计划比生成列表难校验** → PlanEdit 严格 schema + 不变量校验 +
   重试后降级 paused_error；确定性分支表优先，LLM 兜底。
2. **失控循环** → 四道保险：run 级预算硬上限（已有）、节点 max_attempts、
   方法级变更强制 `requires_gate`、§5.4 两条无进展硬停。
3. **验收检查自身的盲区**（[Harness]：验证工具覆盖面是系统性风险）→ checks 允许
   叠加多条互补检查；llm_rubric 判定全部落库可回放，供人工抽检。
4. **档 0 被误升档** → mode 由 kind 静态决定，不暴露给 LLM 或用户选择。
5. **重复驱动与重入副作用** → run 级驱动锁（Redis SETNX `voyage:{id}:driver` + TTL
   续租）：ARQ 重投、`/resume` 端点、闸门审批三个入队入口并发时只允许一个 driver
   存活；有副作用的动作（远端 launch、消息发布、Gate 创建）以 step_id 做幂等键。

## 10. 界面文案对照（大白话）

| 代码/枚举 | 用户可见 |
| --- | --- |
| node / VoyageStep | 任务步骤 |
| plan / PlanEdit / replan | 任务计划 / 计划调整（第 N 次调整） |
| acceptance / checks | 校验标准 / 自动校验 |
| done_criteria | 完成标准 |
| verdict passed/failed | 通过 / 未通过（附原因） |
| obsolete | 已作废（计划调整时被替换） |
| requires_gate / experiment_pivot | 需要审批 / 方法调整审批 |
