"""假设/实验树读路由（#637）：树整体 + 单节点详情 + run 产物只读（#655）。

只读——树由 discovery 引擎写（D2/D3），用户不直接编辑节点。可见性完全
复用任务口径（services.voyages.get_voyage 内部的 can_view_voyage）：
树是 run 的资产，能看任务就能看树，无权限一律 404（不泄露存在性）。
"""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.models.voyage import VoyageRun
from app.schemas.hypothesis import HypothesisNodeRead
from app.services import hypothesis_tree as tree_service
from app.services import voyages as voyages_service

router = APIRouter(prefix="/voyages", tags=["hypotheses"])

# 可外露的产物白名单（#655）：checkpoint["artifacts"] 里还混着内部状态的余地，
# 只放行明确面向用户的两份 discovery 产物；名单外一律 404（与不存在同口径，
# 不泄露「有没有这个键」）。后续 kind 的产物要外露时在这里登记。
ARTIFACT_WHITELIST = frozenset({"discovery-summary.json", "discovery-disclosure.json"})


async def _viewable_run(session: AsyncSession, voyage_id: uuid.UUID, user: User) -> VoyageRun:
    run = await voyages_service.get_voyage(session, voyage_id=voyage_id, user_id=user.id, user=user)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="VOYAGE_NOT_FOUND")
    return run


@router.get("/{voyage_id}/hypothesis-tree", response_model=list[HypothesisNodeRead])
async def get_hypothesis_tree(
    voyage_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[HypothesisNodeRead]:
    """拉平返回 run 的全部节点（父子由前端按 parent_id 拼装）。

    被剪节点带上剪枝原因（``prune`` / ``prune_reason``），取自 checkpoint 的决策留痕。
    """
    run = await _viewable_run(session, voyage_id, user)
    nodes = await tree_service.tree_for_run(session, run.id)
    decisions = ((run.checkpoint or {}).get("discovery") or {}).get("decisions") or []
    pruned: dict[str, dict] = {}
    for decision in decisions:
        if isinstance(decision, dict) and decision.get("decision") == "pruned":
            pruned.setdefault(str(decision.get("node_id")), decision)
    out = []
    for node in nodes:
        read = HypothesisNodeRead.model_validate(node)
        decision = pruned.get(str(node.id)) if node.status == "pruned" else None
        if decision is not None:
            read.prune = decision.get("prune") or None
            read.prune_reason = str(decision.get("why") or "") or None
        out.append(read)
    return out


@router.get("/{voyage_id}/tournament")
async def get_hypothesis_tournament(
    voyage_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict:
    """锦标赛披露（#653，深度模式）：对阵全记录 + 参赛节点终榜。

    数据在 run 的 checkpoint 轮次账本里（不动 hypothesis_nodes 表结构），所以走
    独立端点而不是并进树节点响应；基础模式（tournament=False）如实返回空结构。
    """
    run = await _viewable_run(session, voyage_id, user)
    state = ((run.checkpoint or {}).get("discovery") or {}).get("tournament") or {}
    return {
        "matches": state.get("matches") or [],
        "nodes": state.get("nodes") or {},
    }


@router.get("/{voyage_id}/artifacts/{name}")
async def get_voyage_artifact(
    voyage_id: uuid.UUID,
    name: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict:
    """run 产物只读（#655，补 D5 标注的缺口：研究方案/披露报告此前无端点）。

    产物存在 checkpoint["artifacts"]（值是 JSON 字符串），这里解析后返回——
    前端不必自己 loads。可见性同树端点：能看任务就能看产物，无权限/名单外/
    尚未产出一律 404。
    """
    run = await _viewable_run(session, voyage_id, user)
    if name not in ARTIFACT_WHITELIST:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ARTIFACT_NOT_FOUND")
    raw = ((run.checkpoint or {}).get("artifacts") or {}).get(name)
    if raw is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ARTIFACT_NOT_FOUND")
    return {"name": name, "content": json.loads(raw) if isinstance(raw, str) else raw}
