"""同一篇论文以两种标题写法进库的查重与合并。

真实例子：arXiv 给的是 "REALM: Retrieval-Augmented Language Model Pre-Training"，
OpenAlex 收的会议版是 "Retrieval Augmented Language Model Pre-Training"——后者没有
arXiv 号也没有 DOI，标识和标题哈希都对不上，同一篇在一个库里进了两次。
"""

import uuid

import pytest
from sqlalchemy import func, select

from app.agents.voyage.actions_wiki import _pool_paper_or_new
from app.core.db import get_sessionmaker
from app.models.library_direction import LibraryPaper
from app.models.literature_discovery import LiteratureSearchHit, LiteratureSearchRun
from app.models.paper import Paper, PaperNote, UserPaperTag, new_paper
from app.models.paper_assets import PaperAsset, PdfBlob
from app.models.paper_citation import PaperCitation
from app.models.paper_content import PaperContentVersion
from app.models.paper_extraction import PaperExtraction
from app.models.user import User
from app.services import paper_merge
from app.services.dedup import WorkRef, pool_dedup_key, same_work, titles_match
from app.services.libraries import find_pool_paper, get_library_for_project
from app.services.literature import survey_search
from app.services.literature.openalex import _simplify
from tests.conftest import add_paper, register_and_login

REALM_ARXIV = "REALM: Retrieval-Augmented Language Model Pre-Training"
REALM_OPENALEX = "Retrieval Augmented Language Model Pre-Training"
GUU = [{"name": "Kelvin Guu", "affiliations": ["Google Research"]}, {"name": "Kenton Lee"}]
GUU_OPENALEX = [{"name": "Kelvin Guu", "affiliations": ["Google (United States)"]}]


def _ref(title, year, authors, arxiv_id=None):
    return WorkRef(title=title, year=year, authors=authors, arxiv_id=arxiv_id)


# ---- 判定规则 ----


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (REALM_ARXIV, REALM_OPENALEX),
        (REALM_OPENALEX, REALM_ARXIV),
        ("Retrieval-Augmented Language Model Pretraining", REALM_OPENALEX),  # 连字符 / 连写
        ("retrieval augmented language model pre-training.", REALM_OPENALEX),  # 大小写 / 标点
        ("GPT-3 — Language Models are Few-Shot Learners", "Language models are few-shot learners"),
        ("Réseaux de neurones profonds", "Reseaux de Neurones Profonds"),  # 重音
    ],
)
def test_titles_match_spelling_variants(a, b):
    assert titles_match(a, b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Deep Residual Learning", "Deep Residual Learning II"),  # 续篇
        ("Scaling Laws for Language Models", "Scaling Laws for Language Models v2"),
        # 两边各有代号、去掉后相同：两篇不同的论文
        ("BERT: Pre-training of Deep Transformers", "RoBERTa: Pre-training of Deep Transformers"),
        ("Attention Is All You Need", "Is Attention All You Need"),  # 词序不同
        ("A Study: Retrieval", "Retrieval"),  # 去掉代号后剩得太短，不拿它单独比
        ("Survey", "Survey"),  # 太短的标题不参与
        (REALM_ARXIV, "Retrieval Augmented Language Model Pre-Training for Code"),
    ],
)
def test_titles_do_not_match_different_papers(a, b):
    assert not titles_match(a, b)


def test_same_work_needs_title_year_and_first_author():
    arxiv = _ref(REALM_ARXIV, 2020, GUU, "2002.08909")
    assert same_work(arxiv, _ref(REALM_OPENALEX, 2020, GUU_OPENALEX))
    assert same_work(arxiv, _ref(REALM_OPENALEX, 2021, [{"name": "Guu, Kelvin"}]))  # 跨年发表
    assert same_work(arxiv, _ref(REALM_OPENALEX, 2019, ["K. Guu"]))

    assert not same_work(arxiv, _ref(REALM_OPENALEX, 2022, GUU_OPENALEX))  # 差两年
    assert not same_work(arxiv, _ref(REALM_OPENALEX, None, GUU_OPENALEX))  # 没年份不认
    assert not same_work(arxiv, _ref(REALM_OPENALEX, 2020, [{"name": "Kenton Lee"}]))
    assert not same_work(arxiv, _ref(REALM_OPENALEX, 2020, None))  # 没作者不认
    # 两边各有 arXiv 号且不同：标题作者年份再像也不是同一篇
    assert not same_work(arxiv, _ref(REALM_ARXIV, 2020, GUU, "2003.00001"))
    assert same_work(arxiv, _ref(REALM_ARXIV, 2020, GUU, "2002.08909v3"))


# ---- OpenAlex 记录里的标识 ----


def test_openalex_recovers_arxiv_id_and_doi_from_any_location():
    base = {"id": "https://openalex.org/W1", "title": REALM_OPENALEX, "publication_year": 2020}

    by_doi = _simplify({**base, "doi": "https://doi.org/10.48550/arXiv.2002.08909"})
    assert (by_doi["arxiv_id"], by_doi["doi"]) == ("2002.08909", "10.48550/arXiv.2002.08909")

    by_ids = _simplify({**base, "doi": None, "ids": {"doi": "https://doi.org/10.1000/realm"}})
    assert (by_ids["arxiv_id"], by_ids["doi"]) == (None, "10.1000/realm")

    for key in ("primary_location", "best_oa_location"):
        for field in ("landing_page_url", "pdf_url"):
            url = "https://arxiv.org/pdf/2002.08909v2" if field == "pdf_url" else None
            url = url or "http://arxiv.org/abs/2002.08909"
            assert _simplify({**base, key: {field: url}})["arxiv_id"] == "2002.08909", (key, field)

    in_list = _simplify(
        {
            **base,
            "locations": [
                {"landing_page_url": "http://proceedings.mlr.press/v119/guu20a.html"},
                {"landing_page_url": "https://doi.org/10.48550/arxiv.2002.08909"},
            ],
        }
    )
    assert in_list["arxiv_id"] == "2002.08909"
    assert in_list["doi"] == "10.48550/arxiv.2002.08909"

    oa_url = _simplify({**base, "open_access": {"oa_url": "https://arxiv.org/pdf/hep-th/9901001"}})
    assert oa_url["arxiv_id"] == "hep-th/9901001"

    # 只在会议页面上有的记录：没有就是没有，不编
    plain = _simplify(
        {**base, "primary_location": {"landing_page_url": "http://proceedings.mlr.press/x.pdf"}}
    )
    assert plain["arxiv_id"] is None and plain["doi"] is None


# ---- 多来源合并（入库前） ----


def test_fuse_merges_the_two_spellings_but_not_a_sequel():
    arxiv = {"title": REALM_ARXIV, "arxiv_id": "2002.08909", "year": 2020, "authors": GUU}
    openalex = {"title": REALM_OPENALEX, "year": 2020, "authors": GUU_OPENALEX, "citations": 900}
    sequel = {"title": REALM_OPENALEX + " II", "year": 2020, "authors": GUU_OPENALEX}
    other_team = {"title": REALM_OPENALEX, "year": 2020, "authors": [{"name": "Ada Lovelace"}]}

    from datetime import UTC, datetime

    fused = survey_search.fuse(
        [("arxiv", [arxiv]), ("openalex", [openalex, sequel])],
        recent_since=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert len(fused) == 2
    merged = next(c for c in fused if c.sources == {"arxiv", "openalex"})
    assert merged.entry["arxiv_id"] == "2002.08909"
    assert merged.citations == 900

    # 同名但首作者不同：不合并
    fused = survey_search.fuse(
        [("arxiv", [arxiv]), ("openalex", [other_team])],
        recent_since=datetime(2025, 1, 1, tzinfo=UTC),
    )
    assert len(fused) == 2


# ---- 入库时查池 ----


async def _project(client, email):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "去重方向"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _add_realm_from_arxiv(session, project_id, **extra):
    return await add_paper(
        session,
        project_id=project_id,
        source="arxiv",
        title=REALM_ARXIV,
        year=2020,
        authors=GUU,
        arxiv_id="2002.08909",
        doi="10.48550/arxiv.2002.08909",
        dedup_key="arxiv:2002.08909",
        **extra,
    )


async def _insert_from_openalex(session, library_id, *, title, year=2020, authors=None):
    authors = GUU_OPENALEX if authors is None else authors
    return await _pool_paper_or_new(
        session,
        library_id=library_id,
        make_paper=lambda: new_paper(source="openalex", title=title, year=year, authors=authors),
        arxiv_id=None,
        doi=None,
        title=title,
        year=year,
        authors=authors,
    )


async def _library_titles(session, library_id):
    rows = await session.execute(
        select(Paper.title)
        .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
        .where(LibraryPaper.library_id == library_id)
        .order_by(Paper.title)
    )
    return list(rows.scalars())


async def test_second_spelling_of_a_paper_hits_the_pool_instead_of_a_new_row(client):
    project_id = await _project(client, "dedup-insert@example.com")
    async with get_sessionmaker()() as session:
        realm = await _add_realm_from_arxiv(session, project_id)
        await session.commit()
        library = await get_library_for_project(session, uuid.UUID(project_id))

        key = pool_dedup_key(
            arxiv_id=None, doi=None, title=REALM_OPENALEX, year=2020, authors=GUU_OPENALEX
        )
        found = await find_pool_paper(session, arxiv_id=None, doi=None, dedup_key=key)
        assert found is not None and found.id == realm.id

        # 真正的写路径：这篇已经在库里 → 不新建、不重复加入
        assert await _insert_from_openalex(session, library.id, title=REALM_OPENALEX) is None
        await session.commit()
        assert await _library_titles(session, library.id) == [REALM_ARXIV]


async def test_different_papers_with_similar_titles_stay_separate(client):
    project_id = await _project(client, "dedup-separate@example.com")
    async with get_sessionmaker()() as session:
        await _add_realm_from_arxiv(session, project_id)
        await session.commit()
        library = await get_library_for_project(session, uuid.UUID(project_id))

        sequel = REALM_OPENALEX + " II"
        cases = [
            {"title": sequel},  # 续篇
            {"title": REALM_OPENALEX, "year": 2023},  # 同名，隔了三年
            {"title": REALM_OPENALEX, "authors": [{"name": "Ada Lovelace"}]},  # 同名，别人写的
        ]
        for case in cases:
            paper = await _insert_from_openalex(session, library.id, **case)
            assert paper is not None, case
            # 键带着标题等信息，但存进库里就是一个普通字符串
            assert paper.dedup_key.startswith("title:")
        await session.commit()
        assert await _library_titles(session, library.id) == sorted(
            [REALM_ARXIV, REALM_OPENALEX, REALM_OPENALEX, sequel]
        )


# ---- 合并库里已有的重复 ----


async def test_existing_duplicates_in_a_library_are_merged_once(client):
    project_id = await _project(client, "dedup-merge@example.com")
    async with get_sessionmaker()() as session:
        user_id = (await session.execute(select(User.id).limit(1))).scalar_one()
        # 会议版先进库、信息少；arXiv 版后进、有 PDF 和全文 → 留 arXiv 版
        thin = await add_paper(
            session,
            project_id=project_id,
            source="openalex",
            title=REALM_OPENALEX,
            year=2020,
            authors=GUU_OPENALEX,
            venue="ICML",
            dedup_key="title:thin",
            status="compiled",
            relevance_score=0.95,
        )
        rich = await _add_realm_from_arxiv(
            session, project_id, pdf_path="a.pdf", full_text_path="a.txt", status="scored"
        )
        sequel = await add_paper(
            session,
            project_id=project_id,
            title=REALM_OPENALEX + " II",
            year=2020,
            authors=GUU_OPENALEX,
            dedup_key="title:sequel",
        )
        citing = await add_paper(session, project_id=project_id, title="A Paper Citing REALM")
        library = await get_library_for_project(session, uuid.UUID(project_id))

        # 挂在会议版（将被合并掉的那行）上的各种东西
        session.add(PaperNote(paper_id=thin.id, author_id=user_id, content="读过"))
        session.add(UserPaperTag(paper_id=thin.id, user_id=user_id, name="rag"))
        session.add(UserPaperTag(paper_id=rich.id, user_id=user_id, name="rag"))  # 同名：不重复
        session.add(UserPaperTag(paper_id=thin.id, user_id=user_id, name="icml"))
        session.add(PaperExtraction(paper_id=thin.id, schema_id="method", payload={"a": "b"}))
        session.add(PaperCitation(citing_paper_id=thin.id, ref_index=1, cited_ref_raw="[1] X"))
        session.add(
            PaperCitation(
                citing_paper_id=citing.id, cited_paper_id=thin.id, ref_index=1, cited_ref_raw="R"
            )
        )
        blob = PdfBlob(sha256="0" * 64, byte_size=1, storage_key="blobs/0")
        session.add(blob)
        await session.flush()
        asset = PaperAsset(paper_id=thin.id, blob_id=blob.id, source="upload", is_preferred=True)
        session.add(asset)
        await session.flush()
        session.add(
            PaperContentVersion(paper_id=thin.id, asset_id=asset.id, version_no=1, parser="x")
        )
        run = LiteratureSearchRun(
            library_id=library.id, requested_count=1, candidate_budget=1, topic="rag"
        )
        session.add(run)
        await session.flush()
        session.add(
            LiteratureSearchHit(
                run_id=run.id,
                paper_id=thin.id,
                source="openalex",
                dedup_key="title:x",
                title=REALM_OPENALEX,
                status="promoted",
            )
        )
        await session.commit()
        thin_id, rich_id, sequel_id, citing_id = thin.id, rich.id, sequel.id, citing.id
        library_id = library.id

    async with get_sessionmaker()() as session:
        reports = await paper_merge.merge_library_duplicates(session, library_id=library_id)
        assert [(r["kept_id"], r["dropped_id"]) for r in reports] == [(str(rich_id), str(thin_id))]

    async with get_sessionmaker()() as session:
        assert await session.get(Paper, thin_id) is None
        assert await session.get(Paper, sequel_id) is not None  # 续篇没被并进去
        kept = await session.get(Paper, rich_id)
        assert kept.title == REALM_ARXIV and kept.arxiv_id == "2002.08909"
        assert kept.venue == "ICML"  # 缺的字段从被合并的行补上

        member = (
            await session.execute(
                select(LibraryPaper).where(
                    LibraryPaper.library_id == library_id, LibraryPaper.paper_id == rich_id
                )
            )
        ).scalar_one()  # 一篇论文在库里只剩一行
        assert member.status == "compiled" and member.relevance_score == 0.95

        async def count(model, column, paper_id=rich_id):
            stmt = select(func.count()).select_from(model).where(column == paper_id)
            return (await session.execute(stmt)).scalar_one()

        assert await count(PaperNote, PaperNote.paper_id) == 1
        tags = await session.execute(
            select(UserPaperTag.name).where(UserPaperTag.paper_id == rich_id)
        )
        assert sorted(tags.scalars()) == ["icml", "rag"]
        assert await count(PaperExtraction, PaperExtraction.paper_id) == 1
        assert await count(PaperCitation, PaperCitation.citing_paper_id) == 1
        assert await count(PaperAsset, PaperAsset.paper_id) == 1
        assert await count(PaperContentVersion, PaperContentVersion.paper_id) == 1
        assert await count(LiteratureSearchHit, LiteratureSearchHit.paper_id) == 1
        cited = await session.execute(
            select(PaperCitation.cited_paper_id).where(PaperCitation.citing_paper_id == citing_id)
        )
        assert cited.scalar_one() == rich_id  # 别人引用它的边跟着改指，没有被置空

        # 再跑一遍什么都不做
        assert await paper_merge.merge_library_duplicates(session, library_id=library_id) == []
        assert await paper_merge.merge_all_library_duplicates(session) == 0
        assert len(await _library_titles(session, library_id)) == 3


async def test_library_merge_leaves_lookalikes_alone(client):
    project_id = await _project(client, "dedup-keep@example.com")
    async with get_sessionmaker()() as session:
        await _add_realm_from_arxiv(session, project_id)
        for index, fields in enumerate(
            [
                {"title": REALM_OPENALEX, "year": 2023, "authors": GUU_OPENALEX},  # 隔三年
                {"title": REALM_OPENALEX, "year": 2020, "authors": [{"name": "Ada Lovelace"}]},
                {"title": REALM_OPENALEX, "year": 2020, "authors": None},  # 没作者：不敢合
                {"title": REALM_ARXIV, "year": 2020, "authors": GUU, "arxiv_id": "2003.00001"},
            ]
        ):
            await add_paper(session, project_id=project_id, dedup_key=f"k:{index}", **fields)
        await session.commit()
        library = await get_library_for_project(session, uuid.UUID(project_id))

        assert await paper_merge.merge_library_duplicates(session, library_id=library.id) == []
        assert len(await _library_titles(session, library.id)) == 5
