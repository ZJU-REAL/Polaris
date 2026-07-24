"""作者↔机构 LLM 解析 + 抽取模式测试（services/affiliations.py + 接线）。

覆盖：
- 服务函数：逐位作者映射解析 / apply 对齐到每位作者并汇总去重总表 / 坏 JSON / 无全文 /
  LLM 报错兜底 None / 标题页截断 / 向后兼容拍平；
- OpenAlex `_simplify` 的 authors 现在每人带机构；
- 抽取模式读写（默认 on_add，非法值报错）+ 管理员端点；
- wiki.fetch_extract 接线（on_add：有全文走 LLM 映射、失败/无全文回落 OpenAlex）；
- on_compile：enrich 不调专门抽取（计数=0）；compile_paper 折叠定界块解析 + 剥离干净
  （坏 JSON 也剥净、映射为 None），调用方 apply 后 paper.authors 带机构；
- 手动 fetch-pdf 路径补机构。
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select

from app.agents.voyage import actions_wiki
from app.agents.voyage.actions import ActionContext
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.paper import Paper
from app.models.voyage import VoyageRun
from app.services import affiliations as affil_service
from app.services.affiliations import (
    _HEAD_CHARS,
    _MAX_TOKENS,
    InvalidAffiliationModeError,
    apply_author_affiliations,
    extract_affiliations_llm,
    extract_author_affiliations_llm,
    flatten_affiliations,
    get_affiliation_extraction_mode,
    parse_and_strip_affiliation_block,
    set_affiliation_extraction_mode,
)
from app.services.literature import (
    ArxivClient,
    OpenAlexClient,
    SemanticScholarClient,
    reset_clients,
    set_clients,
)
from app.services.literature.openalex import _simplify
from app.services.wiki_compile import CompiledWiki, compile_paper
from tests.conftest import add_paper, membership_of, register_and_login

FULL_TEXT = (
    "Great Paper Title\n"
    "Alice Zhang (Zhejiang University)  Bob Li (Google DeepMind)\n"
    "alice@zju.edu.cn\n\nAbstract: something interesting.\n"
)

# fake LLM（core/llm/fake.py）对机构抽取的确定性映射输出
FAKE_MAPPING = [
    {"name": "Alice Zhang", "affiliations": ["Zhejiang University"]},
    {"name": "Bob Li", "affiliations": ["Google DeepMind"]},
]

OPENALEX_WORK = {
    "id": "https://openalex.org/W1",
    "title": "Affil Paper",
    "publication_year": 2026,
    "publication_date": "2026-01-02",
    "authorships": [
        {
            "author": {"display_name": "Carol"},
            "institutions": [{"display_name": "OpenAlex University"}],
        }
    ],
}


class _StubLLM:
    """记录调用参数的假路由器；error 给定时抛出（测调用失败兜底）。"""

    def __init__(self, content: str = "[]", error: Exception | None = None):
        self.content = content
        self.error = error
        self.calls: list[dict] = []

    async def complete(self, stage, messages, **kwargs):
        self.calls.append({"stage": stage, "messages": messages, **kwargs})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.content, model="fake-model")


def _paper(tmp_path, full_text: str | None) -> Paper:
    txt_path = None
    if full_text is not None:
        f = tmp_path / "full.txt"
        f.write_text(full_text, encoding="utf-8")
        txt_path = str(f)
    return Paper(title="Affil Paper", full_text_path=txt_path)


# ---- 服务函数单测（无 DB） ----


async def test_extract_author_affiliations_parses_mapping(tmp_path):
    llm = _StubLLM(
        '好的，结果：[{"name": "Alice Zhang", "affiliations": ["Zhejiang University", '
        '"Zhejiang University"]}, {"name": "Bob Li", "affiliations": [" Google DeepMind "]}, '
        '{"name": "", "affiliations": ["Ghost"]}]'
    )
    paper = _paper(tmp_path, FULL_TEXT)
    mapping = await extract_author_affiliations_llm(paper, llm=llm)
    assert mapping == [
        {"name": "Alice Zhang", "affiliations": ["Zhejiang University"]},  # 去重
        {"name": "Bob Li", "affiliations": ["Google DeepMind"]},  # strip
    ]  # 空名整项丢弃
    call = llm.calls[0]
    assert call["stage"] == "librarian"
    assert call["max_tokens"] == _MAX_TOKENS
    assert call["project_id"] is None


def test_apply_author_affiliations_attaches_per_author():
    paper = Paper(
        title="T",
        authors=[{"name": "Alice Zhang"}, {"name": "Bob Li"}, {"name": "Carol"}],
    )
    mapping = [
        {"name": "alice zhang", "affiliations": ["Zhejiang University"]},  # 大小写不敏感对齐
        {"name": "Bob Li", "affiliations": ["Google DeepMind", "Zhejiang University"]},
    ]
    assert apply_author_affiliations(paper, mapping) is True
    by_name = {a["name"]: a.get("affiliations") for a in paper.authors}
    assert by_name["Alice Zhang"] == ["Zhejiang University"]
    assert by_name["Bob Li"] == ["Google DeepMind", "Zhejiang University"]
    assert by_name["Carol"] is None  # 未映射到的作者不带机构键
    # paper.affiliations = 去重保序的机构总表
    assert paper.affiliations == ["Zhejiang University", "Google DeepMind"]


def test_apply_author_affiliations_builds_authors_when_empty():
    paper = Paper(title="T")  # 无已知作者
    assert apply_author_affiliations(paper, FAKE_MAPPING) is True
    assert paper.authors == FAKE_MAPPING
    assert paper.affiliations == ["Zhejiang University", "Google DeepMind"]


def test_apply_author_affiliations_no_affiliations_returns_false():
    paper = Paper(title="T", authors=[{"name": "Alice"}])
    assert apply_author_affiliations(paper, [{"name": "Alice", "affiliations": []}]) is False
    assert apply_author_affiliations(paper, None) is False
    assert paper.affiliations is None


def test_flatten_affiliations_dedup_order():
    assert flatten_affiliations(FAKE_MAPPING) == ["Zhejiang University", "Google DeepMind"]


async def test_extract_author_affiliations_truncates_head(tmp_path):
    # 3500 字以后的内容（LATE_MARKER）不应进 prompt
    text = FULL_TEXT + "x" * _HEAD_CHARS + "LATE_MARKER"
    llm = _StubLLM('[{"name": "Alice Zhang", "affiliations": ["Zhejiang University"]}]')
    paper = _paper(tmp_path, text)
    assert await extract_author_affiliations_llm(paper, llm=llm) == [
        {"name": "Alice Zhang", "affiliations": ["Zhejiang University"]}
    ]
    user_msg = llm.calls[0]["messages"][1].content
    assert "LATE_MARKER" not in user_msg
    assert user_msg.endswith(text[:_HEAD_CHARS].strip())
    assert FULL_TEXT.strip().splitlines()[0] in user_msg  # 标题页开头在


async def test_extract_author_affiliations_bad_json_returns_none(tmp_path):
    for content in ("解析不了，抱歉", '{"name": "x"}', "[]", "[{}]", '[{"name": "  "}]'):
        paper = _paper(tmp_path, FULL_TEXT)
        assert await extract_author_affiliations_llm(paper, llm=_StubLLM(content)) is None


async def test_extract_author_affiliations_llm_error_returns_none(tmp_path):
    paper = _paper(tmp_path, FULL_TEXT)
    llm = _StubLLM(error=RuntimeError("boom"))
    assert await extract_author_affiliations_llm(paper, llm=llm) is None


async def test_extract_author_affiliations_no_fulltext_returns_none(tmp_path):
    llm = _StubLLM('[{"name": "Alice", "affiliations": ["MIT"]}]')
    assert await extract_author_affiliations_llm(_paper(tmp_path, None), llm=llm) is None
    missing = Paper(title="T", full_text_path=str(tmp_path / "nope.txt"))
    assert await extract_author_affiliations_llm(missing, llm=llm) is None
    assert llm.calls == []


async def test_extract_affiliations_llm_backcompat_flattens(tmp_path):
    """向后兼容包装：只要去重机构总表。"""
    llm = _StubLLM(
        '[{"name": "Alice", "affiliations": ["MIT", "MIT"]}, '
        '{"name": "Bob", "affiliations": ["CMU"]}]'
    )
    assert await extract_affiliations_llm(_paper(tmp_path, FULL_TEXT), llm=llm) == ["MIT", "CMU"]


def test_openalex_simplify_authors_carry_affiliations():
    simplified = _simplify(OPENALEX_WORK)
    assert simplified["authors"] == [{"name": "Carol", "affiliations": ["OpenAlex University"]}]
    assert simplified["affiliations"] == ["OpenAlex University"]


# ---- on_compile 定界块解析/剥离（纯函数） ----


def test_parse_and_strip_block_good():
    content = (
        "## TL;DR\n\n正文（fake）。\n\n---\n<<<POLARIS_AFFILIATIONS\n"
        '[{"name": "Alice", "affiliations": ["MIT"]}]\nPOLARIS_AFFILIATIONS>>>\n'
    )
    stripped, mapping = parse_and_strip_affiliation_block(content)
    assert "POLARIS_AFFILIATIONS" not in stripped  # 块 + 前置分隔线都删干净
    assert stripped == "## TL;DR\n\n正文（fake）。\n"
    assert mapping == [{"name": "Alice", "affiliations": ["MIT"]}]


def test_parse_and_strip_block_bad_json():
    content = "正文B\n<<<POLARIS_AFFILIATIONS\n[{\"name\": broken OOPS]\nPOLARIS_AFFILIATIONS>>>\n"
    stripped, mapping = parse_and_strip_affiliation_block(content)
    assert "POLARIS_AFFILIATIONS" not in stripped  # 坏 JSON 也要剥净
    assert stripped == "正文B\n"
    assert mapping is None


def test_parse_and_strip_block_unclosed_marker():
    """模型只吐了开头标记、没闭合 → 从裸标记删到文末，绝不漏进 wiki。"""
    content = "## TL;DR\n\n正文C。\n\n---\n<<<POLARIS_AFFILIATIONS\n[{\"name\": \"Alice\"}]"
    stripped, mapping = parse_and_strip_affiliation_block(content)
    assert "POLARIS_AFFILIATIONS" not in stripped
    assert stripped == "## TL;DR\n\n正文C。\n"
    assert mapping is None


def test_parse_and_strip_block_absent():
    content = "只有正文，没有块\n"
    assert parse_and_strip_affiliation_block(content) == (content, None)


# ---- 抽取模式读写 + 管理员端点 ----


async def test_affiliation_mode_default_and_roundtrip(app):
    async with get_sessionmaker()() as session:
        assert await get_affiliation_extraction_mode(session) == "on_add"  # 默认
        assert await set_affiliation_extraction_mode(session, "on_compile") == "on_compile"
    async with get_sessionmaker()() as session:
        assert await get_affiliation_extraction_mode(session) == "on_compile"
        assert await set_affiliation_extraction_mode(session, "on_add") == "on_add"


async def test_affiliation_mode_rejects_invalid(app):
    async with get_sessionmaker()() as session:
        with pytest.raises(InvalidAffiliationModeError):
            await set_affiliation_extraction_mode(session, "whenever")


async def test_affiliation_mode_admin_endpoint(client):
    admin = await register_and_login(client)  # 首个 = admin
    member = await register_and_login(client, email="bob2@example.com")
    ah = {"Authorization": f"Bearer {admin}"}
    mh = {"Authorization": f"Bearer {member}"}

    resp = await client.get("/api/admin/settings/affiliation-mode", headers=ah)
    assert resp.status_code == 200 and resp.json()["mode"] == "on_add"
    # 普通成员改 → 403
    resp = await client.put(
        "/api/admin/settings/affiliation-mode", json={"mode": "on_compile"}, headers=mh
    )
    assert resp.status_code == 403
    # admin 改；非法值 422（schema Literal 校验）
    resp = await client.put(
        "/api/admin/settings/affiliation-mode", json={"mode": "on_compile"}, headers=ah
    )
    assert resp.status_code == 200 and resp.json()["mode"] == "on_compile"
    resp = await client.put(
        "/api/admin/settings/affiliation-mode", json={"mode": "nonsense"}, headers=ah
    )
    assert resp.status_code == 422


# ---- compile_paper：on_compile 折叠抽取 ----

_WIKI_BODY = "## TL;DR\n\n测试解读正文（fake）。\n"


async def test_compile_paper_collects_and_strips_block():
    paper = Paper(title="Affil Paper", abstract="some abstract", authors=[{"name": "Alice Zhang"}])
    content = (
        _WIKI_BODY
        + "\n---\n<<<POLARIS_AFFILIATIONS\n"
        + '[{"name": "Alice Zhang", "affiliations": ["Zhejiang University"]}]\n'
        + "POLARIS_AFFILIATIONS>>>\n"
    )
    llm = _StubLLM(content)
    compiled = await compile_paper(paper, statement=None, llm=llm, collect_affiliations=True)
    assert isinstance(compiled, CompiledWiki)
    assert "POLARIS_AFFILIATIONS" not in compiled.content  # 绝不残留进 wiki
    assert compiled.content == _WIKI_BODY
    assert compiled.author_affiliations == [
        {"name": "Alice Zhang", "affiliations": ["Zhejiang University"]}
    ]
    # 定界块指令确有追加到 prompt
    assert affil_service.AFFIL_COMPILE_INSTRUCTION in llm.calls[0]["messages"][1].content
    # 调用方据此补库
    assert apply_author_affiliations(paper, compiled.author_affiliations) is True
    assert paper.authors[0]["affiliations"] == ["Zhejiang University"]
    assert paper.affiliations == ["Zhejiang University"]


async def test_compile_paper_bad_block_still_clean():
    paper = Paper(title="Affil Paper", abstract="some abstract")
    content = (
        _WIKI_BODY + '\n<<<POLARIS_AFFILIATIONS\n[{"name": oops OOPS]\nPOLARIS_AFFILIATIONS>>>\n'
    )
    compiled = await compile_paper(
        paper, statement=None, llm=_StubLLM(content), collect_affiliations=True
    )
    assert "POLARIS_AFFILIATIONS" not in compiled.content  # 坏 JSON 也剥净
    assert compiled.content == _WIKI_BODY
    assert compiled.author_affiliations is None  # 解析失败 → None，wiki 照常


async def test_compile_paper_no_collect_leaves_prompt_clean():
    paper = Paper(title="Affil Paper", abstract="some abstract")
    llm = _StubLLM(_WIKI_BODY)
    compiled = await compile_paper(paper, statement=None, llm=llm, collect_affiliations=False)
    assert compiled.author_affiliations is None
    assert affil_service.AFFIL_COMPILE_INSTRUCTION not in llm.calls[0]["messages"][1].content


# ---- wiki.fetch_extract 接线（优先级 LLM > OpenAlex） ----


@pytest_asyncio.fixture
async def lit_clients(app):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    set_clients(
        arxiv=ArxivClient(redis=redis, min_interval=0),
        s2=SemanticScholarClient(redis=redis, api_key="", rate=10_000, backoff_base=0.0),
        openalex=OpenAlexClient(redis=redis, mailto="test@example.org"),
    )
    yield
    reset_clients()
    await redis.aclose()


async def _setup_scored_paper(
    client, tmp_path, *, full_text: str | None, published: bool
) -> tuple[uuid.UUID, VoyageRun]:
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "affil-proj"}, headers=headers)
    assert resp.status_code == 201, resp.text
    project_id = uuid.UUID(resp.json()["id"])
    txt_path = None
    if full_text is not None:
        f = tmp_path / "full.txt"
        f.write_text(full_text, encoding="utf-8")
        txt_path = str(f)
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=project_id,
            source="snowball",
            status="scored",
            title="Affil Paper",
            doi="10.1234/affil.1",
            relevance_score=0.9,
            full_text_path=txt_path,
            published_at=datetime(2026, 1, 1, tzinfo=UTC) if published else None,
        )
        run = VoyageRun(
            kind="wiki_ingest",
            goal="ingest",
            status="executing",
            cursor=0,
            project_id=project_id,
            checkpoint={"params": {}},
        )
        session.add_all([paper, run])
        await session.commit()
        await session.refresh(paper)
        await session.refresh(run)
        return paper.id, run


async def _load_paper(paper_id: uuid.UUID) -> Paper:
    async with get_sessionmaker()() as session:
        return (await session.execute(select(Paper).where(Paper.id == paper_id))).scalar_one()


async def test_fetch_extract_prefers_llm_over_openalex(client, lit_clients, tmp_path):
    paper_id, run = await _setup_scored_paper(client, tmp_path, full_text=FULL_TEXT, published=True)
    with respx.mock(assert_all_called=False) as router:
        openalex_route = router.get(url__regex=r"https://api\.openalex\.org/.*").mock(
            return_value=httpx.Response(200, json=OPENALEX_WORK)
        )
        ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=dict(run.checkpoint or {}))
        obs = await actions_wiki.fetch_extract(ctx, {})
    assert obs["succeeded"] == 1 and obs["failed"] == []
    assert not openalex_route.called  # 有全文 → 走 LLM，OpenAlex 不被调
    paper = await _load_paper(paper_id)
    assert paper.affiliations == ["Zhejiang University", "Google DeepMind"]  # fake LLM 映射拍平
    # 逐位作者带机构（apply 写回 paper.authors）
    by_name = {a["name"]: a.get("affiliations") for a in paper.authors}
    assert by_name["Alice Zhang"] == ["Zhejiang University"]
    assert by_name["Bob Li"] == ["Google DeepMind"]
    async with get_sessionmaker()() as session:
        membership = await membership_of(session, project_id=run.project_id, paper_id=paper_id)
        assert membership.status == "fetched"


async def test_fetch_extract_llm_failure_falls_back_to_openalex(
    client, lit_clients, tmp_path, monkeypatch
):
    async def _fail(paper, **kwargs):
        return None  # 模拟 LLM 解析失败（extract 内部失败即返回 None）

    monkeypatch.setattr(actions_wiki, "extract_author_affiliations_llm", _fail)
    paper_id, run = await _setup_scored_paper(
        client, tmp_path, full_text=FULL_TEXT, published=False
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://api\.openalex\.org/.*").mock(
            return_value=httpx.Response(200, json=OPENALEX_WORK)
        )
        ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=dict(run.checkpoint or {}))
        obs = await actions_wiki.fetch_extract(ctx, {})
    assert obs["succeeded"] == 1
    paper = await _load_paper(paper_id)
    assert paper.affiliations == ["OpenAlex University"]  # OpenAlex 兜底（authors 带机构）
    assert paper.published_at is not None  # 顺带补 DOI 论文发表日期


async def test_fetch_extract_no_fulltext_uses_openalex(client, lit_clients, tmp_path, monkeypatch):
    llm_calls = {"n": 0}

    async def _count(paper, **kwargs):
        llm_calls["n"] += 1
        return FAKE_MAPPING

    monkeypatch.setattr(actions_wiki, "extract_author_affiliations_llm", _count)
    paper_id, run = await _setup_scored_paper(client, tmp_path, full_text=None, published=False)
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://api\.openalex\.org/.*").mock(
            return_value=httpx.Response(200, json=OPENALEX_WORK)
        )
        ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=dict(run.checkpoint or {}))
        await actions_wiki.fetch_extract(ctx, {})
    assert llm_calls["n"] == 0  # 无全文不调 LLM
    paper = await _load_paper(paper_id)
    assert paper.affiliations == ["OpenAlex University"]
    assert paper.published_at is not None


async def test_fetch_extract_on_compile_skips_dedicated_llm(
    client, lit_clients, tmp_path, monkeypatch
):
    """on_compile：fetch_extract 不调专门抽取；无全文时 OpenAlex 兜底仍生效。"""
    llm_calls = {"n": 0}

    async def _count(paper, **kwargs):
        llm_calls["n"] += 1
        return FAKE_MAPPING

    monkeypatch.setattr(actions_wiki, "extract_author_affiliations_llm", _count)
    async with get_sessionmaker()() as session:
        await set_affiliation_extraction_mode(session, "on_compile")
    paper_id, run = await _setup_scored_paper(
        client, tmp_path, full_text=FULL_TEXT, published=False
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://api\.openalex\.org/.*").mock(
            return_value=httpx.Response(200, json=OPENALEX_WORK)
        )
        ctx = ActionContext(run=run, llm=LLMRouter(), checkpoint=dict(run.checkpoint or {}))
        await actions_wiki.fetch_extract(ctx, {})
    assert llm_calls["n"] == 0  # on_compile 跳过专门抽取
    paper = await _load_paper(paper_id)
    assert paper.affiliations == ["OpenAlex University"]  # OpenAlex 兜底不受模式影响


async def test_enrich_on_compile_skips_dedicated_llm(client, tmp_path, monkeypatch):
    """on_compile：enrich 分阶段补全不调专门机构抽取（计数=0）。"""
    from app.services import paper_enrich

    calls = {"n": 0}

    async def _count(paper, **kwargs):
        calls["n"] += 1
        return FAKE_MAPPING

    # enrich 内部从 affiliations 源模块局部导入，故 patch 源符号
    monkeypatch.setattr(affil_service, "extract_author_affiliations_llm", _count)

    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "enrich-proj"}, headers=headers)
    project_id = uuid.UUID(resp.json()["id"])
    f = tmp_path / "full.txt"
    f.write_text(FULL_TEXT, encoding="utf-8")
    async with get_sessionmaker()() as session:
        await set_affiliation_extraction_mode(session, "on_compile")
        paper = await add_paper(
            session,
            project_id=project_id,
            source="manual",
            status="included",
            title="Affil Paper",
            full_text_path=str(f),
        )
        await session.commit()
        paper_id = paper.id

    async def _emit(*args, **kwargs):
        return None

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        await paper_enrich.enrich_paper(
            session, paper, target=None, user_id=None, project_id=project_id, emit=_emit
        )
    assert calls["n"] == 0  # on_compile 跳过专门抽取
    paper = await _load_paper(paper_id)
    assert not paper.affiliations  # 该阶段不补机构（留给编译）


# ---- 手动 fetch-pdf 路径（补机构） ----


def _make_pdf_bytes() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Great Paper Title. Alice Zhang, Zhejiang University.")
    data = doc.tobytes()
    doc.close()
    return data


async def test_fetch_pdf_backfills_affiliations(client, lit_clients):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "affil-proj"}, headers=headers)
    project_id = uuid.UUID(resp.json()["id"])
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=project_id,
            source="manual",
            status="included",
            title="Affil Paper",
            arxiv_id="2404.11111",
        )
        session.add(paper)
        await session.commit()
        await session.refresh(paper)
        paper_id = paper.id
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://arxiv\.org/pdf/.*").mock(
            return_value=httpx.Response(200, content=_make_pdf_bytes())
        )
        resp = await client.post(f"/api/papers/{paper_id}/fetch-pdf", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["affiliations"] == ["Zhejiang University", "Google DeepMind"]
    paper = await _load_paper(paper_id)
    assert paper.affiliations == ["Zhejiang University", "Google DeepMind"]
