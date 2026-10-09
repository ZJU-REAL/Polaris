"""助手对话接外部 agent 的胶水层（#836）：能选哪些 agent、工作目录、把 Polaris 的
工具借给它、会话号落库。

## 能选哪些

登记了且启用着的都能选（单用户本地应用，没有「别人」）。

## 把 Polaris 的工具借给 agent

agent 声明支持 HTTP MCP 时，开会话就把我们自己的 ``/mcp`` 交给它：令牌是这个用户的、
**只读**、一天过期，会话关掉时吊销。于是 Claude Code 在助手里也能检索这个人的文献库，
而且只能看他本来就看得到的东西。不走 stdio（``python -m app.mcp``）：那要把数据库
连接串放进 agent 能读到的环境里，等于把全平台数据交给它。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.chat.acp_loop import AcpChatLoop, AcpTurnRequest
from app.agents.chat.events import ChatEvent
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.models.acp_agent import AcpAgent
from app.models.user import User
from app.services.acp.client import AcpClient
from app.services.acp.pool import get_pool
from app.services.acp.registry import spec_from_row

logger = logging.getLogger("polaris.acp")

MCP_TOKEN_DAYS = 1


async def usable_agents(session: AsyncSession) -> list[AcpAgent]:
    """启用着的 agent，按登记先后。"""
    stmt = select(AcpAgent).where(AcpAgent.enabled.is_(True)).order_by(AcpAgent.created_at)
    return list((await session.execute(stmt)).scalars().all())


async def resolve_agent(session: AsyncSession, backend: str) -> AcpAgent | None:
    try:
        agent_id = uuid.UUID(backend)
    except ValueError:
        return None
    for row in await usable_agents(session):
        if row.id == agent_id:
            return row
    return None


async def agent_for_polaris_turn(session: AsyncSession, user: User) -> tuple[AcpAgent, str] | None:
    """「Polaris」大脑这一轮实际会落在哪个外部 agent 上，以及路由上填的模型
    （没有就 None，走原生循环）。

    看的是助手环节（agent）的路由：显式指向 agent，或没配模型 API、由 agent 接管。
    """
    from app.core.llm.router import LLMNotConfiguredError, get_llm_router

    try:
        _provider, route = await get_llm_router().resolve("agent")
    except (LLMNotConfiguredError, NotImplementedError):
        return None
    if route.acp is None:
        return None
    row = await session.get(AcpAgent, uuid.UUID(route.acp.agent_id))
    if row is None or not row.enabled:
        return None
    return row, route.model or ""


def live_session(conversation_id: uuid.UUID):  # noqa: ANN201 — LiveSession | None
    """这个对话此刻挂着的 agent 会话（没有就 None）。"""
    return get_pool().get(str(conversation_id))


def workdir_for(user_id: uuid.UUID, conversation_id: uuid.UUID) -> str:
    """每个对话一个工作目录：agent 的读写都被钉在这里面（见 client.contained_path）。"""
    return str(
        Path(get_settings().data_dir) / "acp-workspaces" / str(user_id) / str(conversation_id)
    )


def polaris_mcp_factory(
    user_id: uuid.UUID, server_port: int | None
) -> Callable[
    [AcpClient], Awaitable[tuple[list[dict[str, Any]], list[Callable[[], Awaitable[None]]]]]
]:
    async def factory(
        client: AcpClient,
    ) -> tuple[list[dict[str, Any]], list[Callable[[], Awaitable[None]]]]:
        if server_port is None or not client.info.mcp_http:
            return [], []
        from app.schemas.integration_token import IntegrationTokenCreate
        from app.services import integration_tokens

        async with get_sessionmaker()() as session:
            token, raw = await integration_tokens.create_token(
                session,
                user_id=user_id,
                data=IntegrationTokenCreate(
                    name=f"Agent session ({client.spec.name})"[:80],
                    scopes=["mcp:read"],
                    expires_in_days=MCP_TOKEN_DAYS,
                ),
            )
        token_id = token.id

        async def revoke() -> None:
            async with get_sessionmaker()() as session:
                row = await integration_tokens.get_owned_token(
                    session, token_id=token_id, user_id=user_id
                )
                if row is not None:
                    await integration_tokens.revoke_token(session, row)

        server = {
            "type": "http",
            "name": "polaris",
            "url": f"http://127.0.0.1:{server_port}/mcp",
            "headers": [{"name": "Authorization", "value": f"Bearer {raw}"}],
        }
        return [server], [revoke]

    return factory


async def run_turn(
    *,
    agent: AcpAgent,
    user: User,
    conversation_id: uuid.UUID,
    question: str,
    history: list[Message],
    page_context: str,
    resume_session_id: str | None,
    server_port: int | None,
    unseen: list[Message] | None = None,
    instructions: str = "",
    model: str = "",
) -> AsyncIterator[ChatEvent]:
    loop = AcpChatLoop(pool=get_pool(), mcp_factory=polaris_mcp_factory(user.id, server_port))
    req = AcpTurnRequest(
        conversation_id=conversation_id,
        question=question,
        agent_id=agent.id,
        agent_name=agent.name or agent.slug,
        spec=spec_from_row(agent),
        workdir=workdir_for(user.id, conversation_id),
        resume_session_id=resume_session_id,
        page_context=page_context,
        history=history,
        unseen=list(unseen or []),
        instructions=instructions,
        model=model,
    )
    agent_id = str(agent.id)
    try:
        async for ev in loop.run(req):
            yield ev
    finally:
        if loop.session_id:
            # 断线时这里处于取消态，不 shield 的话这次写库会被二次取消
            await asyncio.shield(
                _remember_session(conversation_id, user.id, agent_id, loop.session_id)
            )


async def _remember_session(
    conversation_id: uuid.UUID, user_id: uuid.UUID, agent_id: str, session_id: str
) -> None:
    """记下 agent 那边的会话号：进程被回收后，下一轮用它 session/load 续上。"""
    from app.services import conversations as store

    try:
        async with get_sessionmaker()() as session:
            conv = await store.get_owned(session, conversation_id=conversation_id, user_id=user_id)
            if conv is None:
                return
            settings = dict(conv.settings or {})
            if (
                settings.get("acp_session_id") == session_id
                and settings.get("acp_agent_id") == agent_id
            ):
                return
            settings["acp_session_id"] = session_id
            settings["acp_agent_id"] = agent_id
            conv.settings = settings
            await session.commit()
    except Exception:  # noqa: BLE001 — 记不下来只是下次续不上，不该打断这一轮
        logger.warning("记录 ACP 会话号失败", exc_info=True)


__all__ = ["polaris_mcp_factory", "resolve_agent", "run_turn", "usable_agents", "workdir_for"]
