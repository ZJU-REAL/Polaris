"""FastAPI 应用工厂。"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.auth import block_read_only_writes
from app.api.router import api_router
from app.api.ws import router as ws_router
from app.core.config import get_settings
from app.core.db import create_all, dispose_engine, get_sessionmaker
from app.core.llm.router import LLMNotConfiguredError
from app.core.redis import close_redis
from app.mcp import mcp_router
from app.services.builtin_agent_skills import ensure_builtin_agent_skills
from app.services.crdt_rooms import reset_crdt_rooms
from app.services.crdt_stream import get_crdt_stream_subscriber, stop_crdt_stream_subscriber
from app.services.skills import ensure_builtin_skills

logger = logging.getLogger(__name__)

# Electron 桌面客户端的固定 origin：页面由自定义 app:// scheme 加载（见 src/desktop）。
# 恒在 prod 白名单内且无需配置——网页无法伪造自定义 scheme 的 Origin 头，且这里
# allow_credentials=False + Bearer 鉴权没有 ambient authority，故对 web 攻击面零增量。
DESKTOP_ORIGIN = "app://polaris"


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
            await ensure_builtin_agent_skills(session)
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
        # dev 全放开；prod 只放行桌面客户端 + POLARIS_CORS_ORIGINS 里显式配置的前端域名。
        # web 生产是 nginx 同源反代，不经过这里。
        allow_origins=(
            ["*"] if settings.env == "dev" else [DESKTOP_ORIGIN, *settings.cors_origin_list]
        ),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        # 跨源下这些响应头默认对 JS 不可见。pdf.js 靠 Accept-Ranges / Content-Range /
        # Content-Length 判断服务端支不支持分段取，读不到就退回整包下载——桌面端
        # （app://polaris 是跨源）的 PDF 阅读会因此又变回「等整份下完」。
        expose_headers=["Accept-Ranges", "Content-Range", "Content-Length", "Content-Disposition"],
    )

    @app.exception_handler(LLMNotConfiguredError)
    async def _llm_not_configured(_request: Request, _exc: LLMNotConfiguredError) -> JSONResponse:
        # 未配置大模型：统一 503，前端提示到设置页配置，而不是 500 堆栈
        return JSONResponse(status_code=503, content={"detail": "LLM_NOT_CONFIGURED"})

    # 只读账号的写入闸门挂在路由器层：一处生效，新端点自动被覆盖，不会因为
    # 有人加了个 POST 忘记加守卫就漏出去。
    read_only_gate = [Depends(block_read_only_writes)]
    app.include_router(api_router, prefix="/api", dependencies=read_only_gate)
    # WS 不挂 /api 前缀：nginx 按 /ws 反代（Upgrade），见 docs/architecture.md §7
    # WS 走不到 HTTP 依赖，只读账号在 ws.py 里各自挡（协同房间是写，通知只是收）
    app.include_router(ws_router)
    # MCP 只读工具服务：POST /mcp（Streamable HTTP，JSON-RPC 2.0），见 docs/mcp.md
    app.include_router(mcp_router, dependencies=read_only_gate)
    return app


app = create_app()
