"""Skills v2：SKILL.md 解析、目录渲染、按需加载。

渐进披露的三级在这里落地。最要紧的一条约束写在前面：**技能正文永远作为工具结果
追加，绝不写进 system prompt**。system prompt 是稳定前缀，改它等于作废整个 prompt
cache——而每轮重发的历史加工具 schema 本来就是输入 token 的大头。
"""

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_skill import (
    BODY_MAX,
    SKILL_INVOCATIONS,
    AgentSkill,
    AgentSkillFile,
    skill_catalog_line,
)

#: 目录整体的长度上限。超了按 load_count 降序截断——常用的留在目录里。
CATALOG_MAX_CHARS = 4000
#: 单个附件的大小上限
FILE_MAX_CHARS = 256 * 1024

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


class SkillParseError(ValueError):
    """SKILL.md 的 frontmatter 有问题。"""


def parse_skill_md(text: str) -> dict[str, Any]:
    """解析 SKILL.md：YAML frontmatter + markdown 正文。

    只认几个字段，而且**不引入 YAML 依赖**——frontmatter 就是几行 ``key: value``
    加一个可选的列表，手写解析比拉一个库进来划算，也不给"技能里塞任意 YAML"留口子。
    """
    match = _FRONTMATTER_RE.match(text.strip())
    if not match:
        raise SkillParseError("缺少 --- 包裹的 frontmatter")
    raw, body = match.group(1), match.group(2).strip()

    fields: dict[str, Any] = {}
    key: str | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("- ") and key:
            fields.setdefault(key, [])
            if isinstance(fields[key], list):
                fields[key].append(line.split("- ", 1)[1].strip())
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().replace("-", "_")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            fields[key] = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
        elif value in (">", "|"):
            fields[key] = ""  # 折叠块：下面的缩进行会被下一轮当作续行拼上
        else:
            fields[key] = value.strip("'\"")

    # 折叠块（description: > 换行缩进）的续行拼接
    if fields.get("description") == "":
        lines = []
        seen = False
        for line in raw.splitlines():
            if line.strip().startswith("description:"):
                seen = True
                continue
            if seen:
                if line.startswith((" ", "\t")):
                    lines.append(line.strip())
                else:
                    break
        fields["description"] = " ".join(lines)

    slug = str(fields.get("name") or "").strip()
    if not _SLUG_RE.match(slug):
        raise SkillParseError(f"name 必须是小写短横线 slug：{slug!r}")
    description = str(fields.get("description") or "").strip()
    if not description:
        raise SkillParseError("description 不能为空——它是模型判断要不要加载的唯一依据")
    invocation = str(fields.get("invocation") or "auto")
    if invocation not in SKILL_INVOCATIONS:
        raise SkillParseError(f"invocation 只能是 {SKILL_INVOCATIONS}")
    tools = fields.get("allowed_tools")
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",") if t.strip()]
    return {
        "slug": slug,
        "name": str(fields.get("title") or slug),
        "description": description,
        "invocation": invocation,
        "allowed_tools": tools or None,
        "body": body[:BODY_MAX],
    }


async def visible_skills(session: AsyncSession, *, user_id: uuid.UUID | None) -> list[AgentSkill]:
    """这个用户能看到的技能：内置的 + 自己的。"""
    stmt = select(AgentSkill).where(AgentSkill.enabled.is_(True))
    if user_id is None:
        stmt = stmt.where(AgentSkill.scope == "builtin")
    else:
        stmt = stmt.where(
            (AgentSkill.scope == "builtin") | (AgentSkill.owner_id == user_id)
        )
    stmt = stmt.order_by(AgentSkill.load_count.desc(), AgentSkill.slug)
    return list((await session.execute(stmt)).scalars().all())


async def render_catalog(session: AsyncSession, *, user_id: uuid.UUID | None) -> str:
    """L1：常驻 system prompt 的技能目录。只有名字和触发条件，没有正文。

    超出预算时按加载次数截断——常用的留下。这比按字母序砍更合理，也比不截断安全：
    目录是每轮都要重发的。
    """
    skills = [s for s in await visible_skills(session, user_id=user_id) if s.invocation == "auto"]
    if not skills:
        return ""
    lines: list[str] = []
    used = 0
    for skill in skills:
        line = skill_catalog_line(skill)
        if used + len(line) > CATALOG_MAX_CHARS:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return "可用技能（需要时用 skill_load 取正文，不要凭名字猜内容）：\n" + "\n".join(lines)


async def load_skill(
    session: AsyncSession, *, slug: str, user_id: uuid.UUID | None
) -> dict[str, Any] | None:
    """L2：取技能正文与附件清单。同时把加载次数加一（目录截断按它排序）。"""
    skill = await _find(session, slug=slug, user_id=user_id)
    if skill is None:
        return None
    files = (
        (
            await session.execute(
                select(AgentSkillFile.path, AgentSkillFile.size).where(
                    AgentSkillFile.skill_id == skill.id
                )
            )
        )
        .all()
    )
    skill.load_count += 1
    await session.flush()
    return {
        "slug": skill.slug,
        "name": skill.name,
        "body": skill.body,
        "allowed_tools": skill.allowed_tools,
        "files": [{"path": p, "size": s} for p, s in files],
    }


async def read_skill_file(
    session: AsyncSession, *, slug: str, path: str, user_id: uuid.UUID | None
) -> str | None:
    """L3：读技能目录里的一个附件。

    路径不做任何拼接与规范化——附件是**表里的行**，不是文件系统里的文件，所以既没有
    ``..`` 逃逸的问题，也不需要为此写一堆防御代码。
    """
    skill = await _find(session, slug=slug, user_id=user_id)
    if skill is None:
        return None
    row = await session.scalar(
        select(AgentSkillFile.content).where(
            AgentSkillFile.skill_id == skill.id, AgentSkillFile.path == path
        )
    )
    return row[:FILE_MAX_CHARS] if row is not None else None


async def _find(
    session: AsyncSession, *, slug: str, user_id: uuid.UUID | None
) -> AgentSkill | None:
    stmt = select(AgentSkill).where(AgentSkill.slug == slug, AgentSkill.enabled.is_(True))
    if user_id is None:
        stmt = stmt.where(AgentSkill.scope == "builtin")
    else:
        stmt = stmt.where((AgentSkill.scope == "builtin") | (AgentSkill.owner_id == user_id))
    return (await session.execute(stmt)).scalars().first()


async def upsert_from_md(
    session: AsyncSession,
    *,
    text: str,
    user_id: uuid.UUID | None,
    scope: str = "user",
    files: dict[str, str] | None = None,
) -> AgentSkill:
    """从 SKILL.md 建/更新一个技能（同 slug 覆盖）。"""
    parsed = parse_skill_md(text)
    # owner_id 为 None（内置技能）时必须用 IS NULL：`owner_id == None` 翻成 SQL 是
    # `owner_id = NULL`，永远为假——那样每次启动种子都会重复插入一份。
    owner_clause = (
        AgentSkill.owner_id.is_(None) if user_id is None else AgentSkill.owner_id == user_id
    )
    existing = await session.scalar(
        select(AgentSkill).where(AgentSkill.slug == parsed["slug"], owner_clause)
    )
    skill = existing or AgentSkill(slug=parsed["slug"], owner_id=user_id, scope=scope)
    skill.name = parsed["name"]
    skill.description = parsed["description"]
    skill.body = parsed["body"]
    skill.allowed_tools = parsed["allowed_tools"]
    skill.invocation = parsed["invocation"]
    skill.enabled = True
    session.add(skill)
    await session.flush()

    for path, content in (files or {}).items():
        row = await session.scalar(
            select(AgentSkillFile).where(
                AgentSkillFile.skill_id == skill.id, AgentSkillFile.path == path
            )
        )
        if row is None:
            row = AgentSkillFile(skill_id=skill.id, path=path)
            session.add(row)
        row.content = content[:FILE_MAX_CHARS]
        row.size = len(content)
    await session.flush()
    return skill


def narrow_tools(session_tools: tuple[str, ...], skill_tools: list[str] | None) -> tuple[str, ...]:
    """技能的 allowed-tools 与会话白名单求交集。

    **只能收窄，永不扩权**——技能里写一个会话没给的工具名，结果是那个工具不可用，
    而不是把它加进来。这条是安全底线，不给配置项。
    """
    if not skill_tools:
        return session_tools
    allowed = set(skill_tools)
    return tuple(t for t in session_tools if t in allowed)
