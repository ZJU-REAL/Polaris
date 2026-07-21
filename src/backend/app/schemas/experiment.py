"""实验 / 运行 schema（docs/api-m4.md §2 + docs/api-m5-a.md §3）。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExperimentBudget(BaseModel):
    max_hours: float = Field(default=4, ge=0)
    max_runs: int = Field(default=10, ge=1)
    # 连续 N 轮主指标无提升即停（docs/api-m5-a.md §3）
    no_improve_stop: int = Field(default=2, ge=1)


class ExperimentParams(BaseModel):
    gpu_hint: str | None = None
    budget: ExperimentBudget | None = None
    # 评测模型：非空时 setup 会把 default 路由的 base_url/api_key + 该 model
    # 写成 workdir/llm_config.json，供 training-free agentic 评测代码调用 LLM API
    eval_model: str | None = None
    # HF 镜像：env.sh 注入 HF_ENDPOINT=https://hf-mirror.com（大陆网络拉 HF 模型/数据集）
    hf_mirror: bool = False
    # 用户对实验的补充说明（原文进 plan 与 codegen prompt）
    extra_notes: str | None = None


class ExperimentCreate(BaseModel):
    idea_id: uuid.UUID
    credential_id: uuid.UUID
    params: ExperimentParams | None = None


class ExperimentRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    idea_id: uuid.UUID
    idea_title: str
    # planning | awaiting_gate | setup | running | reporting | done | failed | cancelled
    status: str
    voyage_id: uuid.UUID | None
    workdir: str | None
    server_host: str | None
    budget: dict[str, Any] | None
    trashed_at: datetime | None = None  # 非空即在垃圾箱
    created_at: datetime
    updated_at: datetime


class ExperimentRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seq: int
    command: str
    status: str  # running | succeeded | failed
    exit_code: int | None
    log_path: str | None
    metrics: dict[str, Any] | None
    # 该轮 structured reflection（docs/api-m5-a.md §1）
    reflection: dict[str, Any] | None
    primary_value: float | None
    started_at: datetime | None
    finished_at: datetime | None


class ExperimentFigure(BaseModel):
    """实验图表（内部 path 不出 API，图片经 figures/{index}/image 端点取）。"""

    index: int
    name: str
    caption: str | None


class ExperimentDetail(ExperimentRead):
    plan: dict[str, Any] | None
    runs: list[ExperimentRunRead]
    report: str | None
    metrics: dict[str, Any] | None
    figures: list[ExperimentFigure]
    # {no_improve_streak, debug_count, stopped_reason}
    iteration_state: dict[str, Any] | None


class ExperimentLogsRead(BaseModel):
    lines: list[str]
    truncated: bool
