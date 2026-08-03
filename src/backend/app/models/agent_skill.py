"""Agent 技能（Skills v2，照搬 SKILL.md 的形状）。

与旧的 ``skills`` 表是**两套东西**，不是改造：旧那套是"人在 19 个写死的注入点上勾选、
启动时把正文无条件全文拼进 system prompt"；这套是"模型自己判断要不要用，常驻的只有
一句 description，正文按需加载"。

三级渐进披露：

1. **目录**（L1）：system prompt 里每个技能只占一行 ``name：description``，几十 token。
2. **正文**（L2）：模型调 ``skill_load(slug)`` 才把 SKILL.md 正文取回来——作为**工具结果**
   追加，绝不改写 system prompt（改了会作废整个 prompt cache 前缀）。
3. **附件**（L3）：正文里提到的模板/参考资料，模型调 ``skill_read_file`` 按需读。

``description`` 是这套机制的承重结构：它不是给人看的说明，是**触发条件**。写成"这是
什么"而不是"什么时候用它"，模型就永远不会加载它。
"""

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import JSONVariant, TimestampMixin, UUIDPrimaryKeyMixin

#: auto = 模型可自主加载；manual = 只能由用户显式调用（危险或昂贵的技能用它，
#: 也是外部导入技能的安全默认值——来路不明的东西不该被模型自主加载）
SKILL_INVOCATIONS = ("auto", "manual")
SKILL_SCOPES = ("builtin", "user")

#: 目录里单条的长度上限。整个目录常驻 system prompt，控制不住就吃光预算。
DESCRIPTION_MAX = 300
#: 正文上限。渐进披露之后它不再无条件进 prompt，所以可以比旧系统的 8K 宽得多。
BODY_MAX = 64 * 1024


class AgentSkill(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_skills"
    __table_args__ = (
        UniqueConstraint("owner_id", "slug", name="uq_agent_skills_owner_slug"),
        Index("ix_agent_skills_scope", "scope", "enabled"),
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: **触发条件**，不是功能介绍。渐进披露的成败全在这一句写得好不好。
    description: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 收窄助手的能力面。只能收窄，永不扩权（与会话白名单求交集）。None = 不收窄。
    allowed_tools: Mapped[list[str] | None] = mapped_column(JSONVariant, nullable=True)
    invocation: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    #: builtin 的 owner 为空；用户技能归属本人
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 加载次数，用于目录超限时的截断排序（常用的留在目录里）
    load_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AgentSkillFile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """技能目录里的附件：模板、参考资料、给模型抄的脚本。

    **脚本只能被读，不能执行**：平台只有远程 SSH/容器 runner（命令走严格模板白名单，
    LLM 不能拼 shell），没有本地沙箱。假装有会比没有更危险。
    """

    __tablename__ = "agent_skill_files"
    __table_args__ = (UniqueConstraint("skill_id", "path", name="uq_agent_skill_files_path"),)

    skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_skills.id", ondelete="CASCADE"), nullable=False, index=True
    )
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


def skill_catalog_line(skill: AgentSkill) -> str:
    """目录里的一行。名字 + 触发条件，不带正文。"""
    desc = (skill.description or "").strip().replace("\n", " ")
    return f"- {skill.slug}：{desc[:DESCRIPTION_MAX]}"


def as_dict(skill: AgentSkill) -> dict[str, Any]:
    return {
        "slug": skill.slug,
        "name": skill.name,
        "description": skill.description,
        "invocation": skill.invocation,
        "allowed_tools": skill.allowed_tools,
        "scope": skill.scope,
    }
