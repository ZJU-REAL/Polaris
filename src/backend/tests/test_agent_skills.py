"""Skills v2：SKILL.md 解析与三级渐进披露。

与旧的 skills 表是两套东西：那套是"人在 19 个写死的注入点上勾选、启动时把正文无条件
全文拼进 system prompt"；这套是"模型自己判断要不要用，常驻的只有一句 description"。

**description 是承重结构**——它不是给人看的功能介绍，是模型判断要不要加载的唯一依据。
写成"这是什么"而不是"什么时候用"，技能就永远不会被加载。
"""

import uuid

import pytest

from app.core.db import get_sessionmaker
from app.services import agent_skills
from tests.conftest import register_and_login

SKILL_MD = """\
---
name: literature-triage
description: >
  批量筛选候选文献并给出保留/剔除建议。当用户说"帮我筛一下这批论文"、
  "这些跟我方向相关吗"，或需要对每日推送做初筛时使用。
allowed-tools: [search_papers, get_paper]
invocation: auto
---

# 文献初筛

先按方向说明逐篇判断，再给出保留/剔除清单。判据见 `rubric.md`。
"""


async def _user_id(client, email: str) -> uuid.UUID:
    from sqlalchemy import select

    from app.models.user import User

    await register_and_login(client, email=email)
    async with get_sessionmaker()() as session:
        return (await session.execute(select(User.id).where(User.email == email))).scalar_one()


# ---- 解析 ----


def test_frontmatter_is_parsed_without_a_yaml_dependency():
    """折叠块的 description、列表形式的 allowed-tools 都要认。

    手写解析而不是拉个 YAML 库进来：frontmatter 就这几个字段，而且不给「技能里塞
    任意 YAML」留口子。
    """
    parsed = agent_skills.parse_skill_md(SKILL_MD)
    assert parsed["slug"] == "literature-triage"
    assert "帮我筛一下这批论文" in parsed["description"], "折叠块的续行要拼上"
    assert parsed["allowed_tools"] == ["search_papers", "get_paper"]
    assert parsed["invocation"] == "auto"
    assert parsed["body"].startswith("# 文献初筛")


@pytest.mark.parametrize(
    "text,reason",
    [
        ("没有 frontmatter 的正文", "缺少 --- 包裹"),
        ("---\nname: Bad Slug\ndescription: x\n---\n正文", "slug 必须小写短横线"),
        ("---\nname: ok-slug\ndescription:\n---\n正文", "description 不能为空"),
        ("---\nname: ok-slug\ndescription: x\ninvocation: whenever\n---\n正文", "invocation 枚举"),
    ],
)
def test_bad_frontmatter_is_rejected_with_a_reason(text, reason):
    with pytest.raises(agent_skills.SkillParseError):
        agent_skills.parse_skill_md(text)


# ---- L1 目录 ----


async def test_the_catalog_carries_descriptions_but_never_bodies(client):
    """常驻 system prompt 的目录里只有名字和触发条件。

    正文进了 system prompt 就等于作废整个 prompt cache 前缀，而它每轮都要重发。
    """
    user_id = await _user_id(client, "skill-catalog@example.com")
    async with get_sessionmaker()() as session:
        await agent_skills.upsert_from_md(session, text=SKILL_MD, user_id=user_id)
        await session.commit()
        catalog = await agent_skills.render_catalog(session, user_id=user_id)

    assert "literature-triage" in catalog
    assert "帮我筛一下这批论文" in catalog
    assert "# 文献初筛" not in catalog, "正文绝不能进目录"
    assert "rubric.md" not in catalog


async def test_manual_skills_stay_out_of_the_catalog(client):
    """invocation=manual 的技能不进目录——模型不该自主加载危险或来路不明的东西。"""
    user_id = await _user_id(client, "skill-manual@example.com")
    md = SKILL_MD.replace("invocation: auto", "invocation: manual").replace(
        "literature-triage", "danger-zone"
    )
    async with get_sessionmaker()() as session:
        await agent_skills.upsert_from_md(session, text=md, user_id=user_id)
        await session.commit()
        catalog = await agent_skills.render_catalog(session, user_id=user_id)

    assert "danger-zone" not in catalog


async def test_the_catalog_is_capped_and_keeps_the_popular_ones(client):
    """目录有预算上限；超了按加载次数截断，常用的留下。"""
    user_id = await _user_id(client, "skill-cap@example.com")
    async with get_sessionmaker()() as session:
        for i in range(60):
            md = SKILL_MD.replace("literature-triage", f"skill-{i:03d}")
            skill = await agent_skills.upsert_from_md(session, text=md, user_id=user_id)
            skill.load_count = i  # 越靠后越常用
        await session.commit()
        catalog = await agent_skills.render_catalog(session, user_id=user_id)

    assert len(catalog) <= agent_skills.CATALOG_MAX_CHARS + 200
    assert "skill-059" in catalog, "最常用的必须在"
    assert "skill-000" not in catalog, "最冷门的被挤掉"


# ---- L2 / L3 加载 ----


async def test_loading_a_skill_returns_the_body_and_the_file_list(client):
    user_id = await _user_id(client, "skill-load@example.com")
    async with get_sessionmaker()() as session:
        await agent_skills.upsert_from_md(
            session,
            text=SKILL_MD,
            user_id=user_id,
            files={"rubric.md": "# 判据\n1. 方向相关\n2. 方法新颖"},
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        loaded = await agent_skills.load_skill(
            session, slug="literature-triage", user_id=user_id
        )
        await session.commit()

    assert loaded is not None
    assert "# 文献初筛" in loaded["body"]
    rubric = "# 判据\n1. 方向相关\n2. 方法新颖"
    assert loaded["files"] == [{"path": "rubric.md", "size": len(rubric)}]
    assert loaded["allowed_tools"] == ["search_papers", "get_paper"]

    async with get_sessionmaker()() as session:
        content = await agent_skills.read_skill_file(
            session, slug="literature-triage", path="rubric.md", user_id=user_id
        )
    assert content is not None and "方法新颖" in content


async def test_another_users_skill_is_invisible(client):
    """别人的技能既不进我的目录，也加载不了。"""
    owner = await _user_id(client, "skill-owner@example.com")
    other = await _user_id(client, "skill-other@example.com")
    async with get_sessionmaker()() as session:
        await agent_skills.upsert_from_md(session, text=SKILL_MD, user_id=owner)
        await session.commit()

    async with get_sessionmaker()() as session:
        catalog = await agent_skills.render_catalog(session, user_id=other)
        loaded = await agent_skills.load_skill(
            session, slug="literature-triage", user_id=other
        )
    assert "literature-triage" not in catalog
    assert loaded is None


async def test_loading_bumps_the_counter_used_for_truncation(client):
    user_id = await _user_id(client, "skill-count@example.com")
    async with get_sessionmaker()() as session:
        await agent_skills.upsert_from_md(session, text=SKILL_MD, user_id=user_id)
        await session.commit()

    for _ in range(3):
        async with get_sessionmaker()() as session:
            await agent_skills.load_skill(session, slug="literature-triage", user_id=user_id)
            await session.commit()

    async with get_sessionmaker()() as session:
        skills = await agent_skills.visible_skills(session, user_id=user_id)
    assert skills[0].load_count == 3


# ---- allowed-tools 只能收窄 ----


def test_allowed_tools_can_only_narrow_never_widen():
    """技能里写一个会话没给的工具名，结果是那个工具不可用，而不是把它加进来。

    这条是安全底线，不做成配置项。
    """
    session_tools = ("search_papers", "get_paper", "read_fulltext")

    assert agent_skills.narrow_tools(session_tools, None) == session_tools
    assert agent_skills.narrow_tools(session_tools, ["search_papers"]) == ("search_papers",)
    # 技能想要一个会话没给的工具：不会凭空出现
    widened = agent_skills.narrow_tools(session_tools, ["search_papers", "delete_everything"])
    assert widened == ("search_papers",)
    assert "delete_everything" not in widened


# ---- 工具层 ----


async def test_the_skill_tools_are_registered_and_read_only(client):
    """两个技能工具进了注册表，而且是只读的——它们会经 MCP 暴露给外部客户端。"""
    from app.tools.registry import get_tool

    for name in ("skill_load", "skill_read_file"):
        spec = get_tool(name)
        assert spec is not None, name
        assert spec.read_only is True, f"{name} 必须只读"
        assert spec.input_schema["type"] == "object"


async def test_skill_load_tool_reports_a_missing_skill_instead_of_raising(client):
    """加载不存在的技能返回错误结果，不抛——模型下一轮自己纠正。"""
    from app.core.llm.router import get_llm_router
    from app.tools.context import ToolContext
    from app.tools.skills import skill_load

    user_id = await _user_id(client, "skill-missing@example.com")
    ctx = ToolContext(project_id=uuid.uuid4(), llm=get_llm_router(), user_id=user_id)
    result = await skill_load(ctx, {"slug": "not-there"})
    assert "error" in result


# ---- 内置种子与管理 API ----


async def test_builtin_seeding_is_idempotent(client):
    """种子跑两遍不重复。

    第一版 upsert_from_md 里 `owner_id == user_id` 在 user_id=None 时翻成 SQL 是
    `owner_id = NULL`，永远为假——查重必落空，每次启动都会再插一份。修法是 IS NULL。
    """
    from sqlalchemy import func, select

    from app.models.agent_skill import AgentSkill
    from app.services.builtin_agent_skills import ensure_builtin_agent_skills

    async with get_sessionmaker()() as session:
        first = await ensure_builtin_agent_skills(session)
        await ensure_builtin_agent_skills(session)
        total = await session.scalar(
            select(func.count()).select_from(AgentSkill).where(AgentSkill.scope == "builtin")
        )
    assert first >= 5
    assert total == first, f"跑两遍出现重复：{total} != {first}"


async def test_builtin_skills_appear_in_the_catalog(client):
    """种完之后目录不再是空的——渐进披露的 L1 真的有货了。"""
    from app.services.builtin_agent_skills import ensure_builtin_agent_skills

    user_id = await _user_id(client, "seed-catalog@example.com")
    async with get_sessionmaker()() as session:
        await ensure_builtin_agent_skills(session)
        catalog = await agent_skills.render_catalog(session, user_id=user_id)

    assert "polaris-search-playbook" in catalog
    assert "literature-triage" in catalog
    # 目录里是触发条件，不是正文
    assert "检索工具选型" not in catalog


async def test_seed_bodies_update_when_the_code_changes(client):
    """种子跟代码走：正文变了重新种就该更新，落后的种子只会误导模型。"""
    from sqlalchemy import select

    from app.models.agent_skill import AgentSkill
    from app.services import builtin_agent_skills as seeds

    async with get_sessionmaker()() as session:
        await seeds.ensure_builtin_agent_skills(session)
        row = await session.scalar(
            select(AgentSkill).where(
                AgentSkill.slug == "citation-hygiene", AgentSkill.owner_id.is_(None)
            )
        )
        row.body = "过时的旧正文"
        await session.commit()

    async with get_sessionmaker()() as session:
        await seeds.ensure_builtin_agent_skills(session)
        row = await session.scalar(
            select(AgentSkill).where(
                AgentSkill.slug == "citation-hygiene", AgentSkill.owner_id.is_(None)
            )
        )
    assert "过时的旧正文" not in row.body
    assert "引用纪律" in row.body


async def test_skill_api_import_list_delete(client, monkeypatch):
    """管理 API 的最小闭环：导入 → 列表可见 → 删除；内置的删不掉。"""
    import os

    from app.core.config import get_settings

    monkeypatch.setenv("POLARIS_CHAT_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    try:
        token = await register_and_login(client, email="skill-api@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        md = SKILL_MD.replace("literature-triage", "my-own-skill")
        resp = await client.post(
            "/api/chat/skills",
            json={"skill_md": md, "files": {"note.md": "备注"}},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["slug"] == "my-own-skill"

        listed = (await client.get("/api/chat/skills", headers=headers)).json()
        mine = [s for s in listed if s["slug"] == "my-own-skill"]
        assert mine and mine[0]["is_builtin"] is False

        # 坏 frontmatter 422 并带原因
        bad = await client.post(
            "/api/chat/skills", json={"skill_md": "没有 frontmatter"}, headers=headers
        )
        assert bad.status_code == 422

        assert (
            await client.delete("/api/chat/skills/my-own-skill", headers=headers)
        ).status_code == 204
        # 内置的删不掉
        from app.core.db import get_sessionmaker as gsm
        from app.services.builtin_agent_skills import ensure_builtin_agent_skills

        async with gsm()() as session:
            await ensure_builtin_agent_skills(session)
        assert (
            await client.delete("/api/chat/skills/citation-hygiene", headers=headers)
        ).status_code == 404
    finally:
        os.environ.pop("POLARIS_CHAT_AGENT_ENABLED", None)
        get_settings.cache_clear()
