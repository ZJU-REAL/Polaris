"""FastAPI 应用工厂。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.router import api_router
from app.core.config import get_settings
from app.core.db import create_all, dispose_engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # 仅 sqlite（无 docker 的本地 dev）在启动时建表；postgres 走 alembic migration
    if settings.is_sqlite:
        await create_all()
    yield
    await dispose_engine()


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
    return app


app = create_app()
