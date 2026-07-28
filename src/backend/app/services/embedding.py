"""向量的统一出入口（不 import fastapi）。

**所有**嵌入调用都必须经过这里，不要直接调 ``llm.embed()``。这个模块保证三件事：

1. 查询向量与文档向量出自同一个模型——否则余弦排序就是噪声，而且维度恰好相同时
   谁也不会报错（这正是改造前的状态）；
2. 拿到的向量维度与激活空间一致才准入库，不一致直接抛错，绝不悄悄写进去；
3. 每条向量都带上空间标识，检索时据此过滤。

平台第一次成功嵌入时，按模型**实际返回的维度**定义激活空间——维度不写死在任何
地方，换多少维的模型都不用改代码或改表（issue #191）。
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import (
    EmbeddingSpace,
    EmbeddingSpaceMismatchError,
    active_space,
    set_active_space,
)
from app.models.base import utcnow
from app.models.vectors import IdeaVector, PaperChunkVector, PaperVector

EMBEDDING_STAGE = "embedding"

# 文本口径版本（喂给模型的文本怎么拼的）。口径变了就 +1，不影响向量可比性，
# 只是给将来的选择性重建留个依据。
TEXT_VERSION = 1


async def embed_documents(
    session: AsyncSession,
    texts: Sequence[str],
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> tuple[list[list[float]], EmbeddingSpace]:
    """嵌入一批待入库的文本，返回 (向量, 它们所属的空间)。

    平台还没有激活空间时，这一批就定义了它（模型名 + 实际返回的维度），设置随调用方
    的事务一起提交。``user_id`` 等只用于用量记账，**不影响**用哪个模型——嵌入模型
    全局统一（见 core/llm/router.py 的 GLOBAL_ONLY_STAGES）。

    provider 不支持嵌入时抛 NotImplementedError（调用方按既有降级路径处理）；
    模型或维度与激活空间不符时抛 EmbeddingSpaceMismatchError。
    """
    from app.core.llm.router import get_llm_router

    llm = get_llm_router()
    model = await llm.model_name(EMBEDDING_STAGE)
    if model is None:
        raise NotImplementedError("no embedding model configured")

    space = await active_space(session)
    if space is not None and space.model != model:
        raise EmbeddingSpaceMismatchError(
            f"embedding model changed from {space.model!r} to {model!r}: "
            "existing vectors live in another space and cannot be compared with new "
            "ones — rebuild the index under the new model or switch the model back"
        )

    vectors = [
        list(v)
        for v in await llm.embed(
            list(texts),
            stage=EMBEDDING_STAGE,
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=voyage_id,
        )
    ]
    if not vectors:
        # 空输入：没有向量可校验，也不该拿它去初始化空间
        if space is None:
            raise NotImplementedError("cannot resolve embedding space from an empty batch")
        return [], space

    if space is None:
        space = EmbeddingSpace(model=model, dim=len(vectors[0]))
        await set_active_space(session, space)

    for vector in vectors:
        if len(vector) != space.dim:
            raise EmbeddingSpaceMismatchError(
                f"model {model!r} returned a {len(vector)}-dim vector but the active "
                f"space {space} expects {space.dim}"
            )
    return vectors, space


async def embed_query(
    session: AsyncSession,
    text: str,
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> tuple[list[float], EmbeddingSpace]:
    """嵌入一条检索用文本，返回 (向量, 该向量所属空间)。

    与文档向量走同一个模型，所以结果一定可比。**语义检索用不了的两种情况都抛
    NotImplementedError**，让调用方走既有的降级路径（关键词检索 + mode_used 如实上报）：

    - 平台还没建过任何向量（无激活空间）——库里一条向量都没有，无从检索；
    - 管理员换了嵌入模型但还没确认——现有向量出自旧模型，拿新模型的查询向量去比就是
      噪声。这里宁可降级也不能返回一个看着正常、实则乱序的结果。

    写入侧（embed_documents）不做这个转换：那里必须响亮地失败，不能悄悄跳过。
    """
    space = await active_space(session)
    if space is None:
        raise NotImplementedError("no embedding space yet — nothing has been embedded")
    try:
        vectors, space = await embed_documents(
            session,
            [text],
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=voyage_id,
        )
    except EmbeddingSpaceMismatchError as e:
        raise NotImplementedError(f"semantic search unavailable: {e}") from e
    return vectors[0], space


# ---- 写入 ----


async def upsert_paper_vector(
    session: AsyncSession,
    paper_id: uuid.UUID,
    vector: list[float],
    space: EmbeddingSpace,
) -> None:
    """写入/覆盖论文级向量（调用方负责 commit）。"""
    await _upsert(session, PaperVector, PaperVector.paper_id, paper_id, vector, space)


async def upsert_chunk_vector(
    session: AsyncSession,
    chunk_id: uuid.UUID,
    vector: list[float],
    space: EmbeddingSpace,
) -> None:
    """写入/覆盖分段向量（调用方负责 commit）。"""
    await _upsert(
        session, PaperChunkVector, PaperChunkVector.chunk_id, chunk_id, vector, space
    )


async def upsert_idea_vector(
    session: AsyncSession,
    idea_id: uuid.UUID,
    vector: list[float],
    space: EmbeddingSpace,
) -> None:
    """写入/覆盖想法向量（调用方负责 commit）。"""
    await _upsert(session, IdeaVector, IdeaVector.idea_id, idea_id, vector, space)


async def _upsert(
    session: AsyncSession,
    model_class: type,
    key_column,
    key: uuid.UUID,
    vector: list[float],
    space: EmbeddingSpace,
) -> None:
    """原子写入/覆盖向量（调用方负责 commit）。

    必须是真 upsert，不能「先查后插」：论文在全局内容池里是共享的，多个库并行同步时
    会同时给同一篇论文的同一段建向量，两个事务各自查到「没有」，然后一起插——
    (chunk_id, space) 主键冲突。而挂起的 INSERT 往往由后面某个查询的 autoflush 触发，
    于是 UniqueViolationError 会在一个看起来毫不相干的地方抛出来
    （"raised as a result of Query-invoked autoflush"），排查成本很高。
    """
    if len(vector) != space.dim:
        raise EmbeddingSpaceMismatchError(
            f"refusing to store a {len(vector)}-dim vector in space {space}"
        )
    values = {
        key_column.key: key,
        "space": space.key,
        "dim": space.dim,
        "embedding": vector,
        "model": space.model,
        "text_version": TEXT_VERSION,
        "built_at": utcnow(),
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:  # 其余方言没有 ON CONFLICT，退回先查后插（单机开发场景没有并发问题）
        row = (
            await session.execute(
                select(model_class).where(key_column == key, model_class.space == space.key)
            )
        ).scalar_one_or_none()
        if row is None:
            row = model_class(**{key_column.key: key, "space": space.key})
            session.add(row)
        for field, value in values.items():
            setattr(row, field, value)
        return

    stmt = _insert(model_class).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[key_column.key, "space"],
            set_={k: v for k, v in values.items() if k not in (key_column.key, "space")},
        )
    )
    # 该行可能已在 identity map 里且被这条 INSERT 绕过了，过期它免得读到旧向量
    existing = await session.get(model_class, (key, space.key))
    if existing is not None:
        await session.refresh(existing)


# ---- 空间管理（管理端） ----


async def routed_model() -> str | None:
    """路由表里当前配的嵌入模型名（未配置返回 None）。

    不收 user_id：嵌入只走全局路由，问「对谁而言」是没有意义的。
    """
    from app.core.llm.router import get_llm_router

    return await get_llm_router().model_name(EMBEDDING_STAGE)


async def space_inventory(session: AsyncSession) -> list[dict]:
    """库里出现过的全部向量空间及各自的行数（按论文向量数降序）。"""
    counts: dict[str, dict[str, int]] = {}
    for model_class, key_column, field in (
        (PaperVector, PaperVector.paper_id, "papers"),
        (PaperChunkVector, PaperChunkVector.chunk_id, "chunks"),
        (IdeaVector, IdeaVector.idea_id, "ideas"),
    ):
        rows = (
            await session.execute(
                select(model_class.space, func.count(key_column)).group_by(model_class.space)
            )
        ).all()
        for space_key, n in rows:
            counts.setdefault(space_key, {"papers": 0, "chunks": 0, "ideas": 0})
            counts[space_key][field] = int(n)

    active = await active_space(session)
    items = []
    for space_key, n in counts.items():
        model, _, dim = space_key.rpartition("@")
        items.append(
            {
                "key": space_key,
                "model": model or space_key,
                "dim": int(dim) if dim.isdigit() else 0,
                **n,
                "active": active is not None and active.key == space_key,
            }
        )
    items.sort(key=lambda item: (-item["papers"], item["key"]))
    return items


async def adopt_routed_model(session: AsyncSession) -> tuple[EmbeddingSpace, str | None]:
    """把当前路由的嵌入模型定为激活空间，返回 (新空间, 旧空间 key)。

    换嵌入模型的官方做法：改完路由表调用这里确认，之后新向量建到新空间，检索也只
    认新空间——覆盖率会掉到 0 再由重建入口慢慢补回来。旧空间的向量原封不动留在库里，
    再 adopt 回旧模型即可回滚。

    维度不写死：这里现场探一次模型，用它**实际返回**的维度定义新空间。
    """
    from app.core.llm.router import get_llm_router

    llm = get_llm_router()
    model = await llm.model_name(EMBEDDING_STAGE)
    if model is None:
        raise NotImplementedError("no embedding model configured")
    vectors = await llm.embed(["dimension probe"], stage=EMBEDDING_STAGE)
    if not vectors or not vectors[0]:
        raise NotImplementedError(f"model {model!r} returned no vector")
    previous = await active_space(session)
    space = EmbeddingSpace(model=model, dim=len(vectors[0]))
    await set_active_space(session, space)
    await session.commit()
    return space, previous.key if previous is not None else None


# ---- 读取 ----


async def paper_vectors(
    session: AsyncSession, paper_ids: Sequence[uuid.UUID], space: EmbeddingSpace
) -> dict[uuid.UUID, list[float]]:
    """取这些论文在给定空间下的向量（缺的不出现在结果里）。"""
    if not paper_ids:
        return {}
    rows = (
        await session.execute(
            select(PaperVector.paper_id, PaperVector.embedding).where(
                PaperVector.paper_id.in_(list(set(paper_ids))),
                PaperVector.space == space.key,
            )
        )
    ).all()
    return {row[0]: list(row[1]) for row in rows}


async def idea_vectors(
    session: AsyncSession, idea_ids: Sequence[uuid.UUID], space: EmbeddingSpace
) -> dict[uuid.UUID, list[float]]:
    """取这些想法在给定空间下的向量（缺的不出现在结果里）。"""
    if not idea_ids:
        return {}
    rows = (
        await session.execute(
            select(IdeaVector.idea_id, IdeaVector.embedding).where(
                IdeaVector.idea_id.in_(list(set(idea_ids))),
                IdeaVector.space == space.key,
            )
        )
    ).all()
    return {row[0]: list(row[1]) for row in rows}


async def papers_with_vector(
    session: AsyncSession, paper_ids: Sequence[uuid.UUID], space: EmbeddingSpace
) -> set[uuid.UUID]:
    """这些论文里哪些已在给定空间下建好向量（只查 id，不取向量本体）。"""
    if not paper_ids:
        return set()
    rows = (
        await session.execute(
            select(PaperVector.paper_id).where(
                PaperVector.paper_id.in_(list(set(paper_ids))),
                PaperVector.space == space.key,
            )
        )
    ).scalars()
    return set(rows)


async def chunks_with_vector(
    session: AsyncSession, chunk_ids: Sequence[uuid.UUID], space: EmbeddingSpace
) -> set[uuid.UUID]:
    """这些分段里哪些已在给定空间下建好向量。"""
    if not chunk_ids:
        return set()
    rows = (
        await session.execute(
            select(PaperChunkVector.chunk_id).where(
                PaperChunkVector.chunk_id.in_(list(set(chunk_ids))),
                PaperChunkVector.space == space.key,
            )
        )
    ).scalars()
    return set(rows)
