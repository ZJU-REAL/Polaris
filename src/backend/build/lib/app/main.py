"""FastAPI 应用工厂。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.router import api_router
from app.api.ws import router as ws_router
from app.core.config import get_settings
from app.core.db import create_all, dispose_engine
from app.core.redis import close_redis
from app.services.crdt_rooms import reset_crdt_rooms


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # 仅 sqlite（无 docker 的本地 dev）在启动时建表；postgres 走 alembic migration
    if settings.is_sqlite:
        await create_all()
    yield
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
    return app


app = create_app()
