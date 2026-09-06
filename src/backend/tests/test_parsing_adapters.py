"""解析双轨适配器（#650）：TEI/MinerU fixture 映射、选优裁决矩阵、
适配器不可用 = 现状逐字节不变、citation_graph 双输入源等价。全部 fixture，不打真网。"""

import json
import uuid
from pathlib import Path

import pymupdf
import respx
from httpx import Response
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models.paper import new_paper
from app.models.paper_citation import PaperCitation
from app.services.citation_graph import ensure_citation_edges
from app.services.parsing.contract import ParsedRef, ParsedSection, ParseResult
from app.services.parsing.grobid import GrobidAdapter, parse_tei
from app.services.parsing.mineru import MineruWebAdapter, _markdown_of, parse_mineru_markdown
from app.services.parsing.select import (
    apply_missing_metadata,
    dual_track_available,
    extract_with_dual_track,
    load_structured_references,
    parse_quality_path,
    select_parse,
    structured_references_path,
)

FIXTURES = Path(__file__).parent / "fixtures"
TEI_SAMPLE = (FIXTURES / "grobid_tei_sample.xml").read_text(encoding="utf-8")
MINERU_SAMPLE = json.loads((FIXTURES / "mineru_web_sample.json").read_text(encoding="utf-8"))

# 与 test_citation_intent.FULL_TEXT 同构的正则可解析全文（编号文献表 + 正文 [n] 标记）
FULL_TEXT = """Deep Agent Survey

Introduction

Early systems established the paradigm of tool-augmented reasoning [1].
We follow the planning method of [2] to build our agent loop.
Our results are compared against the strong baseline of [3].

References

[1] Alice Zhang. Foundations of Tool-Augmented Reasoning Systems. Journal of AI, 2020.
[2] Bob Li. Planning With Language Models For Agent Tasks. NeurIPS, 2021.
[3] Carol Wei. A Strong Baseline For Agent Benchmarks. ICML, 2022.
"""


def _make_pdf(tmp_path: Path, text: str = "hello parsing adapters") -> Path:
    pdf_path = tmp_path / "sample.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(pdf_path)
    doc.close()
    return pdf_path


# ---- GROBID：TEI fixture → ParseResult 映射 ----


def test_grobid_tei_mapping():
    result = parse_tei(TEI_SAMPLE)
    assert result is not None
    assert result.metadata["title"] == "Deep Agent Survey"
    assert result.metadata["authors"] == ["Alice Zhang", "Bob Li"]
    assert result.metadata["year"] == 2024
    assert result.metadata["venue"] == "Journal of AI Research"
    assert result.metadata["doi"] == "10.1234/agent.survey"
    assert "survey deep agents" in result.metadata["abstract"]

    assert [s.heading for s in result.sections] == ["Introduction", "Experiments"]
    assert "planning method" in result.sections[0].text

    assert len(result.references) == 3
    # raw_reference 原文优先；没有时由 作者/(年份)/标题 组装
    assert result.references[0].raw.startswith("Alice Zhang. Foundations")
    assert result.references[1].raw == (
        "Bob Li. (2021). Planning With Language Models For Agent Tasks"
    )
    assert result.references[1].doi == "10.5555/planning"
    assert result.references[2].title == "A Strong Baseline For Agent Benchmarks"
    assert result.references[2].year == 2022
    # 上下文句：含 <ref type="bibr"> 标记的整句
    assert result.references[0].contexts == [
        "Early systems established the paradigm of tool-augmented reasoning [1]."
    ]
    assert result.references[1].contexts == [
        "We follow the planning method of [2] to build our agent loop."
    ]
    assert result.references[2].contexts == [
        "Our results are compared against the strong baseline of [3]."
    ]
    assert 0 < result.quality.coverage <= 1


def test_grobid_unparseable_tei_returns_none():
    assert parse_tei("this is not xml <<<") is None
    assert parse_tei("<TEI xmlns='http://www.tei-c.org/ns/1.0'></TEI>") is None


@respx.mock
async def test_grobid_adapter_posts_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "grobid_url", "http://grobid.test", raising=False)
    route = respx.post("http://grobid.test/api/processFulltextDocument").mock(
        return_value=Response(200, text=TEI_SAMPLE)
    )
    result = await GrobidAdapter().parse(_make_pdf(tmp_path))
    assert route.called
    assert result is not None and result.metadata["title"] == "Deep Agent Survey"


# ---- MinerU：markdown fixture → ParseResult 映射 ----


def test_mineru_markdown_mapping():
    result = parse_mineru_markdown(_markdown_of(MINERU_SAMPLE))
    assert result is not None
    assert [s.heading for s in result.sections] == ["Deep Agent Survey", "Method", "Results"]
    assert "Intro paragraph" in result.sections[0].text
    body = result.body_text()
    assert body.startswith("Deep Agent Survey")
    assert "Closing remarks" in body
    # 表格：markdown 竖线表 + html 表都收；公式收 $$..$$
    kinds = sorted(k for t in result.tables for k in t)
    assert kinds == ["html", "markdown"]
    assert result.formulas == [{"latex": "E = mc^2"}]
    assert 0 < result.quality.coverage <= 1


def test_mineru_payload_shapes():
    # 不同部署的响应键名（fixture 锁定的候选键顺序）
    assert _markdown_of({"md_content": "# A"}) == "# A"
    assert _markdown_of({"markdown": "# B"}) == "# B"
    assert _markdown_of({"data": {"md_content": "# C"}}) == "# C"
    assert _markdown_of("# raw text") == "# raw text"
    assert _markdown_of({"other": 1}) == ""
    assert parse_mineru_markdown("") is None


@respx.mock
async def test_mineru_adapter_posts_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "mineru_parse_url", "http://mineru.test", raising=False)
    route = respx.post("http://mineru.test/file_parse").mock(
        return_value=Response(200, json=MINERU_SAMPLE)
    )
    result = await MineruWebAdapter().parse(_make_pdf(tmp_path))
    assert route.called
    assert result is not None and result.body_text().startswith("Deep Agent Survey")


# ---- 选优裁决矩阵（三种可用性组合） ----

_GROBID_RESULT = ParseResult(
    metadata={"title": "G Title", "year": 2024, "doi": "10.1/x"},  # 缺 venue/abstract/authors
    references=[ParsedRef(raw="G ref one", contexts=["cited here."])],
)
_MINERU_RESULT = ParseResult(sections=[ParsedSection(heading="H", text="mineru body")])
_FALLBACK_META = {
    "title": "Fallback Title",
    "authors": ["F Author"],
    "year": 2000,
    "venue": "Fallback Venue",
    "doi": None,
    "abstract": "fallback abstract",
}


def test_select_both_tracks():
    outcome = select_parse(
        grobid=_GROBID_RESULT,
        mineru=_MINERU_RESULT,
        fallback_text="fallback full text",
        fallback_metadata=_FALLBACK_META,
    )
    # metadata：GROBID 有值的字段用 GROBID，缺的字段回退现有值
    assert outcome.metadata["title"] == "G Title"
    assert outcome.metadata["year"] == 2024
    assert outcome.metadata["doi"] == "10.1/x"
    assert outcome.metadata["venue"] == "Fallback Venue"
    assert outcome.metadata["abstract"] == "fallback abstract"
    assert outcome.metadata["authors"] == ["F Author"]
    assert outcome.references == [
        {
            "raw": "G ref one",
            "title": None,
            "authors": [],
            "year": None,
            "doi": None,
            "contexts": ["cited here."],
        }
    ]
    assert outcome.body == "H\n\nmineru body"
    assert outcome.quality_report["decisions"] == {
        "metadata": "grobid",
        "references": "grobid",
        "body": "mineru",
    }
    assert outcome.quality_report["counts"]["references"] == 1


def test_select_grobid_only():
    outcome = select_parse(
        grobid=_GROBID_RESULT,
        mineru=None,
        fallback_text="fallback full text",
        fallback_metadata=_FALLBACK_META,
    )
    assert outcome.body == "fallback full text"
    assert outcome.references is not None
    assert outcome.quality_report["decisions"] == {
        "metadata": "grobid",
        "references": "grobid",
        "body": "fallback",
    }


def test_select_mineru_only():
    outcome = select_parse(
        grobid=None,
        mineru=_MINERU_RESULT,
        fallback_text="fallback full text",
        fallback_metadata=_FALLBACK_META,
    )
    assert outcome.metadata["title"] == "Fallback Title"
    assert outcome.references is None  # 引文建边保持正则路径
    assert outcome.body == "H\n\nmineru body"
    assert outcome.quality_report["decisions"] == {
        "metadata": "fallback",
        "references": "fallback",
        "body": "mineru",
    }


def test_select_grobid_zero_references_falls_back():
    outcome = select_parse(
        grobid=ParseResult(metadata={"title": "G"}),  # 条目数 0
        mineru=None,
        fallback_text="text",
        fallback_metadata=_FALLBACK_META,
    )
    assert outcome.references is None
    assert outcome.quality_report["decisions"]["references"] == "fallback"


# ---- 适配器不可用 = 现状逐字节不变（golden 保全路径） ----


async def test_extract_without_adapters_is_byte_identical(tmp_path):
    from app.services.literature.pdf_extract import extract_full_text

    assert not dual_track_available()  # 测试套件不配任何解析 URL
    pdf_path = _make_pdf(tmp_path)
    legacy_id, dual_id = uuid.uuid4().hex, uuid.uuid4().hex
    legacy_txt = await extract_full_text(legacy_id, pdf_path)
    outcome = await extract_with_dual_track(dual_id, pdf_path)
    assert not outcome.used_adapters
    assert outcome.txt_path.read_bytes() == legacy_txt.read_bytes()
    # 不写任何附加工件
    assert not parse_quality_path(dual_id).exists()
    assert not structured_references_path(dual_id).exists()


@respx.mock
async def test_extract_dual_track_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "grobid_url", "http://grobid.test", raising=False)
    monkeypatch.setattr(get_settings(), "mineru_parse_url", "http://mineru.test", raising=False)
    respx.post("http://grobid.test/api/processFulltextDocument").mock(
        return_value=Response(200, text=TEI_SAMPLE)
    )
    respx.post("http://mineru.test/file_parse").mock(
        return_value=Response(200, json=MINERU_SAMPLE)
    )
    paper_id = uuid.uuid4().hex
    outcome = await extract_with_dual_track(paper_id, _make_pdf(tmp_path))
    assert outcome.used_adapters
    # body 用 MinerU 分节正文
    assert outcome.txt_path.read_text(encoding="utf-8").startswith("Deep Agent Survey")
    # 结构化参考文献工件（含上下文句）
    refs = load_structured_references(paper_id)
    assert len(refs) == 3
    assert refs[1]["contexts"] == ["We follow the planning method of [2] to build our agent loop."]
    # 质检报告
    report = json.loads(parse_quality_path(paper_id).read_text(encoding="utf-8"))
    assert report["decisions"] == {"metadata": "grobid", "references": "grobid", "body": "mineru"}
    assert report["adapters"]["grobid"]["parsed"] and report["adapters"]["mineru"]["parsed"]
    assert outcome.metadata["title"] == "Deep Agent Survey"


@respx.mock
async def test_extract_adapter_failure_degrades_to_fallback(tmp_path, monkeypatch):
    """两轨都配了但都挂：正文 = 原 PyMuPDF 全文，错误进质检报告，旧结构化工件被清掉。"""
    from app.services.literature.pdf_extract import extract_full_text

    monkeypatch.setattr(get_settings(), "grobid_url", "http://grobid.test", raising=False)
    monkeypatch.setattr(get_settings(), "mineru_parse_url", "http://mineru.test", raising=False)
    respx.post("http://grobid.test/api/processFulltextDocument").mock(
        return_value=Response(500)
    )
    respx.post("http://mineru.test/file_parse").mock(return_value=Response(500))
    pdf_path = _make_pdf(tmp_path)
    paper_id = uuid.uuid4().hex
    stale = structured_references_path(paper_id)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"version": 1, "references": [{"raw": "stale"}]}', encoding="utf-8")

    outcome = await extract_with_dual_track(paper_id, pdf_path)
    legacy_txt = await extract_full_text(uuid.uuid4().hex, pdf_path)
    assert outcome.txt_path.read_bytes() == legacy_txt.read_bytes()
    assert not stale.exists()
    report = json.loads(parse_quality_path(paper_id).read_text(encoding="utf-8"))
    assert report["decisions"] == {
        "metadata": "fallback",
        "references": "fallback",
        "body": "fallback",
    }
    assert report["adapters"]["grobid"]["errors"]
    assert report["adapters"]["mineru"]["errors"]


# ---- 元数据写回：只补空字段 ----


def test_apply_missing_metadata_fills_only_empty_fields():
    paper = new_paper(title="Existing", doi="10.9/existing", year=None, venue=None)
    changed = apply_missing_metadata(
        paper,
        {
            "title": "Parsed",
            "doi": "10.9/parsed",
            "year": 2023,
            "venue": "Parsed Venue",
            "abstract": "parsed abstract",
            "authors": ["A One"],
        },
    )
    assert changed
    assert paper.doi == "10.9/existing"  # 已有值不被覆盖
    assert paper.year == 2023
    assert paper.venue == "Parsed Venue"
    assert paper.abstract == "parsed abstract"
    assert paper.authors == [{"name": "A One"}]


# ---- citation_graph：双输入源等价 + 结构化优先 ----


async def _edge_rows(session, paper_id):
    rows = (
        (
            await session.execute(
                select(PaperCitation)
                .where(PaperCitation.citing_paper_id == paper_id)
                .order_by(PaperCitation.ref_index)
            )
        )
        .scalars()
        .all()
    )
    return [(r.ref_index, r.cited_ref_raw, r.context) for r in rows]


async def test_citation_edges_structured_equals_regex(app, tmp_path):
    """同一篇论文：正则路径与等价的结构化工件必须建出同一组边。"""
    async with get_sessionmaker()() as session:
        paper = new_paper(title="Deep Agent Survey")
        session.add(paper)
        await session.flush()
        full_text_path = tmp_path / "full.txt"
        full_text_path.write_text(FULL_TEXT, encoding="utf-8")
        paper.full_text_path = str(full_text_path)

        assert await ensure_citation_edges(session, paper) == 3
        await session.commit()
        regex_rows = await _edge_rows(session, paper.id)
        assert len(regex_rows) == 3

        # 用正则产物同口径构造结构化工件（条目原文 + 首个上下文句），force 重建
        refs_path = structured_references_path(str(paper.id))
        refs_path.parent.mkdir(parents=True, exist_ok=True)
        refs_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "grobid",
                    "references": [
                        {"raw": raw, "contexts": [ctx] if ctx else []}
                        for _idx, raw, ctx in regex_rows
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assert await ensure_citation_edges(session, paper, force=True) == 3
        await session.commit()
        assert await _edge_rows(session, paper.id) == regex_rows


async def test_citation_edges_prefer_structured_over_regex(app, tmp_path):
    """结构化工件存在时优先吃它（条目与全文正则产物不同也以工件为准）。"""
    async with get_sessionmaker()() as session:
        paper = new_paper(title="Deep Agent Survey")
        session.add(paper)
        await session.flush()
        full_text_path = tmp_path / "full.txt"
        full_text_path.write_text(FULL_TEXT, encoding="utf-8")
        paper.full_text_path = str(full_text_path)
        refs_path = structured_references_path(str(paper.id))
        refs_path.parent.mkdir(parents=True, exist_ok=True)
        refs_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source": "grobid",
                    "references": [
                        {"raw": "Only Structured Entry. 2024.", "contexts": ["ctx one."]},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assert await ensure_citation_edges(session, paper) == 1
        await session.commit()
        assert await _edge_rows(session, paper.id) == [
            (1, "Only Structured Entry. 2024.", "ctx one.")
        ]


async def test_citation_edges_corrupt_artifact_falls_back_to_regex(app, tmp_path):
    async with get_sessionmaker()() as session:
        paper = new_paper(title="Deep Agent Survey")
        session.add(paper)
        await session.flush()
        full_text_path = tmp_path / "full.txt"
        full_text_path.write_text(FULL_TEXT, encoding="utf-8")
        paper.full_text_path = str(full_text_path)
        refs_path = structured_references_path(str(paper.id))
        refs_path.parent.mkdir(parents=True, exist_ok=True)
        refs_path.write_text("not json at all", encoding="utf-8")
        assert await ensure_citation_edges(session, paper) == 3  # 正则路径兜底


# ---- 可用性只看配置 ----


def test_adapter_availability_flags(monkeypatch):
    assert not GrobidAdapter().available()
    assert not MineruWebAdapter().available()
    monkeypatch.setattr(get_settings(), "grobid_url", "http://grobid.test", raising=False)
    monkeypatch.setattr(get_settings(), "mineru_parse_url", "http://m.test", raising=False)
    assert GrobidAdapter().available()
    assert MineruWebAdapter().available()
    assert dual_track_available()
