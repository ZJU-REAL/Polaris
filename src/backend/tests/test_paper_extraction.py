"""schema 引导的骨架抽取（#661）：归一化边界、运行时确定性、幂等、钩子与端点。"""

import uuid

from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.models.paper import Paper
from app.models.paper_extraction import PaperExtraction
from app.services import paper_enrich
from app.services.extraction.runtime import extract_paper, normalize_payload
from app.services.extraction.schemas import SKELETON_SCHEMA, get_schema, list_schemas
from tests.conftest import add_paper, make_project_with_library, register_and_login

FULL_TEXT = """Deterministic Extraction Probe

Introduction

We study how deterministic probes behave under schema-guided extraction.

Method

A fake but structured method section with enough prose to look like a paper.

Results

The probe extracts a stable skeleton every time.
"""


async def _noop_emit(stage, status, detail=None):  # noqa: ARG001
    return None


def _write_fulltext(tmp_path, text=FULL_TEXT):
    path = tmp_path / f"{uuid.uuid4().hex}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


async def _extraction_rows(paper_id):
    async with get_sessionmaker()() as session:
        return (
            (
                await session.execute(
                    select(PaperExtraction).where(PaperExtraction.paper_id == paper_id)
                )
            )
            .scalars()
            .all()
        )


# ---- 1. schema 注册表 ----


def test_skeleton_schema_registered():
    schema = get_schema("skeleton")
    assert schema is SKELETON_SCHEMA
    assert schema.version == 1
    assert [f.name for f in schema.fields] == ["problem", "method", "findings", "limitations"]
    assert schema in list_schemas()
    # prompt 由 schema 生成：字段清单与 fields 定义不漂移
    prompt = schema.system_prompt()
    assert "POLARIS_EXTRACT_SKELETON" in prompt
    for field in schema.fields:
        assert f'"{field.name}"' in prompt


def test_unknown_schema_raises():
    try:
        get_schema("nope")
    except ValueError as e:
        assert "nope" in str(e)
    else:
        raise AssertionError("unknown schema must raise")


# ---- 2. 归一化边界（纯函数） ----


def test_normalize_caps_dedupes_and_drops_empty():
    raw = {
        "problem": "  x" + "长" * 2000,  # 超长截断
        "method": "   ",  # 空白 → 不入 payload
        "findings": [
            "  重复的发现  ",
            "重复的发现",  # strip 后重复 → 去重
            "",
            123,  # 非字符串 → 丢弃
            "发现 A",
            "发现 B",
            "发现 C",
            "发现 D",
            "发现 E",  # 超过 max_items=5 → 截掉
        ],
        "limitations": [],  # 空列表 → 不入 payload
        "hallucinated_field": "模型自作主张的键必须被白名单丢掉",
        "confidence": 3.5,  # 越界 → 夹到 1.0
    }
    payload, confidence = normalize_payload(SKELETON_SCHEMA, raw)
    assert set(payload) == {"problem", "findings"}
    assert len(payload["problem"]) == 800  # max_len 帽
    assert payload["findings"] == ["重复的发现", "发现 A", "发现 B", "发现 C", "发现 D"]
    assert confidence == 1.0


def test_normalize_accepts_bare_string_as_single_item_list():
    payload, confidence = normalize_payload(
        SKELETON_SCHEMA, {"findings": "单条发现给成了字符串"}
    )
    assert payload == {"findings": ["单条发现给成了字符串"]}
    assert confidence is None  # 模型没给就空着，不编数


def test_normalize_all_empty_yields_empty_payload():
    payload, _ = normalize_payload(
        SKELETON_SCHEMA, {"problem": "", "method": None, "findings": [], "limitations": ["  "]}
    )
    assert payload == {}


# ---- 3. 运行时：fake provider 确定性 + 幂等 UPSERT + 无正文 skip ----


async def test_extract_deterministic_and_idempotent(client, tmp_path):
    token = await register_and_login(client, email="extract@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="extract-proj")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Deterministic Extraction Probe",
            abstract="A probe.",
            full_text_path=_write_fulltext(tmp_path),
        )
        await session.commit()
        outcome = await extract_paper(session, paper)
        assert outcome.status == "extracted"
        await session.commit()
        row = outcome.row
        assert row.schema_id == "skeleton"
        # fake provider：problem 回显标题（断言 prompt 里带对了论文）
        assert "Deterministic Extraction Probe" in row.payload["problem"]
        assert row.payload["method"]
        assert len(row.payload["findings"]) == 2
        assert len(row.payload["limitations"]) == 1
        assert row.confidence == 0.9
        assert row.stage_meta["stage"] == "extract_skeleton"
        assert row.stage_meta["version"] == 1
        assert row.stage_meta["model"]

        # 幂等：已有产物直接跳过，不重复调用也不再落行
        again = await extract_paper(session, paper)
        assert again.status == "skipped"
        assert again.reason == "already extracted"

        # force：覆盖重抽仍是同一行（UPSERT 语义，唯一约束兜底）
        forced = await extract_paper(session, paper, force=True)
        assert forced.status == "extracted"
        assert forced.row.id == row.id
        await session.commit()
        count = await session.scalar(
            select(func.count())
            .select_from(PaperExtraction)
            .where(PaperExtraction.paper_id == paper.id)
        )
        assert count == 1


async def test_extract_skips_without_fulltext(client):
    token = await register_and_login(client, email="extractskip@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="extractskip-proj")

    async with get_sessionmaker()() as session:
        # bibtex 导入形态：有标题摘要、无全文——如实 skip，不许拿摘要冒充正文
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="No Fulltext Paper",
            abstract="Abstract only.",
        )
        await session.commit()
        paper_id = paper.id
        outcome = await extract_paper(session, paper)
        assert outcome.status == "skipped"
        assert outcome.reason == "no full text"
    assert await _extraction_rows(paper_id) == []


async def test_extract_unknown_schema_raises(client, tmp_path):
    token = await register_and_login(client, email="extractbad@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="extractbad-proj")
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Bad Schema Paper",
            full_text_path=_write_fulltext(tmp_path),
        )
        await session.commit()
        try:
            await extract_paper(session, paper, schema_id="nope")
        except ValueError:
            pass
        else:
            raise AssertionError("unknown schema must raise, not skip")


# ---- 4. 增量钩子：enrich_paper 顺带抽骨架 ----


async def test_enrich_hook_extracts_skeleton(client, tmp_path):
    token = await register_and_login(client, email="extracthook@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="extracthook-proj")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Hooked Extraction Paper",
            full_text_path=_write_fulltext(tmp_path),
        )
        await session.commit()
        paper_id = paper.id

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        await paper_enrich.enrich_paper(
            session, paper, target=None, user_id=None, project_id=None, emit=_noop_emit
        )

    # #663/#665 起钩子把注册表里的每个 schema 都抽一遍：骨架 + 方法卡 + 缺口台账各一行
    rows = sorted(await _extraction_rows(paper_id), key=lambda r: r.schema_id)
    assert [r.schema_id for r in rows] == ["gaps", "method", "skeleton"]
    assert "Hooked Extraction Paper" in rows[2].payload["problem"]
    assert rows[1].payload["purpose"]
    assert rows[0].payload["entries"]


async def test_enrich_hook_zero_output_without_fulltext(client):
    """golden 保全的机制性验证：无全文的论文走完 enrich 链不产生任何抽取行。"""
    token = await register_and_login(client, email="extracthook2@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="extracthook2-proj")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Bibtex Style Paper",
            abstract="No pdf, no full text.",
        )
        await session.commit()
        paper_id = paper.id

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        await paper_enrich.enrich_paper(
            session, paper, target=None, user_id=None, project_id=None, emit=_noop_emit
        )
    assert await _extraction_rows(paper_id) == []


# ---- 5. 端点 ----


async def test_extractions_endpoint(client, tmp_path):
    token = await register_and_login(client, email="extractapi@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _ = await make_project_with_library(client, headers, name="extractapi-proj")

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Endpoint Extraction Paper",
            full_text_path=_write_fulltext(tmp_path),
        )
        await session.commit()
        paper_id = paper.id

    # 没抽过：空列表而非 404（前端折叠区据此显示「还没有」）
    resp = await client.get(f"/api/papers/{paper_id}/extractions", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        outcome = await extract_paper(session, paper)
        assert outcome.status == "extracted"
        await session.commit()

    resp = await client.get(f"/api/papers/{paper_id}/extractions", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    item = body[0]
    assert item["schema_id"] == "skeleton"
    assert "Endpoint Extraction Paper" in item["payload"]["problem"]
    assert item["confidence"] == 0.9
    assert item["stage_meta"]["stage"] == "extract_skeleton"
    assert item["updated_at"]

    # 未登录不可读
    anon = await client.get(f"/api/papers/{paper_id}/extractions")
    assert anon.status_code == 401
