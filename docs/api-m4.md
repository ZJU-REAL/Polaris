# M4 API 契约 — Experiment Lab（前后端共同遵守）

延续 api-m1/m2/m3 总则。所有路由挂 `/api`，JWT Bearer。

## 1. SSH 凭据（每用户私有）

- `GET /ssh-credentials` → `[{id, name, host, port, username, created_at, last_verified_at}]`（**绝不回传私钥**）
- `POST /ssh-credentials` `{name, host, port=22, username, private_key(PEM文本), passphrase?}` → 201（私钥 Fernet 加密入库）
- `DELETE /ssh-credentials/{id}` → 204
- `POST /ssh-credentials/{id}/test` → `{ok: bool, detail: str}`（asyncssh 连接 + `echo ok`，成功则更新 last_verified_at）

## 2. Experiments

- `POST /projects/{pid}/experiments` `{idea_id, credential_id, params?: {gpu_hint?: str, budget?: {max_hours: 4, max_runs: 10}}}`
  → `ExperimentRead`（同时创建并入队 kind=`experiment` 的 voyage；idea 须 status=promoted；每实验一个 voyage，实验与 voyage 1:1）
- `GET /projects/{pid}/experiments` → `ExperimentRead[]`
- `GET /experiments/{id}` → `ExperimentDetail`
- `POST /experiments/{id}/cancel` → 取消关联 voyage + 尝试 SSH kill 运行中的进程
- `GET /experiments/{id}/logs?run_id=&tail=500` → `{lines: [str], truncated: bool}`

`ExperimentRead`: `{id, project_id, idea_id, idea_title, status: planning|awaiting_gate|setup|running|reporting|done|failed|cancelled, voyage_id, workdir, server_host, budget, created_at, updated_at}`
`ExperimentDetail = ExperimentRead & {plan: {hypotheses:[{text, status: testing|verified|falsified}], repro_strategy, steps, budget_estimate} | null, runs: [ExperimentRunRead], report: markdown|null, metrics: {name: [{step, value}]} | null}`
`ExperimentRunRead`: `{id, seq, command, status: running|succeeded|failed, exit_code, log_path, metrics, started_at, finished_at}`

## 3. experiment voyage 管线（后端内部）

固定计划（Navigator 模板 + LLM 填充细节）：

1. **计划**（stage=experiment）：读入 idea 内容 + 相关 wiki 页 → 产出 plan JSON（假设清单、复现策略、实验步骤、预算估计）→ 写 Experiment.plan
2. **预算闸门**：创建 `compute_budget` Gate（payload 含 plan 摘要与预算）→ paused_gate
3. **建环境**：SSH 连接（asyncssh，凭据解密）→ `mkdir -p ~/polaris_runs/<exp_id>` → LLM 生成代码文件（train.py/eval.py/requirements.txt/run.sh 等，写入远端）→ `python -m venv .venv && pip install -r requirements.txt`
4. **冒烟测试**：跑 `run.sh --smoke`（小样本/1 step）→ Sextant 验证退出码与输出；失败回 LLM 修代码（≤2 次）
5. **正式运行**：`nohup bash run.sh > run.log 2>&1 & echo $!` 记 PID → 轮询（30s）：进程存活、增量拉取 run.log 追加到本地日志文件、解析 `POLARIS_METRIC {"name":..,"step":..,"value":..}` 行入 ExperimentRun.metrics → 进程退出后按 exit_code 定 succeeded/failed
6. **报告**（stage=experiment）：汇总指标与日志尾部 → markdown 报告写 Experiment.report → done

安全约束：所有远程命令记入审计日志（Activity + 专用日志文件）；命令模板固定不接受 LLM 自由拼接 shell（LLM 只产出文件内容，执行命令是白名单模板）；工作目录限定 `~/polaris_runs/`；超时/预算超限自动 kill + 置 failed。

## 4. 实时

- 复用 voyage SSE（`/voyages/{vid}/events`）推进度；运行日志新增 SSE：`GET /experiments/{id}/logs/stream`（轮询文件追加转发，15s 心跳）
- WS 复用：`{type:"experiment.status", experiment_id, status}`

## 5. 前端

- Experiment 页（替换占位）：实验列表 + 详情四 Tab：
  - **Plan**：假设清单 chips（testing/verified/falsified）、复现策略、预算卡、闸门状态（awaiting_gate 高亮 → 审批抽屉）
  - **Setup**：环境搭建步骤状态（来自 voyage steps）、生成的代码文件列表（只读预览）
  - **Run**：日志实时滚动（SSE）、指标折线图（轻量 SVG 自绘，metrics 多序列、baseline 虚线可选）、运行列表
  - **Report**：markdown 渲染报告
- 「新建实验」入口：从 promoted idea 发起（Review 页 promoted 卡片上的按钮 + Experiment 页空状态引导）；表单选 SSH 凭据 + 预算
- Settings 页新增「SSH 凭据」Tab：列表/添加（私钥 textarea）/测试连接/删除
