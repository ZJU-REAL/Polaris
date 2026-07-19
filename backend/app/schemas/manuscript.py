"""稿件 / 稿件文件 / 编译结果 schema（docs/api-m5-b.md §1/§2/§4/§5）。"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TemplateInfo(BaseModel):
    """统一模板信息：builtin（内置简化）/ seeded（官方）/ uploaded（用户上传）。"""

    id: str  # builtin=key；库内模板=uuid 字符串（创建稿件时作为 template 传回）
    name: str
    description: str | None = None
    source: Literal["builtin", "seeded", "uploaded"] = "builtin"
    scope: Literal["global", "project"] = "global"
    project_id: str | None = None
    engine: str = "tectonic"
    page_limit: int | None = None
    sections: list[str] = Field(default_factory=list)
    unofficial: bool = True  # 简化样式（非官方 .sty），投稿前须替换官方版
    downloadable: bool = False  # 库内模板可下载 zip；内置的不可
    downloaded: bool = True  # 官方模板未下载时为 false（画廊显示「未下载」）
    download_key: str | None = None  # 未下载官方模板的 manifest key，触发按需下载用
    file_count: int = 0


class TemplateSeedResult(BaseModel):
    key: str
    name: str
    status: Literal["seeded", "skipped", "failed"]
    detail: str | None = None


class TemplateDownloadProgress(BaseModel):
    """官方模板按需下载进度（进度条 / SSE）。"""

    key: str
    name: str
    phase: Literal["pending", "downloading", "extracting", "done", "failed"]
    percent: int = 0
    detail: str = ""
    template_id: str | None = None  # done 后的真实模板 id（用它建稿）
    error: str | None = None


class ManuscriptCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    template: str  # builtin key 或库内模板 id/key
    idea_id: uuid.UUID | None = None
    experiment_id: uuid.UUID | None = None


CompileEngine = Literal["tectonic", "pdflatex", "xelatex", "lualatex"]


class ManuscriptUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=512)
    # Overleaf 式编译设置：入口主文件 + 编译器
    main_tex: str | None = Field(default=None, min_length=1, max_length=1024)
    engine: CompileEngine | None = None


class ManuscriptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    idea_id: uuid.UUID | None
    experiment_id: uuid.UUID | None
    title: str
    template: str
    main_tex: str  # 编译入口主文件
    engine: str  # 编译器 tectonic | pdflatex | xelatex | lualatex
    status: str
    review_passed: bool  # M5-C：评审通过标记（submit 前置）
    created_at: datetime
    updated_at: datetime


class ManuscriptFileBrief(BaseModel):
    id: uuid.UUID
    path: str
    size: int  # 内容 utf-8 字节数（二进制文件为磁盘字节数）
    readonly: bool
    is_binary: bool = False
    is_folder: bool = False
    updated_at: datetime


class FolderCreate(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class CollaboratorRead(BaseModel):
    user_id: str
    email: str
    display_name: str
    role: str
    is_owner: bool


class AddCollaborator(BaseModel):
    user_id: uuid.UUID
    role: Literal["member", "owner"] = "member"


class ShareLinkCreate(BaseModel):
    expires_days: int | None = Field(default=14, ge=1, le=365)
    max_uses: int | None = Field(default=None, ge=1, le=1000)


class ShareLink(BaseModel):
    token: str
    join_path: str  # 前端拼域名：/join/{token}
    expires_at: datetime | None
    max_uses: int | None


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
