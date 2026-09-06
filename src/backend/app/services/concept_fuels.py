"""概念燃料（P2.5 F3，#664；设计报告 §11 燃料 1）：库内概念共现 + 未连接概念对挖掘。

纯确定性计算，不走 LLM。共现口径：两个**正式**概念（active，候选不算——与概念列表 /
图谱同一收口）关联到同一篇库内论文记 1 次共现，边权 = 这样的论文数；论文范围与图谱一致
（:data:`app.services.graph.GRAPH_PAPER_STATUSES`）——回收站 / 未过筛选的论文不供燃料。

为什么**不物化**（不建 concept_cooccurrence 表）：实测 dev 库最大 163 个概念 /
~1.3k 个共现对实例（每篇平均 3–6 个概念），整库共现即时聚合是一次带索引的 join +
Python 分组，毫秒级；即使规模涨两个数量级也在亚秒内。物化要换来的只是省这一次聚合，
代价却是迁移（与并行 F2/F4 撞链）+ 增量刷新钩子 + 陈旧数据对账三件事。即时算读路径
永远反映当前 paper_concepts——正确性由构造保证，没有「落后于库内容变更」的状态。
若未来出现万级概念的库，再把本模块的聚合结果落表即可（接口不变）。

未连接对（Swanson ABC）：A–B 与 B–C 有共现而 A–C 从未共现 → 候选 (A, C)。
bridge 强度取 Σ_B min(w(A,B), w(B,C))——每条桥的贡献受两侧较弱一边限制（一侧只共现
过 1 次的桥再多也只算 1），可逐桥拆开解释，且不像乘积那样被单个高频概念抬爆。
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import LibraryPaper
from app.models.paper import CONCEPT_STATUS_ACTIVE, Concept, paper_concepts
from app.services.graph import GRAPH_PAPER_STATUSES

# 防爆阈值：库内正式概念超过这个数时，只保留度数（共现邻居数）top 席位再挖掘。
# 挖掘是 Σ_B deg(B)² 的（对每个桥概念枚举其邻居两两组合），概念数封顶后总工作量
# 上界 ~2·n·E，n=2000 时最坏也在秒级；实测库在 10² 量级，远够不到这里。
COOCCURRENCE_MAX_CONCEPTS = 2000
# 每个候选对最多附带的桥概念数（按强度取最强的几条，够解释即可）
MAX_BRIDGES_PER_PAIR = 5


async def library_cooccurrence(
    session: AsyncSession, *, library_id: uuid.UUID
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, dict[uuid.UUID, int]]]:
    """库内概念共现图：返回 (概念 id→名字, 邻接表 adj[a][b] = 共现论文数)。

    邻接表对称（a∈adj[b] ⇔ b∈adj[a]）；孤立概念（库内没与任何概念同篇出现过的）
    不出现在邻接表里——它们既当不了桥也构不成候选对。
    """
    rows = (
        await session.execute(
            select(paper_concepts.c.paper_id, Concept.id, Concept.name)
            .join(Concept, Concept.id == paper_concepts.c.concept_id)
            .join(LibraryPaper, LibraryPaper.paper_id == paper_concepts.c.paper_id)
            .where(
                LibraryPaper.library_id == library_id,
                LibraryPaper.status.in_(GRAPH_PAPER_STATUSES),
                Concept.status == CONCEPT_STATUS_ACTIVE,
            )
        )
    ).all()
    names: dict[uuid.UUID, str] = {}
    by_paper: dict[uuid.UUID, list[uuid.UUID]] = {}
    for paper_id, concept_id, name in rows:
        names[concept_id] = name
        by_paper.setdefault(paper_id, []).append(concept_id)

    adj: dict[uuid.UUID, dict[uuid.UUID, int]] = {}
    for concept_ids in by_paper.values():
        # 同一篇论文的概念两两共现一次（paper_concepts 有唯一约束，无重复行）
        ordered = sorted(set(concept_ids))
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                adj.setdefault(a, {})[b] = adj.get(a, {}).get(b, 0) + 1
                adj.setdefault(b, {})[a] = adj[a][b]
    return names, adj


def _prune_to_top_degree(
    adj: dict[uuid.UUID, dict[uuid.UUID, int]],
    names: dict[uuid.UUID, str],
    cap: int,
) -> dict[uuid.UUID, dict[uuid.UUID, int]]:
    """防爆：概念数超过 ``cap`` 时只保留度数 top 席位，边随之收缩到保留集内。

    为什么按度数取：挖掘的工作量集中在高度数桥概念上，且低度数概念既提供不了几条桥、
    也几乎构不成有分量的候选对——裁掉它们对 top 结果影响最小。并列时按共现总权重、
    再按名字取，保证结果确定。
    """
    if len(adj) <= cap:
        return adj
    kept = set(
        sorted(
            adj,
            key=lambda cid: (-len(adj[cid]), -sum(adj[cid].values()), names.get(cid, ""), cid),
        )[:cap]
    )
    return {a: {b: w for b, w in nbrs.items() if b in kept} for a, nbrs in adj.items() if a in kept}


async def mine_unconnected_pairs(
    session: AsyncSession, *, library_id: uuid.UUID, top_n: int = 50
) -> list[dict[str, Any]]:
    """挖掘库内「可能有关联但从未同篇出现」的概念对（Swanson ABC 候选）。

    返回按强度降序的 top_n 个候选对，每个形如::

        {"concept_a": {"id", "name"}, "concept_c": {"id", "name"},
         "strength": int, "bridges": [{"id", "name", "strength"}, ...]}

    concept_a / concept_c 取规范序（a < c，按 uuid）；bridges 为最强的
    ≤ :data:`MAX_BRIDGES_PER_PAIR` 个桥概念，strength 为该桥的 min(w(A,B), w(B,C))。
    排序全程确定：总强度并列时按（名字序的）概念名对排。
    """
    names, adj = await library_cooccurrence(session, library_id=library_id)
    adj = _prune_to_top_degree(adj, names, COOCCURRENCE_MAX_CONCEPTS)

    strength: dict[tuple[uuid.UUID, uuid.UUID], int] = {}
    bridges: dict[tuple[uuid.UUID, uuid.UUID], list[tuple[int, uuid.UUID]]] = {}
    for b, nbrs in adj.items():
        neighbors = sorted(nbrs)  # 排过序，枚举出的 (a, c) 天然满足规范序 a < c
        for i, a in enumerate(neighbors):
            a_nbrs = adj[a]
            for c in neighbors[i + 1 :]:
                if c in a_nbrs:
                    continue  # A–C 已经共现过，不是「未连接」候选
                w = min(nbrs[a], nbrs[c])
                key = (a, c)
                strength[key] = strength.get(key, 0) + w
                bridges.setdefault(key, []).append((w, b))

    # 并列强度按「名字序的概念名对」定序：不依赖随机 uuid，跑多少遍结果都一样
    ranked = sorted(
        strength.items(),
        key=lambda kv: (-kv[1], tuple(sorted((names[kv[0][0]], names[kv[0][1]])))),
    )[: max(top_n, 0)]

    out: list[dict[str, Any]] = []
    for (a, c), total in ranked:
        top_bridges = sorted(bridges[(a, c)], key=lambda wb: (-wb[0], names[wb[1]], wb[1]))
        out.append(
            {
                "concept_a": {"id": str(a), "name": names[a]},
                "concept_c": {"id": str(c), "name": names[c]},
                "strength": total,
                "bridges": [
                    {"id": str(b), "name": names[b], "strength": w}
                    for w, b in top_bridges[:MAX_BRIDGES_PER_PAIR]
                ],
            }
        )
    return out
