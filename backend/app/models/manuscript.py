"""论文稿件与其 LaTeX 文件（docs/api-m5-b.md §1/§2）。"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

# 状态流转（docs/api-m5-b.md §1/§5/§7）：
#   draft →(写作 voyage) writing →(编译 ok) compiled →(submit) under_review
#   →(paper_submission 闸门批准) submitted；approved 为 Wave 3 评审通过预留
MANUSCRIPT_STATUSES = (
    "draft",
    "writing",
    "compiled",
    "under_review",
    "approved",
    "submitted",
)


class Manuscript(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "manuscripts"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    idea_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ideas.id", ondelete="SET NULL"), index=True
    )
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("experiments.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    # 模板 pack key：neurips2026 | iclr2026 | acl（app/assets/templates/）
    template: Mapped[str] = mapped_column(
        String(64), default="neurips2026", server_default="neurips2026", nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    # 编译入口主文件（Overleaf 式可设置）：编译/导出以此为主 .tex，默认 main.tex，
    # 从官方模板建稿时取模板检测到的主文件（如 neurips_2026.tex）
    main_tex: Mapped[str] = mapped_column(
        String(1024), default="main.tex", server_default="main.tex", nullable=False
    )
    # 编译器（Overleaf 式可切换）：tectonic | pdflatex | xelatex | lualatex
    engine: Mapped[str] = mapped_column(
        String(32), default="tectonic", server_default="tectonic", nullable=False
    )
    # 论文评审通过标记（docs/api-m5-c.md §4）：meta.rating ≥ 6 且无 fabricated 引用
    # → true；submit 前置条件（未通过 409 REVIEW_REQUIRED）
    review_passed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # 软删除（垃圾箱）：非空即在垃圾箱，列表默认过滤掉；清空垃圾箱才真正删除
    trashed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # 置顶：非空即置顶，列表按 pinned_at 优先排在前面
    pinned_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # 防幻觉事实源（docs/api-m5-b.md §3）：{idea, hypotheses, metrics, figures,
    # citations, generated_at}；M5-C 评审不通过时追加 revision_notes（修订说明）；
    # citations 条目附内部 paper_id / source 供编译生成 bib
    fact_pack: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    # 最近一次编译结果（CompileResult：{version, status, pdf_available,
    # diagnostics, compiled_at, duration_ms}）
    latest_compile: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)

    files: Mapped[list["ManuscriptFile"]] = relationship(
        back_populates="manuscript", cascade="all, delete-orphan"
    )


class ManuscriptFile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "manuscript_files"

    manuscript_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("manuscripts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    path: Mapped[str] = mapped_column(String(1024), nullable=False)  # e.g. main.tex
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)  # latex（二进制时为空）
    # 模板样式文件（.sty/.cls/.bst）只读：不可改删、不开 CRDT 房间
    readonly: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 二进制资源（图片/PDF/字体等）：content 为空，字节落磁盘
    # data_dir/manuscripts/<id>/assets/<path>；文件夹占位用 is_folder
    is_binary: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    is_folder: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    manuscript: Mapped[Manuscript] = relationship(back_populates="files")
    versions: Mapped[list["ManuscriptFileVersion"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


# 版本快照来源：pre_ai（AI 分节写入前）| compile（编译当刻）| pre_restore（恢复前备份）
FILE_VERSION_ORIGINS = ("pre_ai", "compile", "pre_restore")


class ManuscriptFileVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """稿件文件的版本快照（自动打点，人工可回滚）。"""

    __tablename__ = "manuscript_file_versions"

    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("manuscript_files.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(nullable=False)  # 同文件内递增
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str | None] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    file: Mapped[ManuscriptFile] = relationship(back_populates="versions")


# 模板来源：builtin（app/assets 内置简化样式）| seeded（从 git/zip 拉取的官方模板）
# | uploaded（用户上传 zip）
TEMPLATE_SOURCES = ("builtin", "seeded", "uploaded")
# 模板可见范围：global（全平台可选）| project（仅所属研究方向可选）
TEMPLATE_SCOPES = ("global", "project")


class ManuscriptTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """论文模板（DB 元数据 + data_dir/templates/<id>/files 下的文件）。

    内置 3 个简化样式仍从 app/assets/templates 读取、不入库；本表存
    「上传的 zip 模板」与「从 git/zip 种子拉来的官方模板」。
    """

    __tablename__ = "manuscript_templates"

    key: Mapped[str] = mapped_column(String(96), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024))
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # seeded | uploaded
    scope: Mapped[str] = mapped_column(
        String(16), default="global", server_default="global", nullable=False
    )
    # scope=project 时限定所属研究方向；global 为 null
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # 主 tex 文件路径（展开/编译入口）；引擎提示 pdflatex|xelatex|lualatex|tectonic
    main_tex: Mapped[str] = mapped_column(
        String(512), default="main.tex", server_default="main.tex", nullable=False
    )
    engine: Mapped[str] = mapped_column(
        String(16), default="tectonic", server_default="tectonic", nullable=False
    )
    page_limit: Mapped[int | None] = mapped_column()
    # AI 起草可选分节（无标记的官方模板可为空 → 起草降级为追加）
    sections: Mapped[list[str] | None] = mapped_column(JSONVariant)
    unofficial: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    file_count: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)
    # 额外元数据（bib 风格、来源 URL、种子提交号等）
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
