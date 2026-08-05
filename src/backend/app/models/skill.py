"""技能系统（docs/skill-system.md）：可版本化、可装配的判断性任务指令包。

- Skill：技能主体（builtin 内置只读 / user 个人）
- SkillVersion：不可变版本（manifest JSON + markdown body），只增不改；
  「当前版本」= 该技能 version 最大的一行，不另存指针
- UserSkill：全局启用记录：用户 × 技能 × 注入点，可 pin 版本与配置
  （技能不绑定课题，启用后对该用户所有新任务生效）
- SkillListing：技能市场条目（发布指向具体版本，管理员审核后可安装）
- SkillRating：市场评分（每人每条目一条，可更新）
"""

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class Skill(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "skills"
    # slug 唯一性按 scope 语义在 service 层校验（builtin 全局唯一 / user 按 owner 唯一），
    # DB 只留索引：owner_id 为 NULL 的 builtin 行无法用复合唯一约束表达
    __table_args__ = ()

    slug: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # guidance | rubric | persona | workflow（docs/skill-system.md §1.1）
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    # builtin | user
    scope: Mapped[str] = mapped_column(String(16), default="user", index=True, nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    versions: Mapped[list["SkillVersion"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan", order_by="SkillVersion.version"
    )


class SkillVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("skill_id", "version", name="uq_skill_versions_skill_ver"),)

    skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    changelog: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    skill: Mapped[Skill] = relationship(back_populates="versions")


class UserSkill(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """技能全局启用（不绑定课题）：同一注入点可启用多个技能，按 sort_order 拼接。"""

    __tablename__ = "user_skills"
    __table_args__ = (
        UniqueConstraint("user_id", "skill_id", "target", name="uq_user_skills_target"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # NULL = 跟随最新版本；否则 pin 到指定版本
    version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="SET NULL")
    )
    # 注入点（白名单见 schemas/skill.py，schema 层校验）
    target: Mapped[str] = mapped_column(String(64), nullable=False)
    # config_schema 定义的旋钮取值
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    skill: Mapped[Skill] = relationship()


class SkillListing(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """技能市场条目：永远指向具体 SkillVersion（发布后原技能继续演进互不影响）。"""

    __tablename__ = "skill_listings"

    skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), index=True, nullable=False
    )
    skill_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skill_versions.id", ondelete="CASCADE"), nullable=False
    )
    summary: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str] | None] = mapped_column(JSONVariant)
    # pending | approved | rejected | delisted（管理员审核）
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True, nullable=False)
    install_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    comment: Mapped[str | None] = mapped_column(Text)  # 审核意见

    skill: Mapped[Skill] = relationship()
    version: Mapped[SkillVersion] = relationship()


class SkillRating(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "skill_ratings"
    __table_args__ = (UniqueConstraint("listing_id", "user_id", name="uq_skill_ratings_user"),)

    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skill_listings.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-5
    comment: Mapped[str | None] = mapped_column(Text)
