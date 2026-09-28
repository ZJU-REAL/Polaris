"""学科决定去哪里找文献，而不是代码写死 arXiv（#821）。

以前新建文献库没选来源就等于只用 arXiv，建库表单上摆着一排 cs.* 分类：做临床、做结构
的人第一步就被问一个他的领域里不存在的分类体系。现在默认来源属于学科包；不选学科就用
部署默认（管理员在文献检索设置里配的来源）；存量库（来源为空）仍按当年的含义只用 arXiv。
"""

import pytest

from app.services import discipline_packs as dp
from app.services.literature import sources as literature_sources
from tests.conftest import register_and_login


async def _headers(client) -> dict[str, str]:
    token = await register_and_login(client, email="owner@example.com")
    return {"Authorization": f"Bearer {token}"}


async def _create(client, headers, **extra):
    resp = await client.post(
        "/api/libraries",
        json={"name": "L", "statement": "A research direction.", **extra},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---- 学科包格式 ----


def test_a_pack_may_bring_only_literature():
    pack = dp.parse_pack(
        {"name": "x", "title": "X", "literature": {"sources": ["openalex"]}}, origin="t"
    )
    assert pack.schemas == ()
    assert pack.literature is not None and pack.literature.sources == ("openalex",)


def test_a_pack_must_bring_something():
    with pytest.raises(dp.DisciplinePackError, match="schemas 或 literature"):
        dp.parse_pack({"name": "x", "title": "X"}, origin="t")


@pytest.mark.parametrize(
    "sources",
    [[], ["OpenAlex"], ["open alex"], ["openalex", "openalex"]],
)
def test_bad_source_lists_are_refused(sources):
    with pytest.raises(dp.DisciplinePackError):
        dp.parse_pack({"name": "x", "title": "X", "literature": {"sources": sources}}, origin="t")


def test_every_builtin_pack_names_registered_sources():
    """包里写一个注册表不认识的源，建出来的库会少一个源而没人发现。"""
    registered = {sid for sid, _ in literature_sources.sources_with_capability("search")}
    for pack in dp.discover_packs():
        if pack.literature is not None:
            assert set(pack.literature.sources) <= registered, pack.name


def test_computer_science_is_a_discipline_not_the_default():
    packs = {p.name: p for p in dp.discover_packs()}
    assert "cs" in packs
    assert "arxiv" in packs["cs"].literature.sources
    assert "cs.LG" in packs["cs"].literature.arxiv_categories
    # 代码里不再有自己的默认来源：不选学科 = 跟部署默认走
    assert dp.pack_sources(None) is None


def test_life_science_packs_default_to_biomedical_sources():
    assert dp.pack_sources("clinical")[0] == "pubmed"
    assert dp.pack_sources("wetlab")[0] == "pubmed"
    assert "arxiv" not in dp.pack_sources("structural")


def test_an_unknown_discipline_follows_the_deployment_default():
    assert dp.pack_sources("nope") is None


# ---- 新建文献库 ----


async def test_new_library_without_discipline_uses_the_deployment_sources(client):
    """「通用」库用管理员配的来源；管理员改了，之后新建的库跟着变。"""
    from app.services.literature_settings import DEFAULTS

    headers = await _headers(client)
    lib = await _create(client, headers)
    assert lib["definition"]["keywords"]["sources"] == DEFAULTS["sources"]

    resp = await client.put(
        "/api/admin/settings/literature-search",
        json={"sources": ["europepmc", "crossref"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    lib = await _create(client, headers)
    assert lib["definition"]["keywords"]["sources"] == ["europepmc", "crossref"]


async def test_new_library_follows_its_discipline(client):
    headers = await _headers(client)
    cs = await _create(client, headers, discipline="cs")
    assert cs["definition"]["keywords"]["sources"] == ["arxiv", "openalex"]
    clinical = await _create(client, headers, discipline="clinical")
    assert clinical["definition"]["keywords"]["sources"][0] == "pubmed"


async def test_explicit_sources_are_kept(client):
    headers = await _headers(client)
    lib = await _create(
        client, headers, discipline="cs", keywords={"sources": ["crossref"], "include": ["x"]}
    )
    assert lib["definition"]["keywords"]["sources"] == ["crossref"]
    assert lib["definition"]["keywords"]["include"] == ["x"]


async def test_empty_sources_get_the_discipline_default(client):
    """表单上什么都没勾、只填了关键词时会发 sources: []——不能存成空，否则读取侧会
    按存量库的含义当成「只用 arXiv」。"""
    headers = await _headers(client)
    lib = await _create(
        client, headers, discipline="wetlab", keywords={"sources": [], "include": ["crispr"]}
    )
    assert lib["definition"]["keywords"]["sources"][0] == "pubmed"


# ---- 学科清单接口 ----


async def test_discipline_list_carries_literature_defaults(client):
    headers = await _headers(client)
    resp = await client.get("/api/disciplines", headers=headers)
    assert resp.status_code == 200
    by_name = {d["name"]: d for d in resp.json()}
    assert by_name["cs"]["schema_count"] == 0
    assert "cs.AI" in by_name["cs"]["arxiv_categories"]
    assert by_name["clinical"]["arxiv_categories"] == []
    assert by_name["clinical"]["sources"][0] == "pubmed"


async def test_general_defaults_endpoint_reports_the_deployment_sources(client):
    from app.services.literature_settings import DEFAULTS

    headers = await _headers(client)
    resp = await client.get("/api/disciplines/defaults", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"sources": DEFAULTS["sources"]}
    await client.put(
        "/api/admin/settings/literature-search", json={"sources": ["pubmed"]}, headers=headers
    )
    resp = await client.get("/api/disciplines/defaults", headers=headers)
    assert resp.json() == {"sources": ["pubmed"]}
