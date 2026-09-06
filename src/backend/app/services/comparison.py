"""论文对比表（#669，设计报告 §10 ④层）。

刻意只做**定向表格**，不做整篇综述：行 = skeleton@1 + method@1 的九个抽取字段，
列 = 用户挑的论文（≤10 篇）。全部数据来自 paper_extractions 的存量产物——
本文件零 LLM 调用，判断性工作（逐篇抽字段）已在抽取环节做完，对比层只是
查表 + 摆盘。这样表格秒开、免费、可复算；「先抽后比」也让每个 cell 都能
指回它的抽取来源（schema_id + extracted_at），综述式的一锅炖给不了这种溯源。

没抽过的论文不挡对比：cell 标 present=False，前端显示「未抽取」并提示去论文
详情抽——比起悄悄跳过该论文或整表报错，摆一列空位让用户看见缺口在哪。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import LibraryPaper
from app.models.paper import Paper
from app.models.paper_extraction import PaperExtraction
from app.services.extraction.schemas import METHOD_SCHEMA, SKELETON_SCHEMA

# 对比上限：表格是横向并排读的，10 列已经到「一屏放不下、肉眼对不过来」的边缘；
# 更多论文的横向归纳属于综述场景，本批次刻意不做（见模块 docstring）。
MAX_COMPARISON_PAPERS = 10

# 行序 = skeleton 四字段在前（任何学科都答得上的骨架），method 五字段在后
# （复现要素）。直接从 schema 定义推导，字段集演进时对比表自动跟上，不留手抄副本。
COMPARISON_FIELDS: tuple[tuple[str, str, str], ...] = tuple(
    (field.name, field.kind, schema.id)
    for schema in (SKELETON_SCHEMA, METHOD_SCHEMA)
    for field in schema.fields
)


class PaperNotInLibraryError(LookupError):
    """请求的论文不在该库（或在回收站）：API 层按 404 处理，不泄漏内容池里是否存在。"""


@dataclass(slots=True)
class ComparisonCell:
    """一个格子：值 + 是否抽到 + 出处（哪个 schema、何时抽的）。

    present=False 覆盖两种情况：该论文没跑过这个 schema（extracted_at 为 None），
    或跑过但该字段归一化后为空（extracted_at 保留）。前端统一显示「未抽取」，
    extracted_at 留给需要细究的读者。
    """

    value: str | None
    present: bool
    schema_id: str
    extracted_at: datetime | None


@dataclass(slots=True)
class ComparisonRow:
    field: str
    schema_id: str
    cells: list[ComparisonCell]


@dataclass(slots=True)
class ComparisonPaper:
    paper_id: uuid.UUID
    title: str
    year: int | None


@dataclass(slots=True)
class ComparisonTable:
    papers: list[ComparisonPaper]
    rows: list[ComparisonRow]


def _render_value(raw: object, kind: str) -> str | None:
    """把抽取产物的一个字段拍成展示串：text 原样，list 用中文分号串起来。

    拍成串而不是把 list 透传给前端：对比表和 CSV 导出都按「一格一段文字」
    消费，渲染口径收在后端一处，两端不会各拼各的。
    """
    if kind == "list":
        if not isinstance(raw, list) or not raw:
            return None
        return "；".join(str(item) for item in raw if str(item).strip()) or None
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


async def build_comparison(
    session: AsyncSession,
    library_id: uuid.UUID,
    paper_ids: list[uuid.UUID],
) -> ComparisonTable:
    """构建对比表：列序 = 请求顺序（去重后），行序 = COMPARISON_FIELDS。

    列序跟请求走是刻意的——用户在列表里勾选的顺序就是他想并排看的顺序，
    CSV 导出也依赖这份稳定顺序。成员口径与缺口台账一致：非回收站论文。

    Raises:
        ValueError: 超过 MAX_COMPARISON_PAPERS 篇（API 层由 body 校验先挡，这里兜底）。
        PaperNotInLibraryError: 有论文不属于该库或在回收站。
    """
    # 去重但保序：同一篇勾两次不该出两列，也不该打乱其余列的顺序
    ordered_ids = list(dict.fromkeys(paper_ids))
    if len(ordered_ids) > MAX_COMPARISON_PAPERS:
        raise ValueError(f"comparison supports at most {MAX_COMPARISON_PAPERS} papers")

    rows = (
        (
            await session.execute(
                select(Paper)
                .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
                .where(
                    LibraryPaper.library_id == library_id,
                    LibraryPaper.status != "excluded",
                    Paper.id.in_(ordered_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {paper.id: paper for paper in rows}
    missing = [pid for pid in ordered_ids if pid not in by_id]
    if missing:
        raise PaperNotInLibraryError(f"papers not in library: {missing}")

    schema_ids = {SKELETON_SCHEMA.id, METHOD_SCHEMA.id}
    extractions = (
        (
            await session.execute(
                select(PaperExtraction).where(
                    PaperExtraction.paper_id.in_(ordered_ids),
                    PaperExtraction.schema_id.in_(schema_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    # (paper_id, schema_id) 唯一（表约束），直接建索引
    by_key = {(row.paper_id, row.schema_id): row for row in extractions}

    table_rows: list[ComparisonRow] = []
    for field_name, kind, schema_id in COMPARISON_FIELDS:
        cells: list[ComparisonCell] = []
        for pid in ordered_ids:
            extraction = by_key.get((pid, schema_id))
            value = (
                _render_value(extraction.payload.get(field_name), kind)
                if extraction is not None
                else None
            )
            cells.append(
                ComparisonCell(
                    value=value,
                    present=value is not None,
                    schema_id=schema_id,
                    extracted_at=extraction.updated_at if extraction is not None else None,
                )
            )
        table_rows.append(ComparisonRow(field=field_name, schema_id=schema_id, cells=cells))

    return ComparisonTable(
        papers=[
            ComparisonPaper(paper_id=pid, title=by_id[pid].title, year=by_id[pid].year)
            for pid in ordered_ids
        ],
        rows=table_rows,
    )
