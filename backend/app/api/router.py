"""API 路由汇总（在 main.py 以 /api 前缀挂载）。"""

from fastapi import APIRouter

from app.api import auth, gates, health, projects

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(projects.router)
api_router.include_router(gates.router)
