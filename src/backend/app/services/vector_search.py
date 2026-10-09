"""在 Python 侧给向量打余弦分、取 top-k：没有 pgvector 的数据库（SQLite）上的语义检索。

postgres 上各检索入口照旧用 pgvector 的 ``<=>`` 在 SQL 里排；SQLite 上向量列是 JSON，
数据库算不了距离。做法是**先在 SQL 里把候选集圈好**（激活空间、库成员、状态分组……
与 pgvector 那条 SQL 同一套过滤），只把 ``(键, 向量)`` 两列取出来，由这里打分排序。

口径与 pgvector 对齐：

- 分数 = 1 - 余弦距离 = 余弦相似度，降序，取前 ``limit`` 个；
- 同一个键只算一次（对应 SQL 里的 ``DISTINCT``：一篇论文命中多个库时成员行会重复）；
- 维度不对、JSON 坏掉、含 NaN/inf、零向量的行直接跳过，绝不让一条脏数据把整次检索
  搞崩（pgvector 对零向量给的是 NaN，同样排不出有意义的位置）。

候选集按库圈定是设计前提：每个库内几千到几万条，分批流式读出、边读边保留 top-k，
内存只占一批的量。整库（跨所有库、几十万行）扫一遍在这条路径上太慢——调用方不要
传不带库/论文范围的语句进来（每日池例外：它本身受保留窗口约束，只有几千条）。
"""

from __future__ import annotations

import heapq
import json
import math
import warnings
from collections.abc import Hashable, Iterable, Sequence
from typing import Any

from sqlalchemy import Text, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession

try:  # numpy 是主依赖；万一缺了（裸环境跑单测）就退回纯 Python，结果一致只是慢
    import numpy as np
except ImportError:  # pragma: no cover — 依赖里声明了 numpy，正常环境走不到
    np = None  # type: ignore[assignment]

#: 每批从数据库流式取多少行。1024 维 float64 一批约 16MB。
BATCH_SIZE = 2000


def uses_pgvector(session: AsyncSession) -> bool:
    """这次检索能不能交给 pgvector 在 SQL 里算（否则走本模块的 Python 打分）。"""
    return session.get_bind().dialect.name == "postgresql"


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


async def search_statement(
    session: AsyncSession,
    stmt: Any,
    query_vector: Sequence[float],
    limit: int,
    *,
    batch_size: int = BATCH_SIZE,
) -> list[tuple[Any, float]]:
    """执行候选语句，流式打分取 top-k。

    ``stmt`` 的**最后一列是向量**，前面的列组成键：只有一列键时键就是那个值，
    多列时是元组（例如 ``(chunk_id, paper_id)``）。过滤条件全在 ``stmt`` 里。
    """
    top = TopK(query_vector, limit)
    if top.limit == 0:
        return []
    result = await session.stream(stmt)
    async for partition in result.partitions(batch_size):
        top.add((row[0] if len(row) == 2 else tuple(row[:-1]), row[-1]) for row in partition)
    return top.result()
