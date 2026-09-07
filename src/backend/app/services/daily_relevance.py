"""每日论文池 × 用户文献库的相关性（#623）。

每日池从订阅分类整体抓取，但真正值得用户先看的，是和**他自己的文献库**方向相近的
那部分。本模块把「相关不相关」量化成分数：

- 锚点 = 每个可见库的代表向量：库内论文（真正收录的成员，status 组 ``library``）
  论文级向量的质心。质心缓存在 system_settings（``daily_library_anchor:<id>``），
  用「空间 + 成员数 + 成员行最后更新时间」做指纹，库一变（收录/删除/状态流转都会
  碰成员行的 updated_at）指纹就变，下次读取自动重算——不用在每条写路径上挂失效钩子。
- 库还没有任何成员向量（或平台没有激活空间）时降级为关键词匹配：收录设置里的
  关键词 chips（definition.keywords.include）命中即给一个固定分。关键词是用户
  亲手列的方向意图，比没有强得多。
- 条目得分 = max over 库（这篇最像哪个库就按哪个库算），并带上来源库标注——
  前端徽章「与你的库相关 · 某库」要能点回那个库。

确定性逻辑，不走 LLM；嵌入向量是既有产物（每日同步已无条件建论文级向量）。
"""

import json
import logging
import math
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import EmbeddingSpace, active_space
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.models.vectors import PaperVector
from app.services.libraries import library_visible_to
from app.services.papers import PAPER_STATUS_GROUPS
from app.services.relevance import _include_keywords

logger = logging.getLogger(__name__)

#: 质心缓存的 system_settings 键前缀（每库一行）。
ANCHOR_SETTING_PREFIX = "daily_library_anchor:"

#: 排序融合里库相关性的权重（其余给新近度）。故意温和：每日页的主线是「今天有什么
#: 新东西」，权重给大了列表就会长期被旧兴趣的近亲霸榜，用户再也刷不到方向之外的
#: 新苗头——推荐系统里经典的兴趣过拟合。0.3 的效果是：相关性满分大约能把一篇论文
#: 「抬」半个保留窗口的新近度（0.3 ≈ 0.7 × 6/14），足够让三五天前的高相关论文
#: 排到今天的无关论文前面，但压不过今天同样相关的。
RELEVANCE_WEIGHT = 0.3

#: 徽章阈值：得分低于它就不标「与你的库相关」。质心余弦对同领域论文通常落在
#: 0.6 以上、无关领域在 0.5 以下（bge 系模型的经验区间）；排序不看这个阈值
#: （原始分参与融合），它只管徽章别乱贴。
MATCH_THRESHOLD = 0.55

#: 关键词降级命中给的固定分。取在阈值之上（命中了就该出徽章），又低于「向量高度
#: 相似」的典型区间——关键词命中只是字面证据，不该压过真正的语义近邻。
KEYWORD_HIT_SCORE = 0.6

#: 参与质心的成员状态：真正收进库的（与「已收录」列表口径一致，候选/回收站不算）。
MEMBER_STATUSES = PAPER_STATUS_GROUPS["library"]


@dataclass
class LibraryAnchor:
    """一个库的相关性锚点：优先质心向量，没有则关键词。"""

    library_id: uuid.UUID
    name: str
    centroid: list[float] | None
    keywords: list[str]


def _anchor_setting_key(library_id: uuid.UUID) -> str:
    return f"{ANCHOR_SETTING_PREFIX}{library_id}"


async def library_anchors(session: AsyncSession, *, user: User) -> list[LibraryAnchor]:
    """请求者可见的库各出一个锚点；质心和关键词都没有的库不出（无从比较）。"""
    libraries = (await session.execute(select(DirectionLibrary))).scalars().all()
    visible = [lib for lib in libraries if library_visible_to(lib, user)]
    if not visible:
        return []
    space = await active_space(session)
    anchors: list[LibraryAnchor] = []
    for lib in visible:
        definition = lib.definition if isinstance(lib.definition, dict) else {}
        keywords = _include_keywords(definition)
        centroid = await _library_centroid(session, lib.id, space) if space else None
        if centroid is None and not keywords:
            continue
        anchors.append(
            LibraryAnchor(library_id=lib.id, name=lib.name, centroid=centroid, keywords=keywords)
        )
    return anchors


async def _library_centroid(
    session: AsyncSession, library_id: uuid.UUID, space: EmbeddingSpace
) -> list[float] | None:
    """库内成员论文向量的质心（带指纹缓存）；一条成员向量都没有时为 None。"""
    count, max_updated = (
        await session.execute(
            select(func.count(), func.max(LibraryPaper.updated_at)).where(
                LibraryPaper.library_id == library_id,
                LibraryPaper.status.in_(MEMBER_STATUSES),
            )
        )
    ).one()
    if not count:
        return None
    # 指纹三要素：空间（换嵌入模型必须重算）、成员数、成员行最后更新时间。任何收录/
    # 删除/状态流转都会动其中至少一项。不用「成员 id 列表哈希」是因为那要每次请求
    # 全量拉 id——大库几千行，而这里一条聚合就够。
    fingerprint = f"{space.key}|{count}|{max_updated.isoformat() if max_updated else '-'}"
    key = _anchor_setting_key(library_id)
    row = await session.get(SystemSetting, key)
    if (
        row is not None
        and isinstance(row.value, dict)
        and row.value.get("fingerprint") == fingerprint
    ):
        cached = row.value.get("centroid")
        return [float(x) for x in cached] if cached else None
    centroid = await _compute_centroid(session, library_id, space)
    value: dict[str, Any] = {"fingerprint": fingerprint, "space": space.key, "centroid": centroid}
    if row is None:
        session.add(SystemSetting(key=key, value=value))
    else:
        row.value = value
    await session.commit()
    return centroid


async def _compute_centroid(
    session: AsyncSession, library_id: uuid.UUID, space: EmbeddingSpace
) -> list[float] | None:
    """成员向量取均值。postgres 让数据库聚合（几千条向量不搬出来）；其余方言（测试的
    sqlite，向量是 JSON 列）在 Python 里算——那种部署本来就没有大库。"""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        row = (
            await session.execute(
                text(
                    "SELECT CAST(AVG(v.embedding) AS text) FROM paper_vectors v "
                    "WHERE v.space = :space AND v.paper_id IN ("
                    "SELECT lp.paper_id FROM library_papers lp "
                    "WHERE lp.library_id = :library_id "
                    "AND lp.status = ANY(CAST(:statuses AS varchar[])))"
                ),
                {
                    "space": space.key,
                    "library_id": str(library_id),
                    "statuses": list(MEMBER_STATUSES),
                },
            )
        ).scalar_one_or_none()
        if not row:
            return None
        return [float(x) for x in json.loads(row)]  # pgvector 的文本形式就是 JSON 数组
    member_sq = select(LibraryPaper.paper_id).where(
        LibraryPaper.library_id == library_id, LibraryPaper.status.in_(MEMBER_STATUSES)
    )
    vectors = (
        (
            await session.execute(
                select(PaperVector.embedding).where(
                    PaperVector.space == space.key, PaperVector.paper_id.in_(member_sq)
                )
            )
        )
        .scalars()
        .all()
    )
    if not vectors:
        return None
    dim = len(vectors[0])
    acc = [0.0] * dim
    for vec in vectors:
        for i, x in enumerate(vec):
            acc[i] += float(x)
    return [x / len(vectors) for x in acc]


# ---- 打分 ----


def _cosine(a: list[float], b: list[float]) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def keyword_score(keywords: list[str], text_: str) -> float:
    """关键词降级：任一收录关键词字面命中标题/摘要即给固定分（大小写不敏感）。"""
    if not keywords or not text_:
        return 0.0
    lowered = text_.lower()
    return KEYWORD_HIT_SCORE if any(kw.lower() in lowered for kw in keywords) else 0.0


def anchor_score(anchor: LibraryAnchor, vector: list[float] | None, text_: str) -> float:
    """一篇论文对一个锚点的得分。质心与论文向量都在才用余弦；论文缺向量（嵌入失败的
    兜底）或库没质心时退回关键词——降级路径必须有，否则这些论文永远排在最后。"""
    if anchor.centroid is not None and vector is not None:
        return _cosine(anchor.centroid, vector)
    return keyword_score(anchor.keywords, text_)


async def relevance_for_papers(
    session: AsyncSession, papers: list[Paper], anchors: list[LibraryAnchor]
) -> dict[uuid.UUID, tuple[float, LibraryAnchor]]:
    """每篇论文取所有锚点里的最高分及其来源库；零分的不出现在结果里。"""
    if not papers or not anchors:
        return {}
    space = await active_space(session)
    vectors: dict[uuid.UUID, list[float]] = {}
    if space is not None:
        rows = await session.execute(
            select(PaperVector.paper_id, PaperVector.embedding).where(
                PaperVector.space == space.key,
                PaperVector.paper_id.in_([p.id for p in papers]),
            )
        )
        vectors = {pid: [float(x) for x in emb] for pid, emb in rows.all()}
    out: dict[uuid.UUID, tuple[float, LibraryAnchor]] = {}
    for paper in papers:
        text_ = f"{paper.title or ''}\n{paper.abstract or ''}"
        vector = vectors.get(paper.id)
        best: tuple[float, LibraryAnchor] | None = None
        for anchor in anchors:
            score = anchor_score(anchor, vector, text_)
            if score > 0.0 and (best is None or score > best[0]):
                best = (score, anchor)
        if best is not None:
            out[paper.id] = best
    return out


def fused_score(relevance: float, days_ago: int, window: int) -> float:
    """新近度与库相关性的加权融合（排序键，越大越靠前）。

    新近度按保留窗口线性归一（今天 1.0，掉出窗口 0.0）；权重见
    :data:`RELEVANCE_WEIGHT` 的注释。postgres 侧的 SQL 表达式
    （services/daily_feed 的相关性分支）必须与这里同公式。
    """
    recency = max(0.0, 1.0 - days_ago / float(max(window, 1)))
    return (1.0 - RELEVANCE_WEIGHT) * recency + RELEVANCE_WEIGHT * relevance
