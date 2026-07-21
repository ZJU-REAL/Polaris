"""API 路由汇总（在 main.py 以 /api 前缀挂载；WS 路由单独挂在根路径）。"""

from fastapi import APIRouter

from app.api import (
    admin_llm,
    auth,
    concepts,
    experiments,
    gates,
    health,
    ideas,
    ingest,
    manuscripts,
    notes,
    papers,
    projects,
    ssh_credentials,
    voyages,
    wiki,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(projects.router)
api_router.include_router(gates.router)
api_router.include_router(voyages.router)
api_router.include_router(admin_llm.router)
api_router.include_router(papers.router)
api_router.include_router(notes.router)
api_router.include_router(concepts.router)
api_router.include_router(ingest.router)
api_router.include_router(wiki.router)
api_router.include_router(ideas.router)
api_router.include_router(ssh_credentials.router)
api_router.include_router(experiments.router)
api_router.include_router(manuscripts.router)
