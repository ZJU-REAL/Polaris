"""Agent 编排层（M3 实现，暂为占位）。

规划中的 agents：
- interviewer: 研究方向定义访谈，产出 Project.definition
- surveyor: 文献检索/打分/编纂（Paper/Concept wiki）
- ideator: 想法生成 + 四维评分 + Elo 锦标赛
- reviewer: 多人设评审（ReviewSession/ReviewMessage）
- experimenter: 实验计划/远程执行（asyncssh，写操作过 Gate）
- writer: LaTeX 稿件撰写（Manuscript）

约定：LLM 调用一律经 app.core.llm 抽象层；长任务跑在 ARQ worker；
需人工介入处创建 Gate 并暂停，审批后恢复。
"""
