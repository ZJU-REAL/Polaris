# 调研笔记 — Anthropic 官方 Long-Running Agent 设计文档

> 2026-07 调研。目的：为 Voyage 任务系统演进（`docs/voyage-loop.md`）提供外部依据。
> 引文均出自对应原文；anthropic.com/engineering 部分文章已 308 重定向至 claude.com/blog，内容一致。

## 0. 来源清单

| 简称 | 文章 | 地址 |
| --- | --- | --- |
| [BEA] | Building effective agents（2024-12） | anthropic.com/research/building-effective-agents |
| [Harness] | Effective harnesses for long-running agents | anthropic.com/engineering/effective-harnesses-for-long-running-agents |
| [CtxEng] | Effective context engineering for AI agents | anthropic.com/engineering/effective-context-engineering-for-ai-agents |
| [MultiAgent] | How we built our multi-agent research system | anthropic.com/engineering/multi-agent-research-system |
| [SDK] | Building agents with the Claude Agent SDK | claude.com/blog/building-agents-with-the-claude-agent-sdk |
| [Tools] | Writing effective tools for agents | anthropic.com/engineering/writing-tools-for-agents |

## 1. Workflows vs Agents：什么时候才需要认知循环 [BEA]

- **二分标准 = 控制流由谁决定**：
  - *Workflows*："systems where LLMs and tools are orchestrated through **predefined code paths**"；
  - *Agents*："systems where LLMs **dynamically direct their own processes and tool usage**"。
- **能预测步骤就用 workflow，不能预测才用 agent**：agents 只适用于 "open-ended problems where it's **difficult or impossible to predict the required number of steps**, and where you **can't hardcode a fixed path**"。
- agent 的代价："higher costs, and the potential for **compounding errors**"（错误复利放大）；对策是沙箱测试 + guardrails。
- 总原则："**find the simplest solution possible**, and only increase complexity when needed"；"start with simple prompts … add multi-step agentic systems **only when simpler solutions fall short**"。
- 三大成功原则：Simplicity / Transparency（显式展示规划步骤）/ 精心打磨 ACI（Agent-Computer Interface，工具文档与测试）。

### 五种 workflow 模式及适用条件

| 模式 | 机制 | 适用条件 |
| --- | --- | --- |
| Prompt chaining | 固定顺序步骤，步骤间可加程序化校验点（gate） | 任务能 "easily and cleanly decomposed into fixed subtasks" |
| Routing | 先分类，再分发到专门化下游 | 存在 "distinct categories that are better handled separately" 且分类可做准 |
| Parallelization | Sectioning（独立子任务并行）/ Voting（同任务多次取多样输出） | 子任务可并行提速，或需多视角/多次尝试提置信度 |
| Orchestrator-workers | 中心 LLM **动态**分解、分派 worker、综合 | "can't predict the subtasks needed"——子任务由输入现场决定 |
| Evaluator-optimizer | 一个 LLM 生成、另一个评估给反馈，循环迭代 | 有 "clear evaluation criteria" 且 "iterative refinement provides measurable value" |

注：orchestrator-workers 是 workflow → agent 的过渡形态；evaluator-optimizer 是把验证内建进流程的模式。

## 2. 长时程 harness：状态外置 + 确定性脚手架 [Harness]

背景实验：让模型仅凭高层 prompt + compaction 从零构建生产级应用，失败。结论：**瓶颈不是模型能力，而是跨 context window 的连续性**；仅靠压缩上下文不够。

### 四种失败模式

1. **one-shot 贪多**：试图一口气做完，留下大量无记录的半成品；
2. **过早宣告完成**：新实例看到已有进展就宣告任务完成；
3. **未验证就标完成**：单测通过但端到端不工作（"tendency to mark a feature as complete without proper testing"）；
4. **环境失续**：接手时环境是坏的、无法快速判断状态。

### 对策：Initializer + Coding agent 两段式，状态外置三件套

| 状态载体 | 要点 |
| --- | --- |
| **Feature list（JSON）** | 细粒度任务清单，每条含 description/steps/`passes: bool`。**选 JSON 而非 Markdown**："the model is less likely to inappropriately change or overwrite JSON files" |
| **进度文件** | 显式会话日志，让新实例快速恢复现场 |
| **Git 历史** | 每会话一个带描述的 commit；可回滚到工作状态 |

另加 `init.sh` 一键拉起环境，省去每个会话重新摸索的 token。

### 每个会话的固定协议

1. **定向**：读 git log / 进度文件 / feature list；
2. **健康检查**：先确认环境没坏，坏了先修（或 git revert）再干新活；
3. **增量工作**：**单会话只做一个最高优先级 feature**（"This incremental approach turned out to be critical"）；
4. **收尾到 clean state**：commit + 更新进度文件 + 只有 "after careful testing" 才把 feature 标 passing。

其他要点：用强硬指令保护测试（禁止删改测试来"让测试通过"）；端到端验证必须显式要求（"mostly did well … **once explicitly prompted**"）；验证工具自身有盲区（如 Puppeteer 看不到浏览器原生 alert）。文末点名希望把方法推广到 **scientific research** 等 long-horizon 领域。

## 3. 上下文工程：四种长任务上下文管理策略 [CtxEng]

- Context 是稀缺资源：存在 **context rot**（token 越多回忆越差）与有限 **attention budget**；总原则是"找到最小的高信号 token 集合"。
- **Compaction**：接近上限时总结历史开新窗口。保留架构决策/未解决 bug/实现细节，丢冗余工具输出；先保 recall 再提 precision；最轻量形式是 tool result clearing。
- **Structured note-taking / memory**：定期把状态写到 context 之外的持久化笔记（例：Claude plays Pokémon 跨数千步维持精确状态）。适合**有清晰里程碑的迭代式工作**。
- **Sub-agent 隔离**：子 agent 在干净上下文里烧数万 token，只回传 **1,000–2,000 token 的蒸馏摘要**；探索细节不污染主线。
- **Just-in-time retrieval**：维护轻量标识符（路径/查询/链接），运行时按需加载，支持 progressive disclosure；混合策略最实用（预加载少量关键 + 运行时检索）。

## 4. 多 agent research 系统：经济账与委派契约 [MultiAgent]

- 架构：LeadResearcher（规划 + 把计划持久化到 Memory 防截断）→ 并行 spawn Subagents（各自搜索、评估、回传 findings）→ 综合 → **CitationAgent 独立核对引用**。
- 性能：内部评估比单 agent **高 90.2%**；token 用量解释 80% 的性能差异。
- **经济账：chat : 单 agent : 多 agent ≈ 1 : 4 : 15**。"多 agent 系统需要任务价值足够高来支付增加的性能成本。"
- **委派契约必须结构化**：每个子任务含 objective、output format、工具与信息源约束、**明确边界**——否则子 agent 重复劳动或误解分工。
- **Effort scaling 写进规划提示**：简单事实核查 = 1 agent / 3–10 次调用；直接比较 = 2–4 agent；复杂研究才 10+ agent——防止简单任务被过度编排。
- 大产物直写文件系统、只回传引用，避免多级转述（"传话游戏"）。
- **Durable execution**："不能简单重启，重启昂贵"——regular checkpoint + retry + 把工具失败告知模型让其自适应绕行 + 全量 tracing + rainbow deployment（新旧版并存渐进切换，不打断在跑 agent）。
- 适合多 agent：重度可并行、信息量超单窗口、高价值任务。不适合：可并行子任务少（点名 coding）、需共享同一 context、低价值常规任务。
- **评估**：立刻用 ~20 个真实查询的小样本开始；自由文本输出用 LLM-as-judge rubric（事实准确性/引用准确性/完整性/来源质量）+ 人工兜底盲区；**end-state 评估而非过程评估**——断言正确的最终状态发生了，允许等效替代路径。

## 5. Agent SDK：核心循环与 verify 分层 [SDK]

- 核心循环：**gather context → take action → verify work → repeat**；哲学是"给 agent 一台计算机"——"能检查和改进自身输出的 agent 从根本上更可靠"。
- Gather：agentic search 优先（grep 等），语义搜索谨慎引入；subagent 隔离上下文；compaction 兜底。
- Act：主要动作做成一等工具；bash/脚本兜底；复杂产出走代码生成；外部集成走 MCP。
- **Verify 三层（按官方推荐排序）**：

| 机制 | 适用 | 官方评价 |
| --- | --- | --- |
| **Rules-based feedback** | lint、测试、schema 校验等可判定标准 | **最有效**："明确定义的规则、哪条失败、为什么失败"；确定性、零模型成本 |
| **Visual feedback** | UI/图表/排版 | 截图自查，成本中等 |
| **LLM-as-judge** | 无法规则化的判断性质量 | "通常不够稳健（**not robust**）"，仅在收益值得时兜底 |

- 工程能力：session 持久化/resume/fork；自动 compaction（持久规则放 CLAUDE.md，因早期指令可能被压掉）；file checkpointing（只追踪 Write/Edit，**bash 写入不追踪**——远程 SSH 场景需自建快照）；hooks 在宿主进程做确定性拦截/校验；**structured outputs**（JSON Schema 校验 + 自动重试）。
- 官方建议：跨机恢复不要依赖 session 文件，**把关键结论落成应用状态喂给新 session**——平台自己的 DB 才是真源。

## 6. 工具设计原则 [Tools]

- **少而精、面向工作流而非 API**："更多工具并不总是带来更好结果"；合并离散操作（`schedule_event` 替代三连调用；对科研平台即 `get_paper_context` 优于 metadata/citations/pdf 三个工具）。
- 按服务/资源 namespacing 命名，减少混淆。
- 返回高信号内容：避免 UUID 等低层标识；提供 `response_format: detailed|concise`（实测省约 2/3 token）。
- Token 效率：分页/过滤/截断 + 合理默认；单响应限额（Claude Code 25k token）；截断策略应引导多次小而准的搜索。
- **错误信息必须 actionable**：说明期望格式并给示例——好的错误信息就是免费的 rules-based feedback。
- 工具描述即 prompt engineering："即使小改进也能带来显著性能提升"；用真实多步任务做 eval，读原始转录（"agent 省略了什么往往比它说了什么更重要"）。

## 7. 对 Polaris 的映射结论

1. **分层是官方共识**：步骤可预测的任务用 workflow（预定义代码路径），不可预测才上 agent 循环；简单任务用简单方案。对应 Voyage 的三档分类（见 `docs/voyage-loop.md` §3）。
2. **Navigator ≈ Initializer + orchestrator**：产出机器可校验的任务清单（JSON、带完成/验证状态位），而非自由文本计划；委派契约结构化（objective/output format/边界/验收）。
3. **Sextant 分层验证**：规则 > 可视 > LLM 裁判；end-state 断言而非过程重放；这与平台"确定性 vs 判断性分离"铁律同源。
4. **运行时外壳（checkpoint/Gate/预算/审计）铺给所有任务**是所有文档共同背书的方向：durable execution、clean state 收尾、在 checkpoint 或遇阻时暂停等人类反馈。
5. **防四种失败模式**要机制化：任务状态位防"过早宣告完成"；voyage 级完成标准防"提前走完清单"；单节点小步增量防 one-shot；节点开头健康检查防环境失续。
6. **经济性**：多 agent ≈ 15x token，只配高价值可并行任务（实验、深度综述）；文献同步这类确定性任务连 P-E-V 都不需要。
