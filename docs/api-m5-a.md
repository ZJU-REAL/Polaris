# M5-A API 契约 — 实验自动迭代（前后端共同遵守）

延续既有总则。基于已批准的 M5 计划 Wave 1。

## 1. 实验管线变化（后端内部）

固定计划改为：`plan → (compute_budget 审批) → setup → smoke → iterate → figures → report`

**iterate 步骤**（原 run 步骤升级，内部多轮循环）：
每轮 = launch run（ExperimentRun 行，seq 递增）→ 平台解析 metrics（POLARIS_METRIC 行 + 可选 workdir 的 metrics.json）→ LLM structured reflection（stage=experiment）：

```json
{"observation": "...", "diagnosis": "...", "hypothesis_updates": [{"index": 0, "status": "verified|falsified|testing", "evidence": "..."}],
 "decision": "improve|debug|stop", "planned_change": "...", "stop_reason": null}
```

→ 回写 `Experiment.plan.hypotheses[].status` → 按 decision：
- `improve`：LLM 修改代码/超参（现有 FIX 模式扩展，diff 说明进 prompt）→ 下一轮
- `debug`：修错误（**独立限额 3 次**，超限该分支终止）
- `stop`：结束迭代

终止条件（任一）：decision=stop、达 `budget.max_runs`、超 `budget.max_hours`、**连续 2 轮主指标无提升**（主指标=plan 新增字段 `primary_metric: {name, direction: maximize|minimize}`，plan 生成时 LLM 必填）、假设全部非 testing。

**figures 步骤**：LLM 写 matplotlib 脚本（硬约束写进 prompt：只准读 `metrics_all.json`——平台把所有 run 的解析结果写入 workdir 该文件；禁止硬编码数据）→ SSH 跑 `python plot_figures.py` → 拉回 `figures/*.png`（同名 `.pdf` 一并拉回供论文用）→ VLM 质检（轴/图例/可读性）不合格修脚本 ≤2 次 → 写 `Experiment.figures`。

## 2. 模型与迁移

- ExperimentRun 加列：`reflection` JSON（该轮的 reflection 对象）、`primary_value` float nullable（主指标值，平台解析）
- Experiment 加列：`figures` JSON（`[{index, name, caption, path 内部}]`）、`iteration_state` JSON（`{no_improve_streak, debug_count, stopped_reason}`）
- plan JSON 内新增 `primary_metric`（不需迁移）

## 3. API 扩展

- `GET /experiments/{id}`（ExperimentDetail 扩展）：
  - `runs[]` 每项增加 `reflection`、`primary_value`
  - 新增 `figures: [{index, name, caption}]`、`iteration_state`
  - `plan.hypotheses[].status` 反映最新回写
- `GET /experiments/{id}/figures/{index}/image` → PNG FileResponse（成员校验，模式同论文 figures）
- `POST /projects/{pid}/experiments` 的 `params.budget` 扩展：`{max_hours, max_runs, no_improve_stop: 2}`

## 4. WS/SSE

- 复用现有 voyage SSE 与 `experiment.status`；迭代轮次进展通过 voyage step observation 与 runs 列表轮询呈现（不新增事件类型）

## 5. 前端

- 实验详情 **Run Tab → 「运行与迭代」**：
  - 迭代时间线：每轮一卡（`#seq`、状态、主指标值 + 与上轮差值 Delta、reflection 的 observation/diagnosis/planned_change 折叠、decision 徽章 improve/debug/stop）
  - 顶部主指标趋势（MetricChart 单序列 primary_value by seq）+ 迭代状态（无提升计数/debug 计数/停止原因）
  - 原实时日志面板保留（跟踪当前 run）
- **Plan Tab**：假设清单 HypChip 状态实时反映回写（verified 绿/falsified 红/testing 灰）+ evidence tooltip
- **新「图表 Figures」区**（Report Tab 内或独立小节）：实验图表画廊（复用 FigureGallery 模式，数据源 experiment figures 端点）+ caption
- 新建实验表单预算区加 no_improve_stop 说明文案

## 6. 测试要点（离线 MockSSH + fake LLM）

- 迭代 3 轮路径（improve→improve→stop）：runs/reflection/假设回写/主指标序列断言
- 早停：连续 2 轮无提升自动停；debug 限额 3 次超限终止；max_runs 截断
- figures：脚本执行 mock、VLM 质检失败重试、Experiment.figures 落库、图片端点
- fake provider 扩展：reflection JSON marker、plot 脚本 marker、质检 marker
- 现有 121 例不回归
