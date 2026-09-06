"""缺口与负结果台账（#665）：entries 归一化边界、fake 确定性、库级聚合排序、
启发式矛盾配对、端点权限。"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.paper_extraction import PaperExtraction
from app.services import paper_enrich
from app.services.extraction.runtime import extract_paper, normalize_payload
from app.services.extraction.schemas import GAP_KINDS, GAPS_SCHEMA, get_schema
from app.services.gap_ledger import GapEntry, find_contradiction_pairs, library_gaps
from tests.conftest import add_paper, make_project_with_library, register_and_login

FULL_TEXT = """Gap Ledger Probe

Introduction

We study which open problems and negative results a deterministic probe reports.

Discussion

Cross-domain generalization remains an open problem. Our evaluation is limited.
"""


def _write_fulltext(tmp_path, text=FULL_TEXT):
    path = tmp_path / f"{uuid.uuid4().hex}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _entry(kind="gap", statement="s", span="span", **kw):
    return {"kind": kind, "statement": statement, "source_span": span, **kw}


# ---- 1. schema 注册 ----


def test_gaps_schema_registered():
    schema = get_schema("gaps")
    assert schema is GAPS_SCHEMA
    assert schema.version == 1
    assert schema.stage == "extract_gaps"
    prompt = schema.system_prompt()
    assert "POLARIS_EXTRACT_GAPS" in prompt
    assert '"entries"' in prompt
    # 条目键与枚举都进 prompt（与 entry_keys 定义不漂移）
    for key in schema.fields[0].entry_keys:
        assert f'"{key.name}"' in prompt
    for kind in GAP_KINDS:
        assert kind in prompt


# ---- 2. entries 归一化边界（纯函数） ----


def test_normalize_entries_boundaries():
    raw = {
        "entries": [
            _entry(),  # 合法
            _entry(kind="Gap", statement="大小写归一", span="Span"),  # kind 小写后合法
            _entry(kind="opinion"),  # 枚举外 kind → 整条丢弃
            _entry(span="   "),  # 空 span → 整条丢弃（锚定是硬要求）
            {"kind": "gap", "statement": "缺 source_span 键"},  # 缺键 → 整条丢弃
            _entry(statement="超长" * 300, span="长" * 300),  # 超长截断但保留
            "not a dict",  # 非 dict → 丢弃
            _entry(extra="白名单外的键被丢弃"),
        ],
        "hallucinated": "schema 外字段照旧被白名单丢掉",
        "confidence": 0.5,
    }
    payload, confidence = normalize_payload(GAPS_SCHEMA, raw)
    assert set(payload) == {"entries"}
    entries = payload["entries"]
    # 第 8 条与第 1 条全键相同 → 去重；剩：合法、大小写、超长截断 三条
    assert len(entries) == 3
    assert entries[0] == {"kind": "gap", "statement": "s", "source_span": "span"}
    assert entries[1]["kind"] == "gap"
    assert entries[1]["statement"] == "大小写归一"
    assert len(entries[2]["statement"]) == 300
    assert len(entries[2]["source_span"]) == 200
    assert all(set(e) == {"kind", "statement", "source_span"} for e in entries)
    assert confidence == 0.5


def test_normalize_entries_caps_item_count():
    raw = {"entries": [_entry(statement=f"条目 {i}") for i in range(12)]}
    payload, _ = normalize_payload(GAPS_SCHEMA, raw)
    assert len(payload["entries"]) == 8  # max_items 封顶


def test_normalize_entries_all_invalid_yields_empty_payload():
    raw = {"entries": [_entry(span=""), _entry(kind="nope"), 42]}
    payload, _ = normalize_payload(GAPS_SCHEMA, raw)
    assert payload == {}  # 全丢 → 空 payload → 运行时不落表


# ---- 3. fake 确定性 + enrich 钩子 ----


async def test_extract_gaps_deterministic(client, tmp_path):
    token = await register_and_login(client, email="gaps@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="gaps-proj")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Gap Ledger Probe",
            full_text_path=_write_fulltext(tmp_path),
        )
        await session.commit()
        outcome = await extract_paper(session, paper, schema_id="gaps")
        assert outcome.status == "extracted"
        await session.commit()
        entries = outcome.row.payload["entries"]
        # fake provider 固定两条：一条 gap（回显标题）+ 一条 limitation，各带假 span
        assert [e["kind"] for e in entries] == ["gap", "limitation"]
        assert "Gap Ledger Probe" in entries[0]["statement"]
        assert all(e["source_span"] for e in entries)
        assert outcome.row.stage_meta["stage"] == "extract_gaps"


async def test_enrich_hook_extracts_gaps_too(client, tmp_path):
    token = await register_and_login(client, email="gapshook@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="gapshook-proj")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Hooked Gaps Paper",
            full_text_path=_write_fulltext(tmp_path),
        )
        await session.commit()
        paper_id = paper.id

    async def _noop_emit(stage, status, detail=None):  # noqa: ARG001
        return None

    async with get_sessionmaker()() as session:
        from app.models.paper import Paper

        paper = await session.get(Paper, paper_id)
        await paper_enrich.enrich_paper(
            session, paper, target=None, user_id=None, project_id=None, emit=_noop_emit
        )

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(PaperExtraction).where(PaperExtraction.paper_id == paper_id)
                )
            )
            .scalars()
            .all()
        )
    gaps = next(row for row in rows if row.schema_id == "gaps")
    assert len(gaps.payload["entries"]) == 2


# ---- 4. 库级聚合：排序 / kind 过滤 / 回收站排除 / top 截断 ----


async def _seed_gap_rows(session, project_id, tmp_path, specs):
    """按 specs 造论文 + gaps 抽取行：specs = [(title, year, status, entries)]。"""
    papers = []
    for title, year, status, entries in specs:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title=title,
            year=year,
            status=status,
        )
        session.add(
            PaperExtraction(paper_id=paper.id, schema_id="gaps", payload={"entries": entries})
        )
        papers.append(paper)
    await session.commit()
    return papers


async def test_library_gaps_sorting_and_filters(client, tmp_path):
    token = await register_and_login(client, email="gapsagg@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="gapsagg")

    async with get_sessionmaker()() as session:
        await _seed_gap_rows(
            session,
            project_id,
            tmp_path,
            [
                # 2022 的 gap（分 2025）排在 2024 的 limitation（分 2024）前、
                # 2026 的 limitation（分 2026）后：新近度与 kind 权重互相能翻盘
                ("Old Gap", 2022, "compiled", [_entry(statement="旧缺口")]),
                (
                    "New Limitation",
                    2026,
                    "compiled",
                    [_entry(kind="limitation", statement="新局限")],
                ),
                (
                    "Mid Limitation",
                    2024,
                    "compiled",
                    [_entry(kind="limitation", statement="中局限")],
                ),
                # 回收站论文的条目不聚合
                ("Trashed", 2027, "excluded", [_entry(statement="回收站缺口")]),
                # 无年份论文沉底
                ("No Year", None, "compiled", [_entry(statement="无年份缺口")]),
            ],
        )

    async with get_sessionmaker()() as session:
        entries = await library_gaps(session, library_id)
        assert [e.statement for e in entries] == ["新局限", "旧缺口", "中局限", "无年份缺口"]
        assert all(e.statement != "回收站缺口" for e in entries)
        assert entries[0].paper_title == "New Limitation"

        # kind 过滤
        only_gaps = await library_gaps(session, library_id, kind="gap")
        assert {e.kind for e in only_gaps} == {"gap"}
        assert len(only_gaps) == 2

        # top 截断按排序取前 N
        top_two = await library_gaps(session, library_id, top=2)
        assert [e.statement for e in top_two] == ["新局限", "旧缺口"]


# ---- 5. 启发式矛盾配对（纯函数） ----


def _gap_entry(statement, paper=None, kind="gap", year=2026):
    return GapEntry(
        paper_id=paper or uuid.uuid4(),
        paper_title="t",
        kind=kind,
        statement=statement,
        source_span="span",
        year=year,
    )


def test_contradiction_pairs_positive():
    a = _gap_entry("Dropout regularization improves calibration on ImageNet")
    b = _gap_entry("Dropout regularization does not improve calibration in our runs")
    pairs = find_contradiction_pairs([a, b])
    assert len(pairs) == 1
    assert pairs[0]["heuristic"] is True
    assert "dropout" in pairs[0]["shared_terms"]
    assert {pairs[0]["a"].statement, pairs[0]["b"].statement} == {a.statement, b.statement}


def test_contradiction_pairs_cjk():
    a = _gap_entry("对比学习在小样本场景显著提升表现")
    b = _gap_entry("对比学习在小样本场景并未带来提升")
    assert len(find_contradiction_pairs([a, b])) == 1


def test_contradiction_pairs_negatives():
    # 无共享概念：都带否定极性差也不配
    x = _gap_entry("Batch normalization fails on tiny batches")
    y = _gap_entry("Curriculum ordering improves convergence speed")
    assert find_contradiction_pairs([x, y]) == []
    # 同极性（都是肯定句）不配
    p = _gap_entry("Dropout regularization improves calibration")
    q = _gap_entry("Dropout regularization improves robustness too")
    assert find_contradiction_pairs([p, q]) == []
    # 同一篇论文的两条不配（自述不算相互矛盾）
    pid = uuid.uuid4()
    m = _gap_entry("Dropout regularization improves calibration", paper=pid)
    n = _gap_entry("Dropout regularization does not improve calibration", paper=pid)
    assert find_contradiction_pairs([m, n]) == []


# ---- 6. 端点 ----


async def test_gaps_endpoint(client, tmp_path):
    token = await register_and_login(client, email="gapsapi@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="gapsapi")

    # 没抽过：空列表而非 404
    resp = await client.get(f"/api/libraries/{library_id}/gaps", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"entries": [], "pairs": []}

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Endpoint Gaps Paper",
            year=2026,
            full_text_path=_write_fulltext(tmp_path),
        )
        await session.commit()
        outcome = await extract_paper(session, paper, schema_id="gaps")
        assert outcome.status == "extracted"
        await session.commit()

    resp = await client.get(f"/api/libraries/{library_id}/gaps", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [e["kind"] for e in body["entries"]] == ["gap", "limitation"]
    assert body["entries"][0]["paper_title"] == "Endpoint Gaps Paper"
    assert body["entries"][0]["year"] == 2026
    assert body["entries"][0]["source_span"]

    # kind 过滤透传
    resp = await client.get(
        f"/api/libraries/{library_id}/gaps?kind=limitation", headers=headers
    )
    assert [e["kind"] for e in resp.json()["entries"]] == ["limitation"]
    # 枚举外 kind 直接 422（Query pattern 校验）
    resp = await client.get(f"/api/libraries/{library_id}/gaps?kind=nope", headers=headers)
    assert resp.status_code == 422

    # 未登录不可读
    anon = await client.get(f"/api/libraries/{library_id}/gaps")
    assert anon.status_code == 401


async def test_gaps_endpoint_personal_library_hidden(client):
    owner = await register_and_login(client, email="gapsowner@example.com")
    owner_headers = {"Authorization": f"Bearer {owner}"}
    resp = await client.post(
        "/api/libraries",
        json={"name": "私人缺口库", "statement": "只给自己看的方向"},
        headers=owner_headers,
    )
    assert resp.status_code == 201, resp.text
    lib_id = resp.json()["id"]

    # 创建者可读
    resp = await client.get(f"/api/libraries/{lib_id}/gaps", headers=owner_headers)
    assert resp.status_code == 200

    # 他人不可见：按不存在处理（404，不泄漏个人库存在性）
    other = await register_and_login(client, email="gapsother@example.com")
    resp = await client.get(
        f"/api/libraries/{lib_id}/gaps", headers={"Authorization": f"Bearer {other}"}
    )
    assert resp.status_code == 404
