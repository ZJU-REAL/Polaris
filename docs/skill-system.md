# 技能系统设计 — Skill System（用户自定义 + 内置 + 市场）

> 目标：把散落在 `agents/voyage/actions_*.py` 里硬编码的判断性指令（prompt、评分标准、
> 评审人设、写作规范、流程模板）抽成**可版本化、可装配、可分享的一等数据实体「技能」**，
> 让用户能自定义（如写作技能、论文评审技能）、平台内置一批 CS/AI 研究技能、
> 并通过技能市场在多用户间分享——最终在科研全流程（文献→Idea→评审→实验→写作→论文评审）
> 中被 Voyage 引擎调用。

## 0. 设计原则

1. **技能 = 数据，不是代码**。v1 技能只包含声明式内容（指令 markdown、rubric、
   人设、由白名单动作组成的步骤模板），不允许任意代码执行——与平台
   「确定性逻辑写代码、判断性任务交 LLM」的铁律一致：技能只改变*判断性任务*的行为。
2. **不新增旁路**。技能通过现有机制生效：LLM 调用仍走 `core/llm` 路由（stage 不变，
   管理端模型路由表依旧是唯一的模型选择入口）；流程技能产出的步骤仍是
   `actions.py` 注册表里的已知动作，经 Navigator 校验、Helm 执行、Sextant 验证。
3. **可复现可审计**。Voyage 启动时对生效技能做**内容快照**写入 run，
   事后回放能看到"这次任务用了哪些技能的哪个版本"。
4. **界面文案大白话**：用户可见叫「技能 / 我的技能 / 内置技能 / 技能市场 /
   启用到项目」，不出现 binding/manifest 等术语（代码标识符不受限）。

## 1. 技能模型

### 1.1 技能类型（kind）

| kind | 含义 | 生效方式 | 例子 |
| --- | --- | --- | --- |
| `guidance` | 环节指令增强：追加到某环节 LLM 调用的 system prompt | 挂到一个或多个注入点（§3.1） | 学术写作规范、实验代码风格、领域背景知识 |
| `rubric` | 评分/评估标准：替换或增强某环节的打分 rubric | 挂到打分类注入点，成为 rubric 段落 | 文献相关性标准、Idea 四维打分细则、顶会评分标准 |
| `persona` | 评审/辩论人设包：1..N 个带立场的 agent 人设 | 被 idea 辩论、论文评审的评审员环节消费 | 「严谨方法论者 + 复现怀疑派 + 应用价值派」 |
| `workflow` | 流程模板：一串由白名单动作组成的步骤（同 Navigator 固定计划的 schema） | 作为自由规划 voyage 的计划模板，或注册为可选任务类型 | 「文献综述速写」「rebuttal 起草」「消融实验清单」 |

`tool` 类技能（外部 API/代码工具）**明确排除在 v1 之外**，未来若引入必须过管理员审核 + 闸门。

### 1.2 技能包格式（存储为 JSON manifest + markdown body）

```yaml
# manifest（结构化字段，Pydantic 严格校验）
slug: paper-review-neurips        # 全局唯一（scope 内）
name: 顶会论文评审（NeurIPS 风格）  # 中文为主
name_en: NeurIPS-style Paper Review
kind: rubric                      # guidance | rubric | persona | workflow
description: 按 NeurIPS 评审维度逐项打分并给出改进建议
targets:                          # 注入点（§3.1 白名单枚举，可多个）
  - review.referees
config_schema:                    # 用户可调旋钮（JSON Schema 子集：enum/number/bool/string）
  strictness: {type: string, enum: [宽松, 标准, 严格], default: 标准}
variables: [goal, paper_title]    # body 中允许的模板变量（白名单）
personas: []                      # kind=persona 时使用：[{name, stance, style}]
steps: []                         # kind=workflow 时使用：Navigator 步骤 schema（§3.3）
output_contract:                  # 可选：产出约束，Sextant 先做确定性校验
  format: json                    # json | markdown | text
  json_schema: {...}              # format=json 时用于机器校验
```

body 为 markdown 正文（指令/评分细则/少样本示例），上限 8KB（防 token 失控）；
模板变量沿用 `actions.render_template` 的 `{var}` 语法与 SafeDict 容错。

### 1.3 数据模型

```text
Skill            id, slug, kind, name, name_en, description,
                 scope: builtin|user|project, owner_id(user), project_id(nullable),
                 current_version_id, is_archived, created_at
SkillVersion     id, skill_id, version(int 自增), manifest JSON, body TEXT,
                 changelog, created_by, created_at        —— 不可变，只增不改
ProjectSkill     id, project_id, skill_id, version_id(pin) | null(跟随最新),
                 target(注入点), config JSON, sort_order, enabled, created_by
                 —— 「启用到项目」记录（表 project_skills）；同一注入点可启用多个，
                 按 sort_order 拼接；uq(project, skill, target)
SkillListing     id, skill_version_id, title, summary, tags[], published_by,
                 status: pending|approved|rejected|delisted, install_count, created_at
SkillReview      id, listing_id, user_id, rating(1-5), comment, created_at
```

要点：

- **builtin 技能**：种子数据（alembic data migration / 启动 seed），`scope=builtin`
  只读；用户可「复制为我的技能」（fork：拷贝 manifest+body 成 user scope v1）。
- **版本策略**：编辑技能 = 追加 SkillVersion；SkillEnable 默认 pin 到启用时的版本，
  可切换「跟随最新」。listing 永远指向具体 version。
- 删除技能 = 归档（is_archived），已有 voyage 快照与 listing 不受影响。

## 2. 内置技能（种子清单，v1 全部 guidance/rubric/persona/workflow 四类）

| 技能 | kind | 注入点 |
| --- | --- | --- |
| 文献相关性评估标准 | rubric | wiki.score_relevance |
| 论文精读笔记规范（Librarian 风格） | guidance | wiki.compile |
| Research Gap 分析视角 | guidance | forge.gap_analysis |
| Idea 四维打分细则 | rubric | forge.score |
| 经典评审三人设（方法论者/复现怀疑派/应用价值派） | persona | review.debate |
| 顶会论文评审（NeurIPS/ICLR 风格 rubric + 评审员人设） | rubric + persona | review.referees |
| 学术写作规范（结构/时态/claim-evidence 对齐） | guidance | writing.section |
| Abstract 写作技能 | guidance | writing.section(abstract) |
| Related Work 综述技能 | guidance | writing.related_work |
| 实验设计规范（baseline/消融/显著性） | guidance | experiment.plan |
| 实验代码风格（可复现性 checklist） | guidance | experiment.setup |
| 文献综述速写 | workflow | 自由规划模板 |
| Rebuttal 起草 | workflow | 自由规划模板 |

内置技能同时充当**用户自定义的范本**：编辑器里「从内置技能复制」是主要创建路径。

## 3. 全流程调用机制（核心）

### 3.1 注入点（injection point）白名单

注入点 = 「voyage 动作 × 其内部 LLM 调用」的稳定命名，与动作名一致（个别动作
细分子点，如 `writing.section(abstract)`）。v1 白名单：

```text
wiki.score_relevance   wiki.compile
forge.gap_analysis     forge.generate      forge.score
review.debate          review.referees     review.meta_review
experiment.plan        experiment.setup    experiment.iterate   experiment.report
writing.section        writing.section(<section>)   writing.related_work
navigator.free_plan    （自由规划：workflow 技能作为计划模板）
```

新动作接入技能 = 在其实现里声明注入点常量并调用 SkillSet（见 3.2），一行接入。

### 3.2 运行时装配：SkillSet

新增 `app/services/skills.py`（SkillResolver）与 `agents/voyage/skillset.py`：

1. **voyage 启动时快照**：engine 创建 run 后调用
   `SkillResolver.snapshot(project_id, kind)` → 取该项目所有 enabled 的 SkillEnable，
   解析 pin 版本，把 `{target: [{skill_id, slug, version, kind, manifest, body, config}]}`
   写入 `VoyageRun.checkpoint["skills"]`。此后本次 run 只读快照——中途改技能不影响
   进行中任务，且完整可审计。
2. **ActionContext 增加 `skills: SkillSet`**（从 checkpoint 快照构造，无 DB 依赖，
   断点恢复天然可用）。API：

```python
class SkillSet:
    def guidance(self, target: str) -> str:
        """target 上所有 guidance/rubric 技能 body 按 sort_order 拼接，
        每段包 <skill name=... version=...> 标签便于审计与 prompt 结构化；
        渲染 config 与模板变量；总长超预算（默认 6K tokens）时截断并告警。"""
    def personas(self, target: str) -> list[Persona] | None:
        """persona 技能 → 人设列表；None 表示用代码内默认人设。"""
    def output_contract(self, target: str) -> OutputContract | None: ...
```

3. **动作侧接入**：各 `actions_*.py` 在拼 system prompt 处追加
   `ctx.skills.guidance("forge.score")`；辩论/评审员动作先问
   `ctx.skills.personas(...)` 再回退默认；`review/tournament` API 的 `personas`
   参数保留（显式传入 > persona 技能 > 内置默认）。
4. **Sextant 联动**：动作产出若声明了 `output_contract(format=json)`，Sextant 在
   LLM 判定前先跑确定性 JSON Schema 校验，失败直接 fail 并把 schema 错误作为诊断
   回传 Navigator——省 token 且更准。

### 3.3 workflow 技能与 Navigator

- workflow 技能的 `steps` 使用与 `navigator.validate_steps` 完全相同的 schema
  （title/action/params/acceptance/requires_gate），保存时即校验：
  **action 必须在 `known_actions()` 白名单内**，闸门声明原样保留。
- 自由规划 voyage（无固定计划的 kind）：Navigator 的 system prompt 附上项目已启用的
  workflow 技能摘要（slug + 描述 + 步骤概要），LLM 可整体采用某模板
  （输出 `{"use_skill": "<slug>"}` 时直接展开其 steps），也可自行规划。
- 用户也可从技能详情页直接「运行此流程」→ 创建 kind=`custom` 的 voyage，
  计划即技能 steps 展开（模板变量用运行时填写的表单值渲染，表单来自 config_schema）。

### 3.4 成本与安全

- 技能不选模型：stage 仍由动作决定、模型仍由管理端路由表决定；manifest 可写
  `model_hint`（如"建议强模型"）仅作 UI 提示。
- prompt 注入面控制：body 长度上限、变量白名单、快照审计；市场技能安装前
  强制全文预览。技能内容进入 prompt 时包在带来源标注的标签里，便于排查。

## 4. API 契约（延续 api-m1 总则：`/api` 前缀，JWT，项目接口校验成员）

### 4.1 技能 CRUD 与版本

- `GET /skills?scope=builtin|mine|project&kind=&q=` → `SkillRead[]`
- `POST /skills` `{slug, kind, name, name_en?, description, manifest, body}` → `SkillRead`（自动建 v1）
- `GET /skills/{id}` → `SkillDetail`（含 current 版本 manifest+body）
- `POST /skills/{id}/versions` `{manifest, body, changelog}` → 新 `SkillVersionRead`
- `GET /skills/{id}/versions` → `SkillVersionRead[]`
- `POST /skills/{id}/fork` → 复制为我的技能（builtin/市场技能的编辑路径）
- `DELETE /skills/{id}` → 归档
- `POST /skills/{id}/test` `{target, sample: {goal, vars}}` → SSE 试运行：
  用 FakeProvider 或真实 stage（带单次预算上限）渲染最终 prompt 并预览输出，
  编辑器「试运行」按钮用。

### 4.2 项目启用（界面文案：「启用到项目」）

- `GET /projects/{pid}/skills` → `SkillEnableRead[]`（按注入点分组）
- `POST /projects/{pid}/skills` `{skill_id, target, version_id?, config?, sort_order?}` → `SkillEnableRead`
- `PATCH /project-skills/{id}` `{enabled?, config?, sort_order?, version_id?}`
- `DELETE /project-skills/{id}`

### 4.3 技能市场（部署内共享，实验室多用户）

- `POST /skills/{id}/publish` `{summary, tags}` → listing（status=pending）。
  审核走管理员专用接口 `POST /market/skills/{listing_id}/approve|reject`
  （不复用 Gate：Gate 绑定 project_id，而市场是全平台级；审核队列
  `GET /market/skills?status=pending` 仅管理员可见）
- `GET /market/skills?q=&tags=&sort=installs|rating|-created_at` → `SkillListingRead[]`
- `GET /market/skills/{listing_id}` → 详情（含全文预览、版本、评分、安装数）
- `POST /market/skills/{listing_id}/install` `{project_id?}` → 复制为 user/project scope
  技能（记 install_count；后续原作者发新版时 UI 提示可更新）
- `POST /market/skills/{listing_id}/reviews` `{rating, comment}` / `GET .../reviews`
- 导出/导入（跨部署分享）：`GET /skills/{id}/export` → 单文件 JSON 技能包
  （`format: polaris-skill@1`，含 manifest + body）；`POST /skills/import`
  （JSON body，导入即全量校验，slug 冲突自动加后缀）

### 4.4 Voyage 侧

- `VoyageRead` 增加 `skills_snapshot_summary: [{slug, name, version, target}]`，
  详情页展示「本次任务使用的技能」。
- 各流程启动接口（forge/tournament/experiment/writing/review）新增可选
  `skill_overrides: [{skill_id, target, config}]`，仅本次生效（不落 SkillEnable）。

## 5. 前端

- **技能库页**（新一级导航「技能 / Skills」）：内置 / 我的 / 本项目三个 tab，
  卡片展示 kind 徽标、注入点、版本、启用状态。
- **技能编辑器**：左侧表单（名称、类型、注入点多选、旋钮定义、人设/步骤编辑器），
  右侧 CodeMirror markdown body；底部「试运行」面板（SSE 预览最终 prompt 与输出）。
  workflow 类型用步骤表格编辑（动作下拉 = 白名单）。
- **项目设置 → 技能 tab**：按流程环节分组的启用列表，拖拽排序，旋钮即时编辑。
- **技能市场页**：搜索/标签/排序、详情页全文预览 + 评分区 + 「安装」。
- **任务详情**：技能快照展示；每步 observation 里可展开「注入的技能内容」。
- 文案示例：「把这个技能启用到项目后，AI 在论文评审环节会按你的标准打分」。

## 6. 里程碑拆分

| 阶段 | 内容 |
| --- | --- |
| S1 | Skill/SkillVersion/ProjectSkill 模型 + 内置技能种子 + SkillSet 快照与 guidance/rubric 注入（wiki/forge/writing/review 接入）+ 项目启用 API/UI ——**已实现**（migration e2f3a4b5c6d7；前端 /skills 技能库页） |
| S2 | 技能编辑器 + 试运行 + persona 技能接入辩论与论文评审 + output_contract→Sextant 确定性校验 ——**试运行与 persona 接入已实现**（编辑器 UI 与 output_contract 待做） |
| S3 | workflow 技能（保存校验 + 自由规划模板 + 「运行此流程」）+ voyage 快照展示 ——**后端已实现**（`POST /skills/{id}/run` → kind=custom voyage；快照展示 UI 待做） |
| S4 | 技能市场（发布→管理员审核、安装、评分、导出导入）——**已实现**（migration f5a6b7c8d9e0 + 前端市场视图 + JSON 技能包导出导入） |

## 7. 与现有代码的迁移关系

- `actions_review.py` 默认人设、`actions_wiki.py` 相关性 rubric、`actions_writing.py`
  分节指令中的**风格段落**逐步搬入内置技能（代码保留最小骨架 prompt：任务定义 +
  输出格式；"怎么判断/什么风格"交给技能）。硬约束（引用只能来自知识库、数字只能来自
  ExperimentRun.metrics 等 guardrail）**永远留在代码里**，技能不可覆盖。
- `STAGES` 枚举不变；技能与模型路由正交。
