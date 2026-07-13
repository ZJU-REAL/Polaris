# M1 API 契约（前后端共同遵守）

所有路由挂在 `/api` 下，JWT Bearer 认证（除注册/登录）。错误统一 `{"detail": ...}`。

## 1. Projects（研究方向）

- `GET /projects` → `ProjectRead[]`（仅本人是成员的项目）
- `POST /projects` `{name, definition}` → `ProjectRead`
- `GET /projects/{id}` → `ProjectRead`（含 members）
- `PATCH /projects/{id}` `{name?, definition?, status?}` → `ProjectRead`（owner/admin）
- `POST /projects/{id}/members` `{email, role: "member"|"owner"}` → 204（owner/admin）

`definition`（结构化访谈结果，JSON，前端向导逐步填写，允许部分保存草稿）：

```json
{
  "statement": "一句话方向定义",
  "goals": ["目标..."],
  "in_scope": ["..."], "out_of_scope": ["..."],
  "questions": ["3-5 个具体研究问题"],
  "rubric": [{"name": "维度名", "description": "打分标准", "weight": 1.0}],
  "anchor_papers": [{"title": "", "arxiv_id": "", "url": "", "reason": ""}],
  "keywords": {"arxiv_categories": ["cs.CL"], "include": ["..."], "synonyms": {"术语": ["同义词"]}},
  "cadence": "daily"
}
```

## 2. Admin · LLM（仅 role=admin）

- `GET /admin/llm/providers` → `[{id, name, kind, base_url, api_key_masked, enabled}]`
- `POST /admin/llm/providers` `{name, kind: "openai_compat"|"anthropic"|"fake", base_url?, api_key?, enabled}` 
- `PATCH /admin/llm/providers/{id}`（api_key 传空字符串=不变）
- `DELETE /admin/llm/providers/{id}`
- `GET /admin/llm/routes` → `[{stage, provider_id, model, temperature}]`
- `PUT /admin/llm/routes` 整表覆盖 `[{stage, provider_id, model, temperature?}]`
- `GET /admin/llm/usage?project_id&user_id&days=30` → `[{date, stage, model, prompt_tokens, completion_tokens, calls}]`

`stage` 枚举：`default | navigator | sextant | interview | relevance | librarian | forge | debate | experiment | writing | review`

`kind="fake"`：确定性假 provider（无需 key，回显式响应），用于测试与无 key 演示。

## 3. Voyages（长时程 agent 任务）

- `POST /voyages` `{kind, project_id, goal, params?}` → `VoyageRead`（立即入队 ARQ）
  - M1 kinds：`demo`（三步演示航程：分析目标→生成产物→自检；其中第 2 步声明需要 `compute_budget` 闸门）
- `GET /voyages?project_id=` → `VoyageRead[]`
- `GET /voyages/{id}` → `VoyageRead & {steps: VoyageStepRead[]}`
- `POST /voyages/{id}/cancel` → `VoyageRead`
- `GET /voyages/{id}/events` → **SSE**（`text/event-stream`）

`VoyageRead`: `{id, kind, goal, status, plan, cursor, budget, usage, project_id, created_by, created_at, updated_at}`
`status`: `planning|executing|verifying|replanning|paused_gate|paused_error|done|failed|cancelled`
`VoyageStepRead`: `{id, seq, title, action, params, observation, verdict: null|{passed, reason}, status, tokens, started_at, finished_at}`

SSE 事件（`event: <type>` + `data: <json>`）：
- `status` `{status, cursor}`
- `step` `{step: VoyageStepRead}`（步骤开始/结束/判定时各推一次）
- `log` `{message}`
- 心跳：每 15s 注释行 `: ping`

## 4. Gates（闸门）

- `GET /gates?status=pending|decided&project_id=` → `GateRead[]`（成员可见本项目的）
- `POST /gates/{id}/approve` `{comment?}` → `GateRead`；若 `payload.voyage_id` 存在则自动入队恢复该 voyage
- `POST /gates/{id}/reject` `{comment?}` → `GateRead`；关联 voyage 置为 `failed`

`GateRead`: `{id, kind, status, payload, project_id, requested_by, decided_by, comment, created_at, decided_at}`

## 5. WebSocket 通知

`WS /ws/notifications?token=<jwt>`（query 传 token）。服务端推送：

```json
{"type": "gate.created", "gate": GateRead}
{"type": "gate.decided", "gate": GateRead}
{"type": "voyage.status", "voyage_id": "...", "status": "..."}
```

广播范围：与用户共同项目的事件。客户端无需发消息（心跳 ping/pong 由协议层处理）。

## 6. Redis 频道约定（后端内部）

- `voyage:{id}:events`——voyage 引擎发布，SSE 端点订阅转发
- `notify:project:{project_id}`——gate/voyage 状态变化，WS 端点订阅转发
