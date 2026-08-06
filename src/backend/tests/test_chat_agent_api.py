"""全局助手的 SSE 端点：帧序列、开关、鉴权、落库。

老的六个对话端点一行不动——这是新路径。助手成熟之前两套并存，出问题关掉开关就退回去。
"""

import json
import os
import uuid

import pytest

from app.core.db import get_sessionmaker
from tests.conftest import register_and_login


@pytest.fixture
def agent_on(monkeypatch):
    """打开助手开关（默认关：它每轮重发历史与工具 schema，成本不是一个量级）。

    get_settings 是 lru_cache 的，光改环境变量不生效——两头都要清缓存。
    """
    from app.core.config import get_settings

    monkeypatch.setenv("POLARIS_CHAT_AGENT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    os.environ.pop("POLARIS_CHAT_AGENT_ENABLED", None)
    get_settings.cache_clear()


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event is not None:
            events.append((event, data))
    return events


async def _headers(client, email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def test_the_endpoint_is_hidden_when_the_flag_is_off(client):
    """开关关着时端点直接 404——不是 403，别让它出现在能力探测里。"""
    headers = await _headers(client, "agent-off@example.com")
    resp = await client.post("/api/chat/conversations", json={}, headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "CHAT_AGENT_DISABLED"


async def test_a_turn_streams_meta_delta_done(client, agent_on):
    """一轮问答的最小帧序列：meta 开头、done 结尾，正文走 delta。

    ``delta`` 的形状与老端点一字不差，所以只认 sources/delta/done/error 的客户端
    也能工作，只是看不到工具卡片。
    """
    headers = await _headers(client, "agent-turn@example.com")
    resp = await client.post("/api/chat/conversations", json={}, headers=headers)
    assert resp.status_code == 201, resp.text
    conv_id = resp.json()["id"]

    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": "帮我看看这个方向"},
        headers=headers,
    ) as stream:
        assert stream.status_code == 200
        assert stream.headers["x-accel-buffering"] == "no", "不关 nginx 缓冲就不是真流式"
        body = (await stream.aread()).decode()

    events = _parse_sse(body)
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta"
    assert kinds[-1] == "done"
    assert "delta" in kinds
    assert events[0][1]["conversation_id"] == conv_id
    answer = "".join(d["text"] for e, d in events if e == "delta")
    assert answer.strip(), "总得说点什么"


async def test_the_answer_is_persisted_and_replayed_next_turn(client, agent_on):
    """回答落库，第二轮**不用前端传 history**也接得上。这是搬到服务端的全部意义。"""
    from sqlalchemy import select

    from app.models.conversation import ConversationMessage

    headers = await _headers(client, "agent-persist@example.com")
    conv_id = (
        await client.post("/api/chat/conversations", json={}, headers=headers)
    ).json()["id"]

    for question in ("第一个问题", "第二个问题"):
        async with client.stream(
            "POST",
            f"/api/chat/conversations/{conv_id}/turn",
            json={"question": question},
            headers=headers,
        ) as stream:
            await stream.aread()

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(ConversationMessage)
                    .where(ConversationMessage.conversation_id == uuid.UUID(conv_id))
                    .order_by(ConversationMessage.seq)
                )
            )
            .scalars()
            .all()
        )

    roles = [r.role for r in rows]
    assert roles == ["user", "assistant", "user", "assistant"], roles
    assert rows[0].text == "第一个问题"
    assert rows[1].text.strip(), "assistant 的回答也要落库"

    # 消息接口能取到完整内容（SSE 里的 preview 是截断的）
    resp = await client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 4


async def test_someone_elses_conversation_is_not_readable(client, agent_on):
    """别人的会话既读不到消息，也不能往里发。"""
    owner = await _headers(client, "agent-owner@example.com")
    other = await _headers(client, "agent-other@example.com")
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=owner)).json()["id"]

    resp = await client.get(f"/api/chat/conversations/{conv_id}/messages", headers=other)
    assert resp.status_code == 404

    resp = await client.get("/api/chat/conversations", headers=other)
    assert conv_id not in {c["id"] for c in resp.json()}


async def test_conversations_are_listed_newest_first(client, agent_on):
    headers = await _headers(client, "agent-list@example.com")
    ids = []
    for _ in range(2):
        conv_id = (
            await client.post("/api/chat/conversations", json={}, headers=headers)
        ).json()["id"]
        ids.append(conv_id)
        async with client.stream(
            "POST",
            f"/api/chat/conversations/{conv_id}/turn",
            json={"question": f"问题 {conv_id[:4]}"},
            headers=headers,
        ) as stream:
            await stream.aread()

    listed = (await client.get("/api/chat/conversations", headers=headers)).json()
    assert [c["id"] for c in listed][:2] == ids[::-1], "最近说过话的排在前面"
    assert listed[0]["title"], "标题取首条用户消息"


async def test_an_unknown_scope_is_rejected(client, agent_on):
    headers = await _headers(client, "agent-scope@example.com")
    resp = await client.post(
        "/api/chat/conversations", json={"scope_kind": "made-up"}, headers=headers
    )
    assert resp.status_code == 422


# ---- 作用域解析：真工具、真检索，不用替身 ----
#
# 此前的两层测试都绕开了这条路径：循环测试用自己注册的假工具（不看 project_id），
# 端点测试用不发工具调用的 FakeProvider。于是 project_id 兜随机 UUID 的 bug 安然
# 通过了全部测试——助手调真工具永远查到零条，然后一本正经说「没查到」。
# 这里用 POLARIS_FAKE_TOOL 标记驱动**真的 search_papers**，检索**真的落库论文**。


async def _project_with_paper(client, headers, title: str) -> str:
    from tests.conftest import add_paper

    resp = await client.post(
        "/api/projects", json={"name": f"proj-{title[:8]}", "statement": "agent"}, headers=headers
    )
    project_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title=title,
            abstract=f"{title} abstract",
            tldr="一句话",
            year=2026,
            status="included",
        )
        await session.commit()
    return project_id


async def test_the_assistant_actually_finds_papers_in_the_resolved_project(client, agent_on):
    """全局会话 + 用户只有一个课题：自动解析作用域，真工具查得到真论文。"""
    headers = await _headers(client, "agent-scope-real@example.com")
    await _project_with_paper(client, headers, "Tree Search Planning Methods")

    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]
    marker = 'POLARIS_FAKE_TOOL:search_papers:{"query": "Tree Search", "mode": "keyword"}'
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": f"查一下 planning\n{marker}"},
        headers=headers,
    ) as stream:
        assert stream.status_code == 200
        body = (await stream.aread()).decode()

    events = _parse_sse(body)
    results = [d for e, d in events if e == "tool_result"]
    assert results, f"没有工具结果帧：{[e for e, _ in events]}"
    assert results[0]["ok"] is True
    assert "Tree Search Planning Methods" in results[0]["preview"], (
        "真工具必须查到真论文。查到零条说明作用域又丢了"
        f"——此前这里兜的是随机 UUID：{results[0]}"
    )


async def test_several_projects_no_longer_block_the_turn(client, agent_on):
    """有多个课题也不用先选一个：PolarisBuddy 是全局助手，按「看得见的库」检索。

    以前这里 409 PROJECT_REQUIRED，逼用户替一个纯内部的数据结构做选择题——而他想问
    的问题往往正好跨库。越权由可见性挡住，不是靠「只给一个课题」。
    """
    headers = await _headers(client, "agent-scope-many@example.com")
    for name in ("proj-a", "proj-b"):
        await client.post(
            "/api/projects", json={"name": name, "statement": "x"}, headers=headers
        )

    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": "hi"},
        headers=headers,
    ) as stream:
        assert stream.status_code == 200
        body = (await stream.aread()).decode()
    assert "PROJECT_REQUIRED" not in body


async def test_the_assistant_searches_across_libraries_without_a_project(client, agent_on):
    """不指定课题时，检索范围是**可见的全部库**——包括不属于任何课题的库。

    这条是「全局助手」的核心断言：以前论文只有挂在课题关联库上才搜得到。
    """
    from tests.conftest import add_paper

    headers = await _headers(client, "agent-global-scope@example.com")
    async with get_sessionmaker()() as session:
        from app.models.library_direction import DirectionLibrary, LibraryPaper
        from app.models.paper import new_paper

        lib = DirectionLibrary(name="公共库", definition={"statement": "x"}, is_public=True)
        session.add(lib)
        await session.flush()
        paper = new_paper(
            title="Global Scope Retrieval Works",
            abstract="a paper in a library that belongs to no project",
        )
        session.add(paper)
        await session.flush()
        session.add(
            LibraryPaper(library_id=lib.id, paper_id=paper.id, status="included")
        )
        await session.commit()
    assert add_paper is not None  # 用不上，但保持与其它作用域测试同一套脚手架

    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]
    marker = 'POLARIS_FAKE_TOOL:search_papers:{"query": "Global Scope", "mode": "keyword"}'
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": f"查一下\n{marker}"},
        headers=headers,
    ) as stream:
        body = (await stream.aread()).decode()

    results = [d for e, d in _parse_sse(body) if e == "tool_result"]
    assert results, f"没有工具结果帧：{body[:300]}"
    assert "Global Scope Retrieval Works" in json.dumps(results, ensure_ascii=False), (
        f"没搜到不属于任何课题的库里的论文：{results}"
    )


async def test_someone_elses_project_id_is_rejected_everywhere(client, agent_on):
    """带别人的 project_id 一律 404——建会话和跑轮次两个口都要挡。

    这是比随机 UUID 更严重的洞：不挡的话，助手会拿着别人的课题作用域去检索
    **别人的语料**。MCP 那侧一直有成员校验，这个端点此前漏了。
    """
    owner = await _headers(client, "agent-authz-owner@example.com")
    intruder = await _headers(client, "agent-authz-intruder@example.com")
    project_id = await _project_with_paper(client, owner, "Secret Corpus Paper")

    # 建会话时就挡
    resp = await client.post(
        "/api/chat/conversations", json={"project_id": project_id}, headers=intruder
    )
    assert resp.status_code == 404

    # 跑轮次时显式传也挡
    conv_id = (
        await client.post("/api/chat/conversations", json={}, headers=intruder)
    ).json()["id"]
    resp = await client.post(
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": "hi", "project_id": project_id},
        headers=intruder,
    )
    assert resp.status_code == 404


async def test_the_resolved_project_sticks_to_the_conversation(client, agent_on):
    """解析出的课题存回会话：第二轮不用再传，也不再走解析。"""
    headers = await _headers(client, "agent-scope-stick@example.com")
    project_id = await _project_with_paper(client, headers, "Sticky Scope Paper")

    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": "第一轮", "project_id": project_id},
        headers=headers,
    ) as stream:
        await stream.aread()

    from app.models.conversation import Conversation

    async with get_sessionmaker()() as session:
        conv = await session.get(Conversation, uuid.UUID(conv_id))
        assert str(conv.project_id) == project_id, "第一轮解析出的课题要存回会话"


async def test_capabilities_reports_tools_skills_and_mcp(client, agent_on):
    """「它现在到底能干什么」要有一个能问的地方。

    此前只能靠读代码：工具白名单在常量里，技能目录只出现在提示词里，对外 MCP 暴露的
    是不是同一批也没人说得清。
    """
    from app.services.builtin_agent_skills import ensure_builtin_agent_skills

    headers = await _headers(client, "caps@example.com")
    async with get_sessionmaker()() as session:
        await ensure_builtin_agent_skills(session)  # 生产上由启动钩子种入
    body = (await client.get("/api/chat/capabilities", headers=headers)).json()

    names = {t["name"] for t in body["tools"]}
    assert "search_papers" in names and "update_plan" in names
    assert all(t["read_only"] for t in body["tools"]), "只读期不该有写工具混进来"
    # 内置技能种子在启动时就位
    assert body["skills"], "技能目录是空的"
    assert {s["slug"] for s in body["skills"]} & {"polaris-search-playbook", "paper-deep-read"}
    # MCP：如实说明 Polaris 是服务端，不编一个「已连接的服务器」列表
    assert body["mcp"]["role"] == "server"
    assert body["mcp"]["exposed_tools"] > 0


async def test_figure_tools_carry_a_ref_so_images_never_ride_the_stream(client, agent_on):
    """图片在对话流里只发出处，不发字节。

    base64 一张图几百 KB，几张就能把 SSE 连接和浏览器一起灌爆；而平台本来就有带鉴权的
    取图端点。
    """
    from app.tools.figures import _figure_ref

    ref = _figure_ref(uuid.uuid4(), 3)
    assert ref["kind"] == "paper_figure" and ref["index"] == 3
    assert uuid.UUID(ref["paper_id"])  # 可解析回 uuid

    from app.agents.chat.events import ToolResultEvent

    ev = ToolResultEvent(
        id="c", name="get_paper_figure", ok=True, summary="", preview="", duration_ms=1
    )
    assert ev.image_refs == (), "默认不带图，字段存在即可"


async def test_conversations_can_be_renamed_and_deleted(client, agent_on):
    """标题取自第一句提问，而那句话常常并不概括整场对话——所以要能改，也要能删。"""
    headers = await _headers(client, "conv-crud@example.com")
    conv = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()

    resp = await client.patch(
        f"/api/chat/conversations/{conv['id']}",
        json={"title": "  planning 相关的那次  "},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "planning 相关的那次"  # 首尾空白去掉

    assert (
        await client.delete(f"/api/chat/conversations/{conv['id']}", headers=headers)
    ).status_code == 204
    rows = (await client.get("/api/chat/conversations", headers=headers)).json()
    assert conv["id"] not in {r["id"] for r in rows}


async def test_someone_elses_conversation_is_404_not_403(client, agent_on):
    """别人的对话一律 404。403 等于承认「这个 id 存在」——对话标题里常带着研究方向。"""
    owner = await _headers(client, "conv-owner@example.com")
    intruder = await _headers(client, "conv-intruder@example.com")
    conv = (await client.post("/api/chat/conversations", json={}, headers=owner)).json()

    assert (
        await client.delete(f"/api/chat/conversations/{conv['id']}", headers=intruder)
    ).status_code == 404
    assert (
        await client.patch(
            f"/api/chat/conversations/{conv['id']}", json={"title": "x"}, headers=intruder
        )
    ).status_code == 404
    # 而且真的没被删掉
    rows = (await client.get("/api/chat/conversations", headers=owner)).json()
    assert conv["id"] in {r["id"] for r in rows}


async def test_deleting_a_conversation_takes_its_messages(client, agent_on):
    """消息随对话级联删除，不留孤儿行。"""
    from sqlalchemy import func, select

    from app.models.conversation import ConversationMessage

    headers = await _headers(client, "conv-cascade@example.com")
    conv = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv['id']}/turn",
        json={"question": "留一条消息"},
        headers=headers,
    ) as stream:
        await stream.aread()

    async with get_sessionmaker()() as session:
        before = await session.scalar(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == uuid.UUID(conv["id"]))
        )
    assert before and before > 0

    await client.delete(f"/api/chat/conversations/{conv['id']}", headers=headers)
    async with get_sessionmaker()() as session:
        after = await session.scalar(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == uuid.UUID(conv["id"]))
        )
    assert after == 0


async def test_a_turn_persists_its_whole_timeline_not_just_the_answer(client, agent_on):
    """切到别的对话再切回来，思考过程与工具调用还得在。

    此前每轮只落库最终文本，于是回放只剩一段结论——而那段结论之所以可信，正是因为
    下面那些调用。
    """
    headers = await _headers(client, "timeline@example.com")
    await _project_with_paper(client, headers, "Timeline Persistence Paper")
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]

    marker = 'POLARIS_FAKE_TOOL:search_papers:{"query": "Timeline", "mode": "keyword"}'
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": f"查一下\n{marker}"},
        headers=headers,
    ) as stream:
        await stream.aread()

    messages = (
        await client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers)
    ).json()
    # 一轮会落成几条消息：assistant（正文/思考/调用）与携带工具结果的那条分开。
    # 塞进一条的话，回放到 OpenAI 形状时工具结果就成了没有 tool_calls 前置的孤儿。
    turn = [m for m in messages if m["role"] == "assistant" or m["kind"] == "tool_results"]
    assert turn, "助手消息没落库"
    kinds = [b.get("kind") for m in turn for b in (m["blocks"] or [])]
    assert "tool_use" in kinds, f"工具调用没落库：{kinds}"
    assert "tool_result" in kinds, f"工具结果没落库：{kinds}"
    assert "text" in kinds

    # 工具结果存的是摘要而不是完整 payload——回放只需要「调了什么、拿到什么样的东西」
    results = [
        json.loads(b["content"])
        for m in turn
        for b in (m["blocks"] or [])
        if b.get("kind") == "tool_result"
    ]
    assert results and {"summary", "ok"} <= set(results[0])


async def test_a_tool_that_needs_a_topic_fails_inside_the_loop_not_on_the_wire(client, agent_on):
    """需要课题的工具在没有课题时，必须变成**循环内的一个失败结果**，而不是把流打断。

    线上现象是两张卡片停在「调用中…」然后一条 ⚠️ network error——那说明异常逃出了
    循环、把 SSE 连接带走了。权限/参数一类的错误应该回喂给模型，由它决定下一步；
    连接断掉的话，用户看到的是「网络坏了」，而其实是工具不适用。
    """
    headers = await _headers(client, "toolerr@example.com")
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]

    marker = "POLARIS_FAKE_TOOL:knowledge_graph:{}"
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": f"看看图谱\n{marker}"},
        headers=headers,
    ) as stream:
        assert stream.status_code == 200
        body = (await stream.aread()).decode()

    events = _parse_sse(body)
    names = [e for e, _ in events]
    assert "done" in names, f"流被打断了，没有 done：{names}"
    results = [d for e, d in events if e == "tool_result"]
    assert results, f"没有工具结果帧：{names}"
    assert results[0]["ok"] is False
    assert "课题" in results[0]["summary"], results[0]


async def test_every_tool_module_resolves_scope_the_same_way(client, agent_on):
    """所有工具都必须走同一条范围解析。

    #269 放开检索范围时漏改了三个模块（figures / external / libraries），它们仍按
    课题查——于是全局模式下取图必然报「库内不存在该论文」，而论文和图都好端端在那儿。
    这条把「没有工具再自己按 project_id 判成员资格」钉住：漏一个模块，症状是某类工具
    在全局对话里整体失灵，而其它工具一切正常，很难联想到是范围解析没统一。
    """
    import pathlib

    tools_dir = pathlib.Path(__file__).resolve().parent.parent / "app" / "tools"
    offenders = []
    for path in tools_dir.glob("*.py"):
        if path.name == "scope.py":  # 范围解析自己就住在这儿
            continue
        text = path.read_text(encoding="utf-8")
        if "membership_for_project" in text or "get_source_library_ids" in text:
            offenders.append(path.name)
    assert offenders == [], f"这些模块还在按课题解析范围：{offenders}"


async def test_the_conversation_gets_a_short_name_after_the_first_turn(client, agent_on):
    """对话要有个短名字（≤15 字），而不是把第一句提问整段截下来当标题。"""
    from app.services.conversations import TITLE_MAX_CHARS

    headers = await _headers(client, "title@example.com")
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": "帮我把这个方向的相关工作梳理一遍，重点看最近两年的进展"},
        headers=headers,
    ) as stream:
        await stream.aread()

    rows = (await client.get("/api/chat/conversations", headers=headers)).json()
    title = next(r["title"] for r in rows if r["id"] == conv_id)
    assert title and len(title) <= TITLE_MAX_CHARS


async def test_renaming_survives_the_next_turn(client, agent_on):
    """改过名的对话不该被下一轮悄悄改回去。"""
    headers = await _headers(client, "title-keep@example.com")
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": "第一句"},
        headers=headers,
    ) as stream:
        await stream.aread()

    await client.patch(
        f"/api/chat/conversations/{conv_id}", json={"title": "我自己起的名字"}, headers=headers
    )
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": "第二句"},
        headers=headers,
    ) as stream:
        await stream.aread()

    rows = (await client.get("/api/chat/conversations", headers=headers)).json()
    assert next(r["title"] for r in rows if r["id"] == conv_id) == "我自己起的名字"


async def test_plan_mode_tells_the_model_to_propose_before_acting(client, agent_on):
    """计划模式把「先出方案等人点头」写进这一轮的指令；默认模式一个字都不加。"""
    from app.agents.chat.prompt import mode_instructions

    assert mode_instructions("chat") == ""
    assert mode_instructions("goal", goal="") == ""
    plan = mode_instructions("plan")
    assert "只做调研和规划" in plan and "update_plan" in plan
    goal = mode_instructions("goal", goal="把 CUA 这条线读透")
    assert "把 CUA 这条线读透" in goal


async def test_replayed_history_never_orphans_a_tool_message(client, agent_on):
    """回放出来的历史必须能原样喂回 OpenAI 形状的中转。

    线上撞到的 400：「Messages with role 'tool' must be a response to a preceding
    message with 'tool_calls'」——因为整轮曾被塞进**一条** assistant 消息，工具结果
    和调用挤在一起，展开后 tool 消息前面没有带 tool_calls 的 assistant。
    """
    from app.core.llm.openai_compat import _messages_payload
    from app.services import conversations as store

    headers = await _headers(client, "orphan@example.com")
    await _project_with_paper(client, headers, "Orphan Tool Message Paper")
    conv_id = (await client.post("/api/chat/conversations", json={}, headers=headers)).json()["id"]

    marker = 'POLARIS_FAKE_TOOL:search_papers:{"query": "Orphan", "mode": "keyword"}'
    async with client.stream(
        "POST",
        f"/api/chat/conversations/{conv_id}/turn",
        json={"question": f"查一下\n{marker}"},
        headers=headers,
    ) as stream:
        await stream.aread()

    async with get_sessionmaker()() as session:
        history = await store.replay(session, conversation_id=uuid.UUID(conv_id))

    payload = _messages_payload(history)
    for index, entry in enumerate(payload):
        if entry["role"] != "tool":
            continue
        assert index > 0, "tool 消息不能是第一条"
        prev = payload[index - 1]
        assert prev.get("tool_calls") or prev["role"] == "tool", (
            f"第 {index} 条 tool 消息前面没有带 tool_calls 的 assistant：{payload[:index + 1]}"
        )


def test_a_single_message_holding_both_call_and_result_still_translates():
    """存量历史就是这个形状（一条消息里既有调用又有结果），必须还能回放。"""
    from app.core.llm.base import Message, TextBlock, ToolResultBlock, ToolUseBlock
    from app.core.llm.openai_compat import _messages_payload

    payload = _messages_payload(
        [
            Message(role="user", content="查一下"),
            Message(
                role="assistant",
                content=[
                    TextBlock("我去查"),
                    ToolUseBlock("call-1", "search_papers", {"query": "x"}),
                    ToolResultBlock("call-1", '{"results": []}'),
                ],
            ),
        ]
    )
    roles = [e["role"] for e in payload]
    assert roles == ["user", "assistant", "tool"]
    assert payload[1]["tool_calls"][0]["id"] == "call-1"
    assert payload[2]["tool_call_id"] == "call-1"
