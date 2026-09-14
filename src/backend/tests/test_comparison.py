"""论文对比表（#669）：表构建（有/无产物混合、列序、list 渲染）、条数帽、
越界论文、端点权限。全链路零 LLM——数据直接种 paper_extractions。"""

import uuid

import pytest

from app.core.db import get_sessionmaker
from app.models.paper_extraction import PaperExtraction
from app.services.comparison import (
    COMPARISON_FIELDS,
    MAX_COMPARISON_PAPERS,
    PaperNotInLibraryError,
    build_comparison,
)
from tests.conftest import add_paper, make_project_with_library, register_and_login

# 行序契约：skeleton 四字段在前，method 五字段在后（从 schema 推导，这里锚死
# 顺序——前端 CSV 导出与 vitest 对着同一份顺序断言）
EXPECTED_FIELDS = [
    "problem",
    "method",
    "findings",
    "limitations",
    "purpose",
    "mechanism",
    "baseline",
    "dataset",
    "protocol",
]


def test_comparison_fields_order():
    assert [f for f, _, _, _ in COMPARISON_FIELDS] == EXPECTED_FIELDS
    # 字段归属：前四行 skeleton，后五行 method
    assert [s for _, _, s, _ in COMPARISON_FIELDS] == ["skeleton"] * 4 + ["method"] * 5


async def _seed_papers(session, project_id, specs):
    """按 specs 造论文：specs = [(title, year, status, {schema_id: payload})]。"""
    papers = []
    for title, year, status, extractions in specs:
        paper = await add_paper(
            session, project_id=uuid.UUID(project_id), title=title, year=year, status=status
        )
        for schema_id, payload in extractions.items():
            session.add(
                PaperExtraction(paper_id=paper.id, schema_id=schema_id, payload=payload)
            )
        papers.append(paper)
    await session.commit()
    return papers


SKELETON_PAYLOAD = {
    "problem": "问题 A",
    "method": "方法 A",
    "findings": ["发现一", "发现二"],
    "limitations": ["局限一"],
}
METHOD_PAYLOAD = {
    "purpose": "目的 A",
    "mechanism": "机制 A",
    "baseline": ["B1", "B2"],
    "dataset": ["D1"],
    "protocol": "流程 A",
}


# ---- 1. 表构建：有/无产物混合、列序、list 渲染 ----


async def test_build_comparison_mixed(client):
    token = await register_and_login(client, email="cmp@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="cmp")

    async with get_sessionmaker()() as session:
        full, partial, blank = await _seed_papers(
            session,
            project_id,
            [
                (
                    "Full Paper",
                    2026,
                    "compiled",
                    {"skeleton": SKELETON_PAYLOAD, "method": METHOD_PAYLOAD},
                ),
                # 只抽过 skeleton，且 payload 缺 limitations 键（归一化丢空字段的存量形态）
                (
                    "Partial Paper",
                    2024,
                    "compiled",
                    {"skeleton": {"problem": "问题 B", "method": "方法 B", "findings": []}},
                ),
                ("Blank Paper", None, "compiled", {}),
            ],
        )
        ids = [full.id, partial.id, blank.id]

    async with get_sessionmaker()() as session:
        # 列序跟请求走：故意用 blank, full, partial 的顺序
        table = await build_comparison(session, library_id, [ids[2], ids[0], ids[1]])

    assert [p.title for p in table.papers] == ["Blank Paper", "Full Paper", "Partial Paper"]
    assert [p.year for p in table.papers] == [None, 2026, 2024]
    assert [r.field for r in table.rows] == EXPECTED_FIELDS
    by_field = {r.field: r for r in table.rows}

    # list 字段渲染成中文分号串
    findings = by_field["findings"]
    assert findings.cells[1].value == "发现一；发现二"
    assert findings.cells[1].present is True
    assert findings.cells[1].schema_id == "skeleton"
    assert findings.cells[1].extracted_at is not None

    # 没抽过：present=False 且 extracted_at=None（前端据此显示「未抽取」）
    assert findings.cells[0].present is False
    assert findings.cells[0].value is None
    assert findings.cells[0].extracted_at is None

    # 抽过但字段为空（findings=[] / 缺 limitations 键）：present=False，extracted_at 保留
    assert findings.cells[2].present is False
    assert findings.cells[2].extracted_at is not None
    assert by_field["limitations"].cells[2].present is False

    # method 行对只抽 skeleton 的论文是「没抽过」
    purpose = by_field["purpose"]
    assert purpose.cells[1].value == "目的 A"
    assert purpose.cells[2].present is False and purpose.cells[2].extracted_at is None

    # 每行 cells 与 papers 同长同序
    assert all(len(r.cells) == 3 for r in table.rows)


async def test_build_comparison_dedupes_preserving_order(client):
    token = await register_and_login(client, email="cmpdedupe@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="cmpdedupe")

    async with get_sessionmaker()() as session:
        a, b = await _seed_papers(
            session,
            project_id,
            [("Paper A", 2026, "compiled", {}), ("Paper B", 2025, "compiled", {})],
        )
        ids = [a.id, b.id]

    async with get_sessionmaker()() as session:
        table = await build_comparison(session, library_id, [ids[1], ids[0], ids[1]])
    assert [p.title for p in table.papers] == ["Paper B", "Paper A"]


# ---- 2. 条数帽 / 越界论文 ----


async def test_build_comparison_caps_paper_count(client):
    token = await register_and_login(client, email="cmpcap@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    _, library_id = await make_project_with_library(client, headers, name="cmpcap")

    async with get_sessionmaker()() as session:
        with pytest.raises(ValueError):
            await build_comparison(
                session, library_id, [uuid.uuid4() for _ in range(MAX_COMPARISON_PAPERS + 1)]
            )


async def test_build_comparison_rejects_foreign_and_trashed_papers(client):
    token = await register_and_login(client, email="cmpforeign@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="cmpforeign")

    async with get_sessionmaker()() as session:
        member, trashed = await _seed_papers(
            session,
            project_id,
            [
                ("Member Paper", 2026, "compiled", {}),
                # 回收站论文与库外论文同罪：不该出现在对比里
                ("Trashed Paper", 2026, "excluded", {}),
            ],
        )
        member_id, trashed_id = member.id, trashed.id

    async with get_sessionmaker()() as session:
        with pytest.raises(PaperNotInLibraryError):
            await build_comparison(session, library_id, [member_id, uuid.uuid4()])
        with pytest.raises(PaperNotInLibraryError):
            await build_comparison(session, library_id, [member_id, trashed_id])


# ---- 3. 端点：形状 / 校验 / 权限 ----


async def test_comparison_endpoint(client):
    token = await register_and_login(client, email="cmpapi@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="cmpapi")

    async with get_sessionmaker()() as session:
        extracted, raw = await _seed_papers(
            session,
            project_id,
            [
                (
                    "Extracted Paper",
                    2026,
                    "compiled",
                    {"skeleton": SKELETON_PAYLOAD, "method": METHOD_PAYLOAD},
                ),
                ("Raw Paper", 2025, "compiled", {}),
            ],
        )
        ids = [str(extracted.id), str(raw.id)]

    resp = await client.post(
        f"/api/libraries/{library_id}/comparison", json={"paper_ids": ids}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [p["title"] for p in body["papers"]] == ["Extracted Paper", "Raw Paper"]
    assert [r["field"] for r in body["rows"]] == EXPECTED_FIELDS
    problem = body["rows"][0]
    assert problem["schema_id"] == "skeleton"
    assert problem["cells"][0] == {
        "value": "问题 A",
        "present": True,
        "schema_id": "skeleton",
        "extracted_at": problem["cells"][0]["extracted_at"],
    }
    assert problem["cells"][0]["extracted_at"] is not None
    # 未抽取论文：present=False，前端显示「未抽取」而非空白
    assert problem["cells"][1]["present"] is False
    assert problem["cells"][1]["value"] is None

    # 条数校验：<2 或 >10 都是 422（body 层）
    resp = await client.post(
        f"/api/libraries/{library_id}/comparison", json={"paper_ids": [ids[0]]}, headers=headers
    )
    assert resp.status_code == 422
    resp = await client.post(
        f"/api/libraries/{library_id}/comparison",
        json={"paper_ids": [str(uuid.uuid4()) for _ in range(11)]},
        headers=headers,
    )
    assert resp.status_code == 422

    # 越界论文 → 404
    resp = await client.post(
        f"/api/libraries/{library_id}/comparison",
        json={"paper_ids": [ids[0], str(uuid.uuid4())]},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "PAPER_NOT_FOUND"

    # 未登录不可读
    anon = await client.post(
        f"/api/libraries/{library_id}/comparison", json={"paper_ids": ids}
    )
    assert anon.status_code == 401


async def test_comparison_endpoint_personal_library_hidden(client):
    owner = await register_and_login(client, email="cmpowner@example.com")
    owner_headers = {"Authorization": f"Bearer {owner}"}
    resp = await client.post(
        "/api/libraries",
        json={"name": "私人对比库", "statement": "只给自己看的方向"},
        headers=owner_headers,
    )
    assert resp.status_code == 201, resp.text
    lib_id = resp.json()["id"]

    fake_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    # 创建者可达（论文不存在 → 404 PAPER_NOT_FOUND，但库本身可见）
    resp = await client.post(
        f"/api/libraries/{lib_id}/comparison", json={"paper_ids": fake_ids}, headers=owner_headers
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "PAPER_NOT_FOUND"

    # 他人不可见：按库不存在处理（LIBRARY_NOT_FOUND，不泄漏个人库存在性）
    other = await register_and_login(client, email="cmpother@example.com")
    resp = await client.post(
        f"/api/libraries/{lib_id}/comparison",
        json={"paper_ids": fake_ids},
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "LIBRARY_NOT_FOUND"


# ---- 对比表与缺口台账按库的学科走（#790）----


def test_comparison_rows_are_builtin_only_without_a_discipline():
    """没声明学科的库，行集与学科包出现之前完全一致。"""
    from app.services.comparison import comparison_fields

    schema_ids = {schema_id for _n, _k, schema_id, _l in comparison_fields(None)}
    # 缺口台账不进对比表：逐条挂原文出处的清单横向并排读没有意义
    assert schema_ids == {"skeleton", "method"}


def test_comparison_rows_pick_up_the_declared_discipline():
    """结构工程库该比的是 构件/作用/分析/验证，而不是它根本不抽的 baseline/dataset。"""
    from app.services.comparison import comparison_fields

    names = {name for name, _k, _s, _l in comparison_fields("structural")}
    assert {"structure", "actions", "analysis", "validation"} <= names


def test_comparison_rows_carry_the_packs_label():
    """行按字段名键控；``structure`` 这种机器名当行头读起来像半成品。"""
    from app.services.comparison import comparison_fields

    labels = {name: label for name, _k, _s, label in comparison_fields("structural")}
    assert labels["structure"] == "结构对象"


def test_the_skeleton_stays_the_first_rows():
    """骨架是任何学科都答得上的四问，排在最前是有意的阅读顺序。"""
    from app.services.comparison import comparison_fields

    first = comparison_fields("structural")[0]
    assert first[2] == "skeleton"


def test_comparison_fields_is_computed_per_call_not_at_import():
    """模块级常量必然早于任何包装载——这正是旧代码漏掉学科字段的原因。"""
    from app.services import comparison as comparison_module
    from app.services import discipline_packs as dp

    before = {name for name, _k, _s, _l in comparison_module.comparison_fields("latecomer")}
    assert "widget" not in before

    dp.register_pack(
        dp.parse_pack(
            {
                "name": "latecomer",
                "title": "迟到的包",
                "schemas": [
                    {
                        "id": "method",
                        "prompt": "抽取：\n{fields_spec}\n只输出 JSON。",
                        "fields": [
                            {"name": "purpose", "kind": "text", "max_len": 200},
                            {"name": "mechanism", "kind": "text", "max_len": 200},
                            {"name": "widget", "kind": "text", "max_len": 200},
                        ],
                    }
                ],
            },
            origin="t",
        )
    )
    after = {name for name, _k, _s, _l in comparison_module.comparison_fields("latecomer")}
    assert "widget" in after, "注册后仍看不到 = 清单被冻在 import 期了"


async def test_the_comparison_table_of_a_discipline_library_uses_its_axes(client):
    """端到端：走一遍真的对比接口。"""
    import uuid as _uuid

    from app.core.db import get_sessionmaker
    from app.models.library_direction import DirectionLibrary
    from app.models.paper_extraction import PaperExtraction
    from app.services import comparison as comparison_service
    from tests.conftest import add_paper, make_project_with_library, register_and_login

    token = await register_and_login(client, email="cmp@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="cmp")

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, library_id)
        library.discipline = "structural"
        paper = await add_paper(
            session,
            project_id=_uuid.UUID(project_id),
            title="Blast response",
            status="scored",
        )
        session.add(
            PaperExtraction(
                paper_id=paper.id,
                schema_id="structural.method",
                payload={
                    "purpose": "p",
                    "mechanism": "m",
                    "structure": "steel concrete composite beam",
                },
            )
        )
        await session.commit()
        paper_id = paper.id

    async with get_sessionmaker()() as session:
        table = await comparison_service.build_comparison(session, library_id, [paper_id])

    by_field = {row.field: row for row in table.rows}
    assert "structure" in by_field, "学科字段连行都没有"
    assert by_field["structure"].label == "结构对象"
    assert by_field["structure"].cells[0].value == "steel concrete composite beam"
    assert by_field["structure"].cells[0].present is True


async def test_the_gap_ledger_reads_a_disciplines_own_gap_card(client):
    """包格式允许 ``<包名>.gaps``；写死内置 id 的话它抽出来了却静默不进台账。"""
    import uuid as _uuid

    from app.core.db import get_sessionmaker
    from app.models.library_direction import DirectionLibrary
    from app.models.paper_extraction import PaperExtraction
    from app.services import discipline_packs as dp
    from app.services import gap_ledger
    from tests.conftest import add_paper, make_project_with_library, register_and_login

    dp.register_pack(
        dp.parse_pack(
            {
                "name": "structural",
                "title": "结构工程",
                "schemas": [
                    {
                        "id": "gaps",
                        "prompt": "抽取：\n{fields_spec}\n只输出 JSON。",
                        "fields": [
                            {
                                "name": "entries",
                                "kind": "entries",
                                "max_items": 5,
                                "entry_keys": [
                                    {"name": "kind", "max_len": 32},
                                    {"name": "statement", "max_len": 200},
                                ],
                            }
                        ],
                    }
                ],
            },
            origin="t",
        )
    )

    token = await register_and_login(client, email="gapdisc@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="gapd")

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, library_id)
        library.discipline = "structural"
        paper = await add_paper(
            session,
            project_id=_uuid.UUID(project_id),
            title="Blast gaps",
            status="scored",
            year=2026,
        )
        session.add(
            PaperExtraction(
                paper_id=paper.id,
                schema_id="structural.gaps",
                payload={
                    "entries": [
                        {
                            "kind": "gap",
                            "statement": "接缝滞回规律未知",
                            "source_span": "原文：节点滞回性能尚缺系统试验",
                        }
                    ]
                },
            )
        )
        await session.commit()

    async with get_sessionmaker()() as session:
        entries = await gap_ledger.library_gaps(session, library_id)
    assert [e.statement for e in entries] == ["接缝滞回规律未知"]
