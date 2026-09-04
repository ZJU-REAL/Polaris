"""假设/实验树读路由（#637）：树整体 + 单节点详情。

只读——树由 discovery 引擎写（D2/D3），用户不直接编辑节点。可见性完全
复用任务口径（services.voyages.get_voyage 内部的 can_view_voyage）：
树是 run 的资产，能看任务就能看树，无权限一律 404（不泄露存在性）。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.hypothesis import HypothesisNode
from app.models.user import User
from app.models.voyage import VoyageRun
from app.schemas.hypothesis import HypothesisNodeRead
from app.services import hypothesis_tree as tree_service
from app.services import voyages as voyages_service

router = APIRouter(prefix="/voyages", tags=["hypotheses"])


async def _viewable_run(
    session: AsyncSession, voyage_id: uuid.UUID, user: User
) -> VoyageRun:
    run = await voyages_service.get_voyage(
        session, voyage_id=voyage_id, user_id=user.id, user=user
    )
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="VOYAGE_NOT_FOUND")
    return run


@router.get("/{voyage_id}/hypothesis-tree", response_model=list[HypothesisNodeRead])
async def get_hypothesis_tree(
    voyage_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[HypothesisNodeRead]:
    """拉平返回 run 的全部节点（父子由前端按 parent_id 拼装）。"""
    run = await _viewable_run(session, voyage_id, user)
    nodes = await tree_service.tree_for_run(session, run.id)
    return [HypothesisNodeRead.model_validate(n) for n in nodes]


@router.get(
    "/{voyage_id}/hypothesis-tree/{node_id}", response_model=HypothesisNodeRead
)
async def get_hypothesis_node(
    voyage_id: uuid.UUID,
    node_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> HypothesisNodeRead:
    run = await _viewable_run(session, voyage_id, user)
    node = await session.get(HypothesisNode, node_id)
    # 节点必须属于路径里的 run：跨 run 直取节点 id 视为不存在
    if node is None or node.run_id != run.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="HYPOTHESIS_NODE_NOT_FOUND")
    return HypothesisNodeRead.model_validate(node)
