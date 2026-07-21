"""用户反馈：分类 + 上下文 + 截图；管理员可 LLM 改写成 issue 草稿并建 GitHub issue。"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

# 分类：缺陷 / 功能建议 / 内部任务 / 界面体验 / 使用疑问 / 性能 / 其他
FEEDBACK_TYPES = ("bug", "feature", "task", "ui", "question", "perf", "other")
# 严重度
FEEDBACK_SEVERITIES = ("blocker", "high", "normal", "low")
# 处理状态
FEEDBACK_STATUSES = ("new", "triaged", "in_progress", "resolved", "closed", "wontfix")


class Feedback(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "feedback"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    type: Mapped[str] = mapped_column(String(16), default="bug", nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="normal", nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 提交时自动抓取：当前路由 + 推导的模块名（给 CC 指路）
    route: Mapped[str | None] = mapped_column(String(255))
    module: Mapped[str | None] = mapped_column(String(64))
    # 环境上下文 JSON：app 版本 / UA / 视口 / 当前研究方向 / 语言
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    status: Mapped[str] = mapped_column(String(16), default="new", nullable=False)
    admin_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # LLM 生成的 issue 草稿（{title, body, labels}），admin 可编辑后再建
    issue_draft: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    # 建成的 GitHub issue
    github_issue_number: Mapped[int | None] = mapped_column(Integer)
    github_issue_url: Mapped[str | None] = mapped_column(String(255))


class FeedbackImage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """反馈截图：落盘到 data_dir/feedback/<feedback_id>/，路径入库，FileResponse 服务。"""

    __tablename__ = "feedback_images"

    feedback_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("feedback.id", ondelete="CASCADE"), index=True, nullable=False
    )
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
