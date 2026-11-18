"""假设/实验树的读 schema（#637）。写入走引擎（D2/D3），本期无写模型。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class HypothesisNodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    parent_id: uuid.UUID | None  # None = 树根
    kind: str  # hypothesis | experiment | analysis
    statement: str
    # 子命题→文献绑定 [{subclaim, stance, paper_ids, snippets}]
    grounding: list[Any] | None
    novelty_report: dict[str, Any] | None
    feasibility: dict[str, Any] | None
    score: float | None
    status: str  # open | expanded | pruned | validated | refuted
    created_at: datetime
    updated_at: datetime
    # 被剪节点的原因（取自任务的决策留痕；没被剪、或随父级联被剪的为 None）：
    # prune 是结构化的 {score, threshold, support_ratio, novel_ratio,
    # cause: low_score | not_novel | no_support}，只有自动剪枝且记录较新时才有；
    # prune_reason 是当时记下的原话，旧记录只有它
    prune: dict[str, Any] | None = None
    prune_reason: str | None = None
