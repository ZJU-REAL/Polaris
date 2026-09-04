"""假设/实验树的服务层（#637，设计报告 §8.2；不 import fastapi）。

树是 discovery run 的资产：引擎（D2/D3）经这里写节点，读 API 只消费
tree_for_run。结构不变量全部收在 create_node：

- 每 run 单根：parent_id=None 的节点只许有一个（第二次抛 ValueError，
  API 层如需暴露转 409）；
- 防环：parent 必须**已存在**且属同 run——新节点落库时不可能是任何既有
  节点的祖先，因此按 parent 链构造的图天然无环，无需运行时环检测。

状态机（HYPOTHESIS_NODE_STATUSES）：
    open → expanded | pruned
    expanded → validated | refuted | pruned
validated / refuted / pruned 均为终态；剪枝级联整个子树（被剪分支留痕不删，
防择优汇报——§8.2 的防失败模式）。
"""

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.hypothesis import HYPOTHESIS_NODE_KINDS, HypothesisNode
from app.models.voyage import VoyageRun

# 合法状态迁移表；不在表里（含从终态出发）一律拒绝
_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"expanded", "pruned"}),
    "expanded": frozenset({"validated", "refuted", "pruned"}),
}


async def create_node(
    session: AsyncSession,
    run: VoyageRun,
    *,
    parent_id: uuid.UUID | None,
    kind: str,
    statement: str,
    grounding: list[Any] | None = None,
    novelty_report: dict[str, Any] | None = None,
    feasibility: dict[str, Any] | None = None,
    score: float | None = None,
) -> HypothesisNode:
    """建节点并 commit。结构不变量（单根/同 run 父/合法 kind）违例抛 ValueError。"""
    if kind not in HYPOTHESIS_NODE_KINDS:
        raise ValueError(f"unknown hypothesis node kind: {kind!r}")
    if parent_id is None:
        # 每 run 单根：恢复语义（best_open_node）与树读取都假定单棵树，
        # 多根会让「从最优节点继续」失去唯一起点
        existing_root = (
            await session.execute(
                select(HypothesisNode.id).where(
                    HypothesisNode.run_id == run.id, HypothesisNode.parent_id.is_(None)
                )
            )
        ).first()
        if existing_root is not None:
            raise ValueError(f"run {run.id} already has a root hypothesis node")
    else:
        parent = await session.get(HypothesisNode, parent_id)
        if parent is None or parent.run_id != run.id:
            # 跨 run 挂父等同于父不存在：树是 run 私有资产，不能互相嫁接
            raise ValueError(f"parent node {parent_id} not found in run {run.id}")
    node = HypothesisNode(
        run_id=run.id,
        parent_id=parent_id,
        kind=kind,
        statement=statement,
        grounding=grounding,
        novelty_report=novelty_report,
        feasibility=feasibility,
        score=score,
        status="open",
    )
    session.add(node)
    await session.commit()
    await session.refresh(node)
    return node


async def set_grounding(
    session: AsyncSession, node: HypothesisNode, grounding: list[Any] | None
) -> HypothesisNode:
    node.grounding = grounding
    node.updated_at = utcnow()
    await session.commit()
    return node


async def set_novelty(
    session: AsyncSession, node: HypothesisNode, novelty_report: dict[str, Any] | None
) -> HypothesisNode:
    node.novelty_report = novelty_report
    node.updated_at = utcnow()
    await session.commit()
    return node


async def set_feasibility(
    session: AsyncSession, node: HypothesisNode, feasibility: dict[str, Any] | None
) -> HypothesisNode:
    node.feasibility = feasibility
    node.updated_at = utcnow()
    await session.commit()
    return node


async def set_score(
    session: AsyncSession, node: HypothesisNode, score: float | None
) -> HypothesisNode:
    node.score = score
    node.updated_at = utcnow()
    await session.commit()
    return node


async def transition(
    session: AsyncSession, node: HypothesisNode, to_status: str
) -> HypothesisNode:
    """按状态机迁移并 commit；非法迁移抛 ValueError。

    to_status="pruned" 级联：整个子树全部置 pruned——父分支被放弃后，子节点
    单独存活没有意义（它们的前提没了），且留痕（而非删除）是 §8.2 防择优
    汇报的硬要求。级联用迭代逐层下推而不是递归 CTE：sqlite/postgres 都支持
    WITH RECURSIVE，但 ORM 层拼递归 CTE 的可读性差、收益只在深树，而假设树
    深度是个位数（每层是一次 Navigator 决策）。
    """
    allowed = _TRANSITIONS.get(node.status, frozenset())
    if to_status not in allowed:
        raise ValueError(
            f"invalid hypothesis node transition: {node.status} -> {to_status}"
        )
    now = utcnow()
    node.status = to_status
    node.updated_at = now
    if to_status == "pruned":
        frontier: list[uuid.UUID] = [node.id]
        while frontier:
            children = (
                (
                    await session.execute(
                        select(HypothesisNode.id).where(
                            HypothesisNode.parent_id.in_(frontier)
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not children:
                break
            # 子树无条件置 pruned（不走状态机校验）：validated 的子节点也一并
            # 剪掉——结论仍留在行里（status 之外的列不动），只是不再是活分支
            # synchronize_session="fetch"：expire_on_commit=False 下，identity map
            # 里已加载的子节点对象不会被裸 UPDATE 刷新，同 session 随后的读会
            # 拿到旧 status——fetch 策略把内存对象一并改掉
            await session.execute(
                update(HypothesisNode)
                .where(HypothesisNode.id.in_(children))
                .values(status="pruned", updated_at=now)
                .execution_options(synchronize_session="fetch")
            )
            frontier = list(children)
    await session.commit()
    await session.refresh(node)
    return node


async def tree_for_run(
    session: AsyncSession, run_id: uuid.UUID
) -> list[HypothesisNode]:
    """一次查询拉平返回 run 的全部节点（父子拼装交给 API/前端）。

    created_at 再按 id 定序：同秒批量建的兄弟节点也要有稳定顺序。
    """
    return list(
        (
            await session.execute(
                select(HypothesisNode)
                .where(HypothesisNode.run_id == run_id)
                .order_by(HypothesisNode.created_at, HypothesisNode.id)
            )
        )
        .scalars()
        .all()
    )


async def best_open_node(
    session: AsyncSession, run_id: uuid.UUID
) -> HypothesisNode | None:
    """score 最高的 open 节点（D2 恢复语义的地基）；空树/无 open 返回 None。

    score 为空的节点排最后（还没评分不代表最优）；同分按 created_at 取早的，
    保证恢复起点确定性。
    """
    return (
        await session.execute(
            select(HypothesisNode)
            .where(HypothesisNode.run_id == run_id, HypothesisNode.status == "open")
            .order_by(
                HypothesisNode.score.desc().nulls_last(), HypothesisNode.created_at
            )
            .limit(1)
        )
    ).scalar_one_or_none()
