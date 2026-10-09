"""FastAPI 应用工厂。"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.router import api_router
from app.api.ws import router as ws_router
from app.core.config import get_settings
from app.core.db import create_all, dispose_engine, drain_side_writes, get_sessionmaker
from app.core.llm.router import LLMNotConfiguredError
from app.core.redis import close_redis
from app.mcp import mcp_router
from app.services.crdt_rooms import reset_crdt_rooms
from app.services.crdt_stream import get_crdt_stream_subscriber, stop_crdt_stream_subscriber
from app.services.discipline_packs import load_disciplines
from app.services.interdisciplinary_workflows import ensure_guidance_documents
from app.services.local_user import ensure_local_user

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
    # 跨学科工作流的指引文档种子（按 slug 幂等）；失败不阻断启动（如 migration 未跑）
    try:
        async with get_sessionmaker()() as session:
            await ensure_guidance_documents(session)
    except Exception:  # noqa: BLE001
        logger.warning("guidance document seeding failed (migrations pending?)", exc_info=True)
    # 学科包：把抽取 schema 的注册缝接到磁盘（内置包 + <data_dir>/disciplines）。
    # 纯文件读取、不碰数据库，所以不跟着上面的 DB 种子一起 try
    load_disciplines()
    # 上一次进程被杀时写到一半的「进行中」行：标失败、清暂存（#850）。此刻还没有任何
    # 后台任务在跑，所以凡是「进行中」的都是孤儿
    from app.services.startup_recovery import recover_interrupted_work

    await recover_interrupted_work()
    # AI 起草流式镜像订阅（worker 发布 → 写活跃 CRDT 房间；连不上 redis 自动放弃）
    get_crdt_stream_subscriber().start()
    # 本地用户先落定再起定时任务（#850）：否则第一拍时 local@ 还没建，
    # local_user_id() 会先认到别的用户、等 local-session 建出 local@ 后又换人
    try:
        async with get_sessionmaker()() as session:
            await ensure_local_user(session)
    except Exception:  # noqa: BLE001
        logger.warning("ensuring the local user failed (migrations pending?)", exc_info=True)
    # 定时任务由引擎进程自己跑（#842）
    from app.core.scheduler import start_engine_scheduler

    await start_engine_scheduler()
    yield
    from app.core.scheduler import stop_engine_scheduler

    await stop_engine_scheduler()
    # 进程内后台任务：先取消并等它们收尾，再关数据库——否则它们会往已 dispose 的
    # engine 里写（#850）。被打断的航程下次启动由 reconcile_stuck_voyages 续跑
    from app.core.queue import shutdown_task_queue
    from app.services.paper_enrich import cancel_all_tasks

    await asyncio.gather(shutdown_task_queue(), cancel_all_tasks())
    # 转到后台的记账/日志写入：落完盘再关库
    await drain_side_writes()
    # 外部 agent 子进程（#836）：随服务一起收掉，别留孤儿进程继续烧额度
    from app.core.llm.acp import shutdown_pools
    from app.services.acp.pool import shutdown_pool

    await shutdown_pool()
    await shutdown_pools()
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

    # 只读账号（游客）的写入闸门已随治理机制移除（#614）：单机档位登录即主人。
    app.include_router(api_router, prefix="/api")
    # WS 不挂 /api 前缀：nginx 按 /ws 反代（Upgrade），见 docs/architecture.md §7
    app.include_router(ws_router)
    # MCP 工具服务：默认只读；受 scope 保护的 Profile 可显式开放写工具。
    app.include_router(mcp_router)
    return app


app = create_app()
