"""在 Python 侧给向量打余弦分、取 top-k：本地引擎（SQLite）上的全部语义检索。

SQLite 上向量列是 JSON，数据库算不了距离。做法是**先在 SQL 里把候选集圈好**（激活
空间、库成员、状态分组……），只把 ``(键, 向量)`` 两列取出来，由这里打分排序。

口径：

- 分数 = 余弦相似度，降序，取前 ``limit`` 个；
- 同一个键只算一次（一篇论文命中多个库时成员行会重复）；
- 维度不对、JSON 坏掉、含 NaN/inf、零向量的行直接跳过，绝不让一条脏数据把整次检索
  搞崩。

怎么读、怎么算（#850）：

- **不跨打分持有游标**。SQLite 的读游标开着就占着共享锁，期间别的连接提交写入会
  等满 busy_timeout 后报 "database is locked"。所以先只取候选的键（没有向量列，
  很快），再按键分批取向量：每批 ``execute → fetchall`` 读完即放锁，然后才打分。
  写入方最多等一批的读取时间，而不是整次扫描。
- **打分不占事件循环**。解析 JSON 文本与矩阵乘法放进 ``asyncio.to_thread``，每批
  一次；running top-k 跨批保留，内存只占一批的量。
- **扫描行数有上限**。候选集按库圈定是设计前提：每个库内几千到几万条。跨很多库的
  段落/全文检索（全局助手）用 :func:`budgeted_search`：候选行超过
  ``settings.vector_search_row_budget`` 时先用论文级向量（一篇一条）挑出最相关的
  ``vector_search_narrow_papers`` 篇，再只在这些论文的段落里排——命中的段落仍按
  段落余弦排序，只是候选论文被收窄了，调用方通过 :class:`SearchStats` 如实告知。
"""

from __future__ import annotations

import asyncio
import heapq
import json
import math
import uuid
import warnings
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Text, func, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

try:  # numpy 是主依赖；万一缺了（裸环境跑单测）就退回纯 Python，结果一致只是慢
    import numpy as np
except ImportError:  # pragma: no cover — 依赖里声明了 numpy，正常环境走不到
    np = None  # type: ignore[assignment]

#: 每批按键取多少行向量。1024 维的 JSON 文本一行约 20KB，一批约 20MB；读一批的
#: 时间就是并发写入最多要等的时间。也是一条 IN 列表的长度（远低于 SQLite 的参数上限）。
BATCH_SIZE = 1000


@dataclass
class SearchStats:
    """一次检索实际扫了多少：调用方据此如实告诉用户结果是不是收窄过的。"""

    #: 圈定范围内的候选行数（收窄前）
    candidates: int = 0
    #: 实际读出向量、打过分的行数
    scanned: int = 0
    #: 收窄时只在这么多篇论文里搜；None = 没收窄
    narrowed_to_papers: int | None = None


def raw_embedding(column: Any) -> Any:
    """候选语句里选向量列用：SQLite 上按原始 JSON 文本取出，省掉逐行 json.loads。

    打分时 numpy 直接解析这段文本（见 ``TopK._np_row``）；没装 numpy 时退回 json.loads。
    """
    return type_coerce(column, Text)


def _as_vector(embedding: Any, dim: int) -> Sequence[float] | None:
    """把一行的向量列规整成长度为 ``dim`` 的数列；不合格返回 None（调用方跳过）。"""
    if isinstance(embedding, str | bytes | bytearray):
        try:
            embedding = json.loads(embedding)
        except (ValueError, TypeError):
            return None
    if not isinstance(embedding, list | tuple) or len(embedding) != dim:
        return None
    return embedding


class TopK:
    """流式余弦 top-k：``add`` 多批候选，``result`` 取最终排序。

    同分时先到的在前（排序稳定），同一个键只记第一次出现。
    """

    def __init__(self, query_vector: Sequence[float], limit: int) -> None:
        self.limit = max(0, int(limit))
        self.dim = len(query_vector)
        self._seen: set[Hashable] = set()
        # 小顶堆：(score, -seq, key)；堆顶是当前 top-k 里最差的那个
        self._heap: list[tuple[float, int, Hashable]] = []
        self._seq = 0
        self._query: Any = None
        try:
            query = [float(x) for x in query_vector]
        except (TypeError, ValueError):
            query = []
        norm = math.sqrt(sum(x * x for x in query)) if query else 0.0
        if not query or not math.isfinite(norm) or norm == 0.0:
            self.limit = 0  # 查询向量本身没法比：什么都不返回
            return
        if np is not None:
            self._query = np.asarray(query, dtype=np.float64) / norm
        else:
            self._query = [x / norm for x in query]

    def add(self, rows: Iterable[tuple[Hashable, Any]]) -> None:
        if self.limit == 0:
            return
        keys: list[Hashable] = []
        vectors: list[Any] = []
        parse = self._np_row if np is not None else self._py_row
        for key, embedding in rows:
            if key in self._seen:
                continue
            vector = parse(embedding)
            if vector is None:
                continue
            self._seen.add(key)
            keys.append(key)
            vectors.append(vector)
        if not keys:
            return
        scores = self._score(vectors)
        for key, score in zip(keys, scores, strict=True):
            seq = self._seq
            self._seq += 1
            if score is None:
                continue
            item = (score, -seq, key)
            if len(self._heap) < self.limit:
                heapq.heappush(self._heap, item)
            elif item[:2] > self._heap[0][:2]:
                heapq.heapreplace(self._heap, item)

    def _np_row(self, embedding: Any) -> Any:
        """一行 → 长度 dim 的 float64 数组；不合格返回 None。

        JSON 文本直接交给 numpy 的 C 解析器（``fromstring``），不经 json.loads 造
        上千个 Python float——SQLite 上这一步占了打分耗时的大头。
        """
        try:
            if isinstance(embedding, bytes | bytearray):
                embedding = embedding.decode()
            if isinstance(embedding, str):
                text = embedding.strip()
                if not (text.startswith("[") and text.endswith("]")):
                    return None
                with warnings.catch_warnings():
                    # 解析不完整时 numpy 只给 DeprecationWarning 并返回截断结果：当错误处理
                    warnings.simplefilter("error")
                    row = np.fromstring(text[1:-1], dtype=np.float64, sep=",")
            elif isinstance(embedding, list | tuple):
                row = np.asarray(embedding, dtype=np.float64)
            else:
                return None
        except (TypeError, ValueError, UnicodeDecodeError, Warning):
            return None
        if row.shape != (self.dim,):
            return None
        return row

    def _py_row(self, embedding: Any) -> Sequence[float] | None:
        return _as_vector(embedding, self.dim)

    def _score(self, vectors: list[Any]) -> list[float | None]:
        if np is not None:
            matrix = np.vstack(vectors)
            norms = np.linalg.norm(matrix, axis=1)
            with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
                scores = (matrix @ self._query) / norms
            ok = np.isfinite(scores) & (norms > 0)
            return [float(s) if good else None for s, good in zip(scores, ok, strict=True)]
        return [self._score_one_py(v) for v in vectors]

    def _score_one_py(self, vector: Sequence[float]) -> float | None:
        try:
            row = [float(x) for x in vector]
        except (TypeError, ValueError):
            return None
        norm = math.sqrt(sum(x * x for x in row))
        if not math.isfinite(norm) or norm == 0.0:
            return None
        score = sum(a * b for a, b in zip(row, self._query, strict=True)) / norm
        return score if math.isfinite(score) else None

    def result(self) -> list[tuple[Hashable, float]]:
        ranked = sorted(self._heap, key=lambda item: (item[0], item[1]), reverse=True)
        return [(key, score) for score, _neg_seq, key in ranked]


def top_k_cosine(
    query_vector: Sequence[float],
    candidates: Iterable[tuple[Hashable, Any]],
    limit: int,
) -> list[tuple[Hashable, float]]:
    """一次性版本：``candidates`` 是 ``(键, 向量)``，返回 ``[(键, 余弦)]`` 降序前 ``limit``。"""
    top = TopK(query_vector, limit)
    top.add(candidates)
    return top.result()


def _key_of(row: Sequence[Any]) -> Hashable:
    """候选行的键：最后一列是向量，前面一列就是值本身，多列是元组。"""
    return row[0] if len(row) == 2 else tuple(row[:-1])


def _key_columns(stmt: Any) -> list[Any]:
    columns = list(stmt.selected_columns)
    if len(columns) < 2:
        raise ValueError("候选语句至少要有一列键和最后一列向量")
    return columns[:-1]


async def count_candidates(session: AsyncSession, stmt: Any) -> int:
    """候选语句圈出多少行（不读向量列）。"""
    keys_only = stmt.with_only_columns(*_key_columns(stmt))
    return int(
        (await session.execute(select(func.count()).select_from(keys_only.subquery()))).scalar()
        or 0
    )


async def candidate_keys(session: AsyncSession, stmt: Any) -> list[Hashable]:
    """只取候选的键（不带向量列），按出现顺序去重。一次读完，不留游标。"""
    keys_only = stmt.with_only_columns(*_key_columns(stmt))
    seen: set[Hashable] = set()
    keys: list[Hashable] = []
    for row in (await session.execute(keys_only)).all():
        key = row[0] if len(row) == 1 else tuple(row)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _add_in_key_order(top: TopK, keys: Sequence[Hashable], rows: Sequence[Any]) -> None:
    """按键顺序把一批行交给 TopK（在线程里跑：解析 + 打分都是 CPU 活）。

    IN 查询回来的顺序不保证与键顺序一致；按键顺序喂进去，同分时的先后就与
    「整表一次扫」一致（先出现的在前）。
    """
    by_key: dict[Hashable, Any] = {}
    for row in rows:
        by_key.setdefault(_key_of(row), row[-1])
    top.add((key, by_key[key]) for key in keys if key in by_key)


async def search_statement(
    session: AsyncSession,
    stmt: Any,
    query_vector: Sequence[float],
    limit: int,
    *,
    batch_size: int = BATCH_SIZE,
    stats: SearchStats | None = None,
) -> list[tuple[Any, float]]:
    """执行候选语句，分批取向量、线程里打分，返回 ``[(键, 余弦)]`` 降序前 ``limit``。

    ``stmt`` 的**最后一列是向量**，前面的列组成键：只有一列键时键就是那个值，
    多列时是元组（例如 ``(chunk_id, paper_id)``）。**第一列在候选集里必须唯一**
    （按它分批回查）。过滤条件全在 ``stmt`` 里，每批回查时原样再套一次。
    """
    top = TopK(query_vector, limit)
    if top.limit == 0:
        return []
    keys = await candidate_keys(session, stmt)
    if stats is not None:
        stats.candidates = stats.candidates or len(keys)
    if not keys:
        return []
    first_column = _key_columns(stmt)[0]
    single = len(_key_columns(stmt)) == 1
    batch_size = max(1, int(batch_size))
    for start in range(0, len(keys), batch_size):
        batch = keys[start : start + batch_size]
        firsts = batch if single else [key[0] for key in batch]  # type: ignore[index]
        # 一次读完就放掉读锁；打分在线程里做，期间不占游标也不占事件循环
        rows = (await session.execute(stmt.where(first_column.in_(firsts)))).all()
        if stats is not None:
            stats.scanned += len(rows)
        if rows:
            await asyncio.to_thread(_add_in_key_order, top, batch, rows)
    return top.result()


async def top_paper_ids(
    session: AsyncSession,
    *,
    library_ids: Sequence[uuid.UUID],
    query_vector: Sequence[float],
    space_key: str,
    limit: int,
) -> list[uuid.UUID]:
    """论文级向量（一篇一条，便宜）在这些库里挑最相关的 ``limit`` 篇。"""
    from app.models.library_direction import LibraryPaper
    from app.models.vectors import PaperVector
    from app.services.papers import PAPER_STATUS_GROUPS

    stmt = select(PaperVector.paper_id, raw_embedding(PaperVector.embedding)).where(
        PaperVector.space == space_key,
        PaperVector.paper_id.in_(
            select(LibraryPaper.paper_id).where(
                LibraryPaper.library_id.in_(list(library_ids)),
                LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
            )
        ),
    )
    return [pid for pid, _ in await search_statement(session, stmt, query_vector, limit)]


async def budgeted_search(
    session: AsyncSession,
    stmt: Any,
    *,
    paper_column: Any,
    library_ids: Sequence[uuid.UUID],
    query_vector: Sequence[float],
    space_key: str,
    limit: int,
    stats: SearchStats | None = None,
) -> list[tuple[Any, float]]:
    """段落级检索的有上限版本：候选行太多时先按论文收窄，再在段落里排。

    候选行数不超过 ``settings.vector_search_row_budget`` 时与 :func:`search_statement`
    完全一样；超过时用 :func:`top_paper_ids` 挑出最相关的
    ``settings.vector_search_narrow_papers`` 篇论文，``stmt`` 再加一条
    ``paper_column IN (这些论文)``。论文没有论文级向量就不会进入收窄后的候选——
    这是收窄的代价，``stats.narrowed_to_papers`` 让调用方能说明这一点。
    """
    from app.core.config import get_settings

    settings = get_settings()
    stats = stats if stats is not None else SearchStats()
    stats.candidates = await count_candidates(session, stmt)
    if stats.candidates <= max(0, settings.vector_search_row_budget):
        return await search_statement(session, stmt, query_vector, limit, stats=stats)
    papers = await top_paper_ids(
        session,
        library_ids=library_ids,
        query_vector=query_vector,
        space_key=space_key,
        limit=max(1, settings.vector_search_narrow_papers),
    )
    stats.narrowed_to_papers = len(papers)
    if not papers:
        return []
    return await search_statement(
        session, stmt.where(paper_column.in_(papers)), query_vector, limit, stats=stats
    )
