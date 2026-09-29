"""论文标识不再只认 arXiv（#821）。

以前不是 DOI 的一律当 arXiv 编号：PMID 被存成 arxiv_id、在 arXiv 上查不到；锚点只能
填 arXiv 编号，非 CS 的库等于没有引用扩展；vault 导入要求每篇都有 arXiv 编号。
"""

import httpx
import pytest
import respx

from app.services.paper_identifiers import (
    PaperRef,
    normalize_pmid,
    parse_paper_ref,
    semantic_scholar_ref,
)
from tests.test_paper_manual_add import _setup, lit_clients  # noqa: F401 — fixture 复用

# ---- 识别 ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2401.01234", PaperRef("arxiv", "2401.01234")),
        ("2401.01234v3", PaperRef("arxiv", "2401.01234v3")),
        ("arXiv:2401.01234", PaperRef("arxiv", "2401.01234")),
        ("https://arxiv.org/abs/2401.01234v2", PaperRef("arxiv", "2401.01234v2")),
        ("https://arxiv.org/pdf/2401.01234.pdf", PaperRef("arxiv", "2401.01234")),
        ("hep-th/9901001", PaperRef("arxiv", "hep-th/9901001")),
        ("math.GT/0309136", PaperRef("arxiv", "math.GT/0309136")),
        ("10.1038/s41586-020-2649-2", PaperRef("doi", "10.1038/s41586-020-2649-2")),
        (
            "https://doi.org/10.1016/j.cell.2020.01.001",
            PaperRef("doi", "10.1016/j.cell.2020.01.001"),
        ),
        ("doi:10.1000/xyz", PaperRef("doi", "10.1000/xyz")),
        ("31452104", PaperRef("pmid", "31452104")),
        ("PMID: 31452104", PaperRef("pmid", "31452104")),
        ("https://pubmed.ncbi.nlm.nih.gov/31452104/", PaperRef("pmid", "31452104")),
    ],
)
def test_each_identifier_is_recognised(raw, expected):
    assert parse_paper_ref(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "hello world", "1234567890", "0", "10.1/"])
def test_junk_is_not_guessed_as_arxiv(raw):
    """以前认不出的一律当 arXiv 编号；现在认不出就是认不出。"""
    assert parse_paper_ref(raw) is None


def test_a_pmid_is_not_mistaken_for_an_arxiv_id():
    assert parse_paper_ref("31452104").kind == "pmid"
    assert parse_paper_ref("2401.01234").kind == "arxiv"


def test_normalize_pmid_rejects_non_numbers():
    with pytest.raises(ValueError):
        normalize_pmid("PMC123")
    assert normalize_pmid("PMID:0031452104") == "31452104"


def test_semantic_scholar_ref_prefixes():
    assert semantic_scholar_ref("arxiv", "2401.01234") == "arXiv:2401.01234"
    assert semantic_scholar_ref("doi", "10.1/x") == "DOI:10.1/x"
    assert semantic_scholar_ref("pmid", "123") == "PMID:123"


# ---- 按 PMID 导入 ----

PMID_WORK = {
    "id": "https://openalex.org/W99",
    "title": "Deep Learning for Sepsis Prediction",
    "doi": "https://doi.org/10.1001/jama.2019.1",
    "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/31452104"},
    "publication_year": 2019,
    "primary_location": {"source": {"display_name": "JAMA"}},
    "authorships": [{"author": {"display_name": "Ana Lee"}}],
}


@respx.mock
async def test_add_by_pmid(client, lit_clients):  # noqa: F811
    project_id, headers = await _setup(client)
    route = respx.get(url__regex=r"https://api\.openalex\.org/works/pmid:31452104.*").mock(
        return_value=httpx.Response(200, json=PMID_WORK)
    )
    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"pmid": "31452104"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert route.called
    assert body["title"] == "Deep Learning for Sepsis Prediction"
    assert body["doi"] == "10.1001/jama.2019.1"
    assert body["arxiv_id"] is None


@respx.mock
async def test_an_unknown_pmid_is_a_readable_422(client, lit_clients):  # noqa: F811
    project_id, headers = await _setup(client)
    respx.get(url__regex=r"https://api\.openalex\.org/works/pmid:.*").mock(
        return_value=httpx.Response(404)
    )
    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"pmid": "99999999"}, headers=headers
    )
    assert resp.status_code == 422
    assert "PMID" in resp.text


async def test_pmid_counts_toward_exactly_one_source(client):
    project_id, headers = await _setup(client)
    resp = await client.post(
        f"/api/projects/{project_id}/papers",
        json={"pmid": "1", "doi": "10.1/x"},
        headers=headers,
    )
    assert resp.status_code == 422


# ---- 锚点：批量解析任意标识 ----


@respx.mock
async def test_resolve_batch_accepts_mixed_identifiers(client, lit_clients):  # noqa: F811
    _project_id, headers = await _setup(client)
    respx.get(url__regex=r"https://api\.openalex\.org/works/pmid:31452104.*").mock(
        return_value=httpx.Response(200, json=PMID_WORK)
    )
    respx.get(url__regex=r"https://api\.openalex\.org/works/doi:10\.1234/landmark.*").mock(
        return_value=httpx.Response(
            200, json={"id": "W7", "title": "Landmark", "doi": "https://doi.org/10.1234/landmark"}
        )
    )
    resp = await client.post(
        "/api/papers/resolve-batch",
        json={"refs": ["PMID: 31452104", "https://doi.org/10.1234/landmark", "not an id"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["kind"] for i in items] == ["pmid", "doi", None]
    assert items[0]["pmid"] == "31452104"
    assert items[0]["title"] == "Deep Learning for Sepsis Prediction"
    assert items[1]["doi"] == "10.1234/landmark"
    assert items[1]["title"] == "Landmark"
    assert items[2]["error"] and "not an id" in items[2]["error"]


async def test_resolve_batch_still_takes_arxiv_ids(client):
    """旧入口照旧能用：前端升级前的调用不能坏。"""
    _project_id, headers = await _setup(client)
    resp = await client.post("/api/papers/resolve-batch", json={}, headers=headers)
    assert resp.status_code == 422  # 两者都没给


# ---- 引用扩展的种子 ----


def test_anchor_refs_cover_every_identifier():
    from app.agents.voyage.actions_wiki import _anchor_ref

    assert _anchor_ref({"arxiv_id": "2401.01234v2"}) == "arXiv:2401.01234"
    assert _anchor_ref({"doi": "10.1/x"}) == "DOI:10.1/x"
    assert _anchor_ref({"pmid": "123"}) == "PMID:123"
    assert _anchor_ref({"title": "only a title"}) is None
