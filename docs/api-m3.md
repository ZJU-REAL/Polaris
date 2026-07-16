# M3 API 契约 — Idea Forge 与多agent评审（前后端共同遵守）

延续 api-m1/m2 总则。所有路由挂 `/api`，JWT Bearer，权限=项目成员。

> Idea 2.0 升级（docs/api-idea2.md）：§1 forge 管线已升级为多信号源七步版（端点不变，
> knobs 新增 signals，产物 depth=sketch）；深度生成（Research Proposal）、Idea 新字段与
> 锦标赛同 depth 配对规则见 api-idea2.md。

## 1. Idea Forge（生成）

- `POST /projects/{pid}/forge` body：

```json
{"knobs": {"num_ideas": 8, "dedup_threshold": 0.85, "max_context_papers": 20}}
```

→ `VoyageRead`（kind=`idea_forge`；同项目同时只允许一个 forge/review voyage，409）。
管线（固定计划）：`读取知识库上下文（compiled wiki 页 + 概念）→ gap 分析（LLM stage=forge）→ 生成 N 个候选 idea → 四维独立打分（新颖性/可行性/可操作性/影响力，0-10，LLM 逐条）→ 语义去重（embedding 相似度 > threshold 视为重复，rerank 复核）→ 入库候选池（status=candidate）`

- `GET /projects/{pid}/forge/state` → `{running_voyage_id|null, last_run, idea_counts: {candidate, under_review, promoted, rejected, total}}`

## 2. Ideas

- `GET /projects/{pid}/ideas?status=&sort=elo|-created_at|score` → `IdeaRead[]`
- `GET /ideas/{id}` → `IdeaDetail`
- `PATCH /ideas/{id}` `{status: "rejected"}`（人工淘汰；其他状态转换走专用接口）
- `POST /ideas/{id}/promote` → 创建 `idea_promotion` Gate（pending），响应 `GateRead`；审批通过后 idea.status=promoted（gates approve 联动，复用 M1 机制）

`IdeaRead`: `{id, project_id, title, summary, scores: {novelty, feasibility, operability, impact} | null,
elo_rating, status: candidate|under_review|promoted|rejected, created_at}`
`IdeaDetail = IdeaRead & {content(markdown：动机/方法概述/预期实验/风险), parent_paper_ids: [uuid],
parent_papers: [{id, title}], score_rationale: {novelty: "...", ...} | null}`

## 3. 评审锦标赛（多agent辩论 + Elo）

- `POST /projects/{pid}/review/tournament` body：

```json
{
  "idea_ids": null,          // null = 全部 candidate/under_review
  "rounds": 2,               // 每对 idea 的辩论轮数
  "personas": null           // null = 默认三人设；或 [{"name":"严谨方法论者","stance":"专挑方法漏洞"},...]
}
```

→ `VoyageRead`（kind=`idea_review`）。
管线：Swiss/循环配对 → 每对开一场**科学辩论**（正方 agent 为 A 辩护、反方为 B 辩护、裁判 agent 判胜负与理由，stage=debate）→ Elo 更新（K=32）→ 参与 idea 置 status=under_review → 全部结束后汇总。
每场辩论落库为一个 `ReviewSession`（target_type="idea_match", payload 含 idea_a/idea_b/winner），逐轮发言为 `ReviewMessage`。

- `GET /projects/{pid}/review/leaderboard` → `[{...IdeaRead, matches, wins}]`（按 elo 降序）

## 4. 讨论（人机同场）

- `GET /ideas/{id}/sessions` → `ReviewSessionRead[]`（该 idea 参与的辩论场次 + 讨论区）
- `GET /sessions/{sid}/messages` → `ReviewMessageRead[]`
- `POST /sessions/{sid}/messages` `{content}` → `ReviewMessageRead`（author_type=human）；广播 WS `{type:"review.message", session_id, message}`
- 每个 idea 自动有一个 `target_type="idea_discussion"` 的常驻讨论 session（首次 GET sessions 时惰性创建）；**人类评论在下次锦标赛/打分时注入相关 agent 的上下文**（后端收集该 idea 全部 human 消息拼入 prompt）

`ReviewSessionRead`: `{id, target_type, target_id, status, payload, created_at}`
`ReviewMessageRead`: `{id, session_id, author_type: agent|human, author_name(人设名或用户displayname),
content, round, created_at}`

## 5. WS 事件（复用 /ws/notifications）

新增：`{type:"review.message", session_id, project_id, message: ReviewMessageRead}`、
`{type:"idea.status", idea_id, status}`
