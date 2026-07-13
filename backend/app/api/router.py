"""API 路由汇总（在 main.py 以 /api 前缀挂载；WS 路由单独挂在根路径）。"""

from fastapi import APIRouter

from app.api import admin_llm, auth, gates, health, projects, voyages

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(projects.router)
api_router.include_router(gates.router)
api_router.include_router(voyages.router)
api_router.include_router(admin_llm.router)
