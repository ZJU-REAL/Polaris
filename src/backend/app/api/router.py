"""API 路由汇总（在 main.py 以 /api 前缀挂载；WS 路由单独挂在根路径）。"""

from fastapi import APIRouter

from app.api import (
    admin_codes,
    admin_llm,
    admin_users,
    auth,
    concepts,
    daily,
    experiments,
    feedback,
    gates,
    health,
    highlights,
    ideas,
    ingest,
    invites,
    libraries,
    library,
    manuscripts,
    market,
    mcp_meta,
    me_llm,
    notes,
    papers,
    presentations,
    projects,
    publications,
    search,
    shelf,
    skills,
    ssh_credentials,
    users_profile,
    voyages,
    wiki,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users_profile.router)
api_router.include_router(invites.router)
api_router.include_router(admin_users.router)
api_router.include_router(admin_codes.router)
api_router.include_router(feedback.router)
api_router.include_router(projects.router)
api_router.include_router(gates.router)
api_router.include_router(voyages.router)
api_router.include_router(admin_llm.router)
api_router.include_router(me_llm.router)
api_router.include_router(papers.router)
api_router.include_router(library.router)
api_router.include_router(libraries.router)
api_router.include_router(publications.router)
api_router.include_router(notes.router)
api_router.include_router(highlights.router)
api_router.include_router(concepts.router)
api_router.include_router(ingest.router)
api_router.include_router(wiki.router)
api_router.include_router(ideas.router)
api_router.include_router(search.router)
api_router.include_router(shelf.router)
api_router.include_router(daily.router)
api_router.include_router(skills.router)
api_router.include_router(market.router)
api_router.include_router(mcp_meta.router)
api_router.include_router(presentations.router)
api_router.include_router(ssh_credentials.router)
api_router.include_router(experiments.router)
api_router.include_router(manuscripts.router)
