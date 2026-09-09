"""平台内置指引文档（#741：技能三套并行收敛第一步）。

跨学科工作流的指引原先寄存在 v1 技能表（skills/skill_versions 的 builtin 行）里——
那是唯一一处**新功能代码仍在写 v1** 的地方。v1 已冻结（仅服务存量用户配置），
而 v2 agent_skills 的形状也装不下它：v2 是「模型自己判断要不要加载」的渐进披露，
这里需要的是「引擎在固定注入点无条件全文注入」——两者的消费模型南辕北辙，
硬塞进 v2 只会让这份文档出现在 Buddy 的技能目录里（错误的地方）。

所以给它一张自己的小表：按 (slug, version) 只增不改（同 v1 SkillVersion 的
不可变语义），最新版本 = version 最大的一行。任务运行时把内容整体冻进
checkpoint，断点恢复与回放不再依赖本表。
"""

from typing import Any

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin


class GuidanceDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "guidance_documents"
    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_guidance_documents_slug_ver"),
    )

    slug: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: 注入点列表（含 navigator.free_plan 的虚拟注入点）
    targets: Mapped[list[str] | None] = mapped_column(JSONVariant)
    #: workflow 步骤模板（navigator.free_plan 消费；纯 guidance 文档为 NULL）
    steps: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONVariant)
