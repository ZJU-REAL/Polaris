"""对话的服务端持久化。

此前历史只在浏览器 localStorage 里，每次请求前端把最近 10 轮原样 POST 回来。这对
一问一答够用，对 agent 不够：一轮里模型可能调好几次工具、中途会断线、事后要能审计
"它到底查了什么"。
"""

import uuid

import pytest

from app.core.db import get_sessionmaker
from app.core.llm.base import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.services import conversations as store
from tests.conftest import register_and_login


async def _user_id(client, email: str) -> uuid.UUID:
    from sqlalchemy import select

    from app.models.user import User

    await register_and_login(client, email=email)
    async with get_sessionmaker()() as session:
        return (await session.execute(select(User.id).where(User.email == email))).scalar_one()


async def test_a_tool_using_round_survives_a_round_trip(client):
    """带工具调用的一轮存下去、读回来，形状不丢。

    这是整件事的核心契约：第二轮**不用前端传 history**，服务端自己重放得出上下文。
    """
    user_id = await _user_id(client, "conv-store@example.com")
    async with get_sessionmaker()() as session:
        conv = await store.get_or_create(session, user_id=user_id, scope_kind="global")
        await store.append_message(session, conversation=conv, role="user", text="查一下 planning")
        await store.append_message(
            session,
            conversation=conv,
            role="assistant",
            blocks=[
                ThinkingBlock("先检索", "sig-1"),
                TextBlock("我来查一下"),
                ToolUseBlock("c1", "search_papers", {"query": "planning"}),
            ],
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )
        await store.append_message(
            session,
            conversation=conv,
            role="user",
            blocks=[ToolResultBlock("c1", '{"hits": 3}')],
        )
        await session.commit()
        conv_id = conv.id

    async with get_sessionmaker()() as session:
        history = await store.replay(session, conversation_id=conv_id)

    assert [m.role for m in history] == ["user", "assistant", "user"]
    assistant_blocks = history[1].content
    assert isinstance(assistant_blocks, list)
    kinds = [type(b).__name__ for b in assistant_blocks]
    assert kinds == ["ThinkingBlock", "TextBlock", "ToolUseBlock"]
    # 签名必须活着：缺了它这轮回放会被 Anthropic 拒掉
    assert assistant_blocks[0].signature == "sig-1"
    call = assistant_blocks[2]
    assert call.name == "search_papers" and call.input == {"query": "planning"}
    assert isinstance(history[2].content[0], ToolResultBlock)


async def test_images_are_not_persisted_as_base64(client):
    """工具结果里的图片不落 base64，只留一句占位。

    一次取图工具能返回好几张，二十轮下来 JSON 就爆了；而且存了也没用——回放时把
    几十张图喂回去，token 立刻见底。模型对"历史里少张图"的容忍度高得多。
    """
    user_id = await _user_id(client, "conv-img@example.com")
    async with get_sessionmaker()() as session:
        conv = await store.get_or_create(session, user_id=user_id, scope_kind="global")
        row = await store.append_message(
            session,
            conversation=conv,
            role="user",
            blocks=[
                ToolResultBlock(
                    "c1", '{"figures": 2}', images=(ImageBlock(b"\x89PNG" * 500), ImageBlock(b"x"))
                )
            ],
        )
        await session.commit()
        raw, conv_id = row.blocks, conv.id

    assert raw[0]["images"] == 2, "只记数量"
    assert "PNG" not in str(raw), "绝不能把图片字节写进 JSON"

    async with get_sessionmaker()() as session:
        history = await store.replay(session, conversation_id=conv_id)
    assert "2 张图片" in history[0].content[0].content


async def test_interrupted_messages_stay_in_the_table_but_out_of_the_replay(client):
    """中断/出错的消息留在库里给人看，但不喂回给模型。

    半截的 assistant 轮会让模型以为自己说过那些话，接着往下编。
    """
    user_id = await _user_id(client, "conv-interrupt@example.com")
    async with get_sessionmaker()() as session:
        conv = await store.get_or_create(session, user_id=user_id, scope_kind="global")
        await store.append_message(session, conversation=conv, role="user", text="问题")
        partial = await store.append_message(
            session,
            conversation=conv,
            role="assistant",
            blocks=[TextBlock("说到一半就")],
            status="streaming",
        )
        await store.finish_message(session, message=partial, status="interrupted")
        await session.commit()
        conv_id = conv.id

    async with get_sessionmaker()() as session:
        history = await store.replay(session, conversation_id=conv_id)
        from sqlalchemy import func, select

        from app.models.conversation import ConversationMessage

        total = await session.scalar(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == conv_id)
        )

    assert [m.role for m in history] == ["user"], "中断那条不该进重放"
    assert total == 2, "但它必须留在库里"


async def test_usage_accumulates_on_the_conversation(client):
    """每轮的 token 累加到会话上——"这场对话花了多少"要能一眼看到。"""
    user_id = await _user_id(client, "conv-usage@example.com")
    async with get_sessionmaker()() as session:
        conv = await store.get_or_create(session, user_id=user_id, scope_kind="global")
        for _ in range(3):
            await store.append_message(
                session,
                conversation=conv,
                role="assistant",
                text="答",
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            )
        await session.commit()
        usage = dict(conv.usage)

    assert usage["prompt_tokens"] == 30
    assert usage["completion_tokens"] == 15
    assert usage["total_tokens"] == 45


async def test_title_comes_from_the_first_user_message(client):
    user_id = await _user_id(client, "conv-title@example.com")
    async with get_sessionmaker()() as session:
        conv = await store.get_or_create(session, user_id=user_id, scope_kind="global")
        await store.append_message(
            session, conversation=conv, role="user", text="  最近有哪些值得关注的新论文？  "
        )
        await store.append_message(session, conversation=conv, role="user", text="第二个问题")
        await session.commit()
        title = conv.title

    assert title == "最近有哪些值得关注的新论文？", "取首条、去空白、不被第二条覆盖"


async def test_someone_elses_conversation_is_never_continued(client):
    """带着别人的 conversation_id 来，不能把那场对话续下去。"""
    owner = await _user_id(client, "conv-owner@example.com")
    other = await _user_id(client, "conv-other@example.com")
    async with get_sessionmaker()() as session:
        conv = await store.get_or_create(session, user_id=owner, scope_kind="global")
        await session.commit()
        owned_id = conv.id

    async with get_sessionmaker()() as session:
        got = await store.get_or_create(
            session, user_id=other, scope_kind="global", conversation_id=owned_id
        )
        await session.commit()

    assert got.id != owned_id, "归属对不上就该另开一条，而不是接管"
    assert got.user_id == other


@pytest.mark.parametrize(
    "raw",
    [
        [{"kind": "who-knows", "text": "x"}],  # 将来的新块类型
        [{"kind": "text"}],  # 缺字段
        None,  # 空
    ],
)
def test_unknown_blocks_never_raise(raw):
    """存量数据与将来的新块类型都不该炸在反序列化这一步。"""
    assert isinstance(store.blocks_from_json(raw), list)


def test_blocks_round_trip_is_provider_neutral():
    """块存的是中立形状，不是某一家的 payload——管理端随时能改模型路由。"""
    blocks = [
        TextBlock("t"),
        ThinkingBlock("think", "sig"),
        ToolUseBlock("c1", "search_papers", {"q": 1}),
        ToolResultBlock("c1", "{}", is_error=True),
    ]
    raw = store.blocks_to_json(blocks)
    assert [b["kind"] for b in raw] == ["text", "thinking", "tool_use", "tool_result"]
    back = store.blocks_from_json(raw)
    assert [type(b).__name__ for b in back] == [type(b).__name__ for b in blocks]
    assert back[3].is_error is True
    # 拍平成文本时工具调用不能凭空消失
    assert "search_papers" in Message(role="assistant", content=back).text


# ---- 回放预算：历史不能无界增长 ----
#
# run_turn 此前不带任何上界地回放全部历史：会话越长每轮重发越多，token 成本平方
# 增长，最终撑爆上下文窗口。P2 加的 model_routes.context_window 列在此之前没人读。


def _turn(question: str, result_chars: int = 3000) -> list:
    from app.core.llm.base import Message, TextBlock, ToolResultBlock, ToolUseBlock
    from app.services.conversations import trim_history  # noqa: F401 — 确保可导入

    return [
        Message(role="user", content=question),
        Message(
            role="assistant",
            content=[TextBlock("我查一下"), ToolUseBlock("c1", "search_papers", {"q": question})],
        ),
        Message(role="user", content=[ToolResultBlock("c1", "x" * result_chars)]),
        Message(role="assistant", content=f"关于{question}的回答"),
    ]


def test_trim_cuts_only_at_turn_boundaries():
    """裁剪永不把 tool_use 和它的 tool_result 拆散——孤儿 tool_use 会被 Anthropic 400。"""
    from app.core.llm.base import Message, ToolResultBlock, ToolUseBlock
    from app.services.conversations import trim_history

    history: list[Message] = []
    for i in range(8):
        history += _turn(f"问题{i}")

    trimmed = trim_history(history, budget_chars=8000)
    assert trimmed, "至少保留最新一轮"
    assert trimmed[0].role == "user", "开头必须是一个用户提问"

    # 逐条核对：每个 tool_use 后面必须跟着装它 result 的消息
    pending: set[str] = set()
    for msg in trimmed:
        blocks = msg.content if isinstance(msg.content, list) else []
        for b in blocks:
            if isinstance(b, ToolUseBlock):
                pending.add(b.id)
            elif isinstance(b, ToolResultBlock):
                pending.discard(b.tool_use_id)
    assert not pending, f"有孤儿 tool_use：{pending}"


def test_trim_keeps_the_newest_and_drops_the_oldest():
    from app.core.llm.base import Message
    from app.services.conversations import trim_history

    history: list[Message] = []
    for i in range(10):
        history += _turn(f"问题{i}")

    # 预算要算上「旧轮次的工具结果会被压到 ~250 字符」：10 个轮次里最新一轮 ~3000，
    # 旧轮次每轮 ~270——4000 的预算装得下最新 + 约 3 个旧轮次，更早的必须淘汰
    trimmed = trim_history(history, budget_chars=4_000)
    text = "\n".join(m.text for m in trimmed)
    assert "问题9" in text, "最新的必须在"
    assert "问题0" not in text, "最旧的该被裁掉"
    assert len(trimmed) < len(history)


def test_the_latest_turn_survives_even_when_it_alone_exceeds_the_budget():
    """预算再小也不能把最新一轮裁没——那等于把用户刚说的话扔了。"""
    from app.services.conversations import trim_history

    history = _turn("唯一的问题", result_chars=50_000)
    trimmed = trim_history(history, budget_chars=100)
    assert any("唯一的问题" in m.text for m in trimmed)


def test_old_turns_have_their_tool_results_shrunk_but_the_latest_keeps_full_text():
    """保留的旧轮次里工具结果压成一行；最新一轮保留原文（追问经常要引用它）。"""
    from app.core.llm.base import Message, ToolResultBlock
    from app.services.conversations import trim_history

    history: list[Message] = []
    for i in range(3):
        history += _turn(f"问题{i}", result_chars=3000)

    trimmed = trim_history(history, budget_chars=1_000_000)  # 预算充足，全部保留
    results = [
        b
        for m in trimmed
        for b in (m.content if isinstance(m.content, list) else [])
        if isinstance(b, ToolResultBlock)
    ]
    assert len(results) == 3
    assert len(results[0].content) < 300, "旧轮次的结果该被压掉"
    assert "已截断" in results[0].content
    assert len(results[-1].content) >= 3000, "最新一轮的结果保留原文"


def test_plain_conversations_without_tools_trim_cleanly():
    """纯文本会话（没有工具块）也照常按轮次裁。"""
    from app.core.llm.base import Message
    from app.services.conversations import trim_history

    history: list[Message] = []
    for i in range(20):
        history.append(Message(role="user", content=f"问题{i}" * 100))
        history.append(Message(role="assistant", content=f"回答{i}" * 100))

    trimmed = trim_history(history, budget_chars=5000)
    assert trimmed[0].role == "user"
    assert len(trimmed) < 40
    assert "问题19" in trimmed[-2].text
