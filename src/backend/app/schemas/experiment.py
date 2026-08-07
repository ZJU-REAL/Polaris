"""实验 / 运行 schema（docs/api-m4.md §2 + docs/api-m5-a.md §3）。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExperimentBudget(BaseModel):
    # 0 = 无限时（默认）：修复循环/运行轮询只受它约束，不再按次数设限（用户定调）
    max_hours: float = Field(default=0, ge=0)
    max_runs: int = Field(default=10, ge=1)
    # 连续 N 轮主指标无提升即停（docs/api-m5-a.md §3）
    no_improve_stop: int = Field(default=2, ge=1)


class IntakeQA(BaseModel):
    """开题问答：创建实验时 AI 按 idea 提出的问题与用户的回答（可留空）。"""

    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(default="", max_length=4000)


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
    # 开题问答（AI 按 idea 生成 ≤5 个整体性问题，用户作答后随创建提交；
    # 原文进 plan 与 codegen prompt——其余不确定点实验中经 ask 机制动态交互）
    intake: list[IntakeQA] | None = None


class ExperimentIntakeQuestion(BaseModel):
    question: str
    hint: str | None = None
    # 候选答案（前端渲染为可点选项 + 「其他」自由输入）；可为空列表
    options: list[str] = []


class ExperimentIntakeQuestions(BaseModel):
    questions: list[ExperimentIntakeQuestion]


class ExperimentIntakeRequest(BaseModel):
    idea_id: uuid.UUID


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
    trashed_at: datetime | None = None  # 非空即在回收站
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
