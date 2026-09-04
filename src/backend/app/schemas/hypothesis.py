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
