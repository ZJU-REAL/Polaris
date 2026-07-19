"""FastAPI 应用工厂。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.router import api_router
from app.api.ws import router as ws_router
from app.core.config import get_settings
from app.core.db import create_all, dispose_engine, get_sessionmaker
from app.core.redis import close_redis
from app.mcp import mcp_router
from app.services.crdt_rooms import reset_crdt_rooms
from app.services.crdt_stream import get_crdt_stream_subscriber, stop_crdt_stream_subscriber
from app.services.skills import ensure_builtin_skills

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # 仅 sqlite（无 docker 的本地 dev）在启动时建表；postgres 走 alembic migration
    if settings.is_sqlite:
        await create_all()
    # 内置技能种子（按 slug 幂等）；失败不阻断启动（如 migration 未跑）
    try:
        async with get_sessionmaker()() as session:
            await ensure_builtin_skills(session)
    except Exception:  # noqa: BLE001
        logger.warning("builtin skill seeding failed (migrations pending?)", exc_info=True)
    # AI 起草流式镜像订阅（worker 发布 → 写活跃 CRDT 房间；连不上 redis 自动放弃）
    get_crdt_stream_subscriber().start()
    yield
    await stop_crdt_stream_subscriber()
    await reset_crdt_rooms()  # 关停 CRDT 房间服务器（先冲刷不了的防抖任务直接取消）
    await dispose_engine()
    await close_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Polaris",
        description="自动 AI 科研平台 backend",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        # TODO(部署): prod 收紧为前端域名白名单
        allow_origins=["*"] if settings.env == "dev" else [],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix="/api")
    # WS 不挂 /api 前缀：nginx 按 /ws 反代（Upgrade），见 docs/architecture.md §7
    app.include_router(ws_router)
    # MCP 只读工具服务：POST /mcp（Streamable HTTP，JSON-RPC 2.0），见 docs/api-mcp.md
    app.include_router(mcp_router)
    return app


app = create_app()
