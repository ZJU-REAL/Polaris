"""稿件 / 稿件文件 / 编译结果 schema（docs/api-m5-b.md §1/§2/§4/§5）。"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TemplateInfo(BaseModel):
    key: str
    name: str
    page_limit: int
    sections: list[str]
    unofficial: bool = True  # 开发用简化样式（非官方 .sty），投稿前须替换官方版


class ManuscriptCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    template: str
    idea_id: uuid.UUID | None = None
    experiment_id: uuid.UUID | None = None


class ManuscriptUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=512)


class ManuscriptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    idea_id: uuid.UUID | None
    experiment_id: uuid.UUID | None
    title: str
    template: str
    status: str
    review_passed: bool  # M5-C：评审通过标记（submit 前置）
    created_at: datetime
    updated_at: datetime


class ManuscriptFileBrief(BaseModel):
    id: uuid.UUID
    path: str
    size: int  # 内容 utf-8 字节数
    readonly: bool
    updated_at: datetime


class CompileDiagnostic(BaseModel):
    severity: Literal["error", "warning"]
    file: str | None
    line: int | None
    rule: Literal["undefined_citation", "undefined_reference", "latex_error", "overfull", "other"]
    message: str


class CompileResult(BaseModel):
    version: int
    status: Literal["ok", "error", "timeout"]
    pdf_available: bool
    diagnostics: list[CompileDiagnostic]
    compiled_at: datetime
    duration_ms: int


class ManuscriptDetail(ManuscriptRead):
    files: list[ManuscriptFileBrief]
    fact_pack: dict[str, Any] | None
    latest_compile: CompileResult | None
    writing_voyage_id: uuid.UUID | None


class ManuscriptFileCreate(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    content: str = ""


class ManuscriptFileRename(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class ManuscriptFileContent(BaseModel):
    id: uuid.UUID
    path: str
    content: str
    readonly: bool


class DraftRequest(BaseModel):
    # null = 模板全部节；显式列表时只写指定节（related_work 也在可选值内）
    sections: list[str] | None = None
    notes: str | None = None


class FileVersionMeta(BaseModel):
    """版本快照元数据（列表用，不含内容）。"""

    id: uuid.UUID
    seq: int
    origin: Literal["pre_ai", "compile", "pre_restore"]
    label: str | None
    size: int  # 内容 utf-8 字节数
    created_by: uuid.UUID | None
    created_at: datetime


class FileVersionContent(FileVersionMeta):
    content: str


class AssistRequest(BaseModel):
    """内联 AI 写作辅助（SSE 流）：polish/rewrite 需要 text，rewrite 还需要 instruction。"""

    mode: Literal["polish", "rewrite", "continue"]
    text: str = Field(default="", max_length=20_000)
    instruction: str = Field(default="", max_length=4_000)
    before: str = Field(default="", max_length=8_000)
    after: str = Field(default="", max_length=8_000)
