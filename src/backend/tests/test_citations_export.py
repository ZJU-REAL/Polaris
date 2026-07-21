"""引用导出（docs/api-lit.md §6）：BibTeX key 规则 / entry 类型 / eprint + CSL-JSON 结构。"""

import json
import uuid

from app.core.db import get_sessionmaker
from app.models.paper import Paper
from app.services.citations import split_author_name
from tests.conftest import register_and_login


async def _setup(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "cite-proj"}, headers=headers)
    project_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        pid = uuid.UUID(project_id)
        p1 = Paper(  # venue 含 Proceedings → inproceedings；与 p2 的 key 冲突（后缀 a）
            project_id=pid,
            title="The Great Agent Benchmark",
            authors=[{"name": "Alice Smith"}, {"name": "Bob Jones"}],
            year=2024,
            venue="Proceedings of NeurIPS",
            doi="10.1/one",
            url="https://example.org/one",
            status="included",
        )
        p2 = Paper(  # 有 venue（期刊）→ article + journal 字段
            project_id=pid,
            title="Great Expectations of LLMs",
            authors=[{"name": "Smith, Alice"}],
            year=2024,
            venue="Nature",
            status="compiled",
        )
        p3 = Paper(  # 无 venue → misc；arxiv 论文带 eprint；中文名整个作 family
            project_id=pid,
            title="Quantum Annealing Survey",
            authors=[{"name": "张三"}],
            year=2025,
            arxiv_id="2501.00042",
            status="included",
        )
        p4 = Paper(project_id=pid, title="Excluded Paper", year=2020, status="excluded")
        session.add_all([p1, p2, p3, p4])
        await session.commit()
        p1_id = str(p1.id)
    return project_id, headers, p1_id


async def test_bibtex_export_keys_types_eprint(client):
    project_id, headers, p1_id = await _setup(client)
    resp = await client.get(
        f"/api/projects/{project_id}/export/citations?format=bibtex", headers=headers
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert ".bib" in resp.headers["content-disposition"]
    bib = resp.text

    # key = 第一作者姓小写 + year + 标题首个实义词小写（"The" 是虚词）；冲突加 a 后缀
    assert "@inproceedings{smith2024great,\n" in bib
    assert "@article{smith2024greata,\n" in bib
    assert "@misc{张三2025quantum,\n" in bib
    # entry 字段
    assert "booktitle = {Proceedings of NeurIPS}," in bib
    assert "journal = {Nature}," in bib
    assert "author = {Smith, Alice and Jones, Bob}," in bib  # First Last 归一为 Last, First
    assert "doi = {10.1/one}," in bib
    # arxiv 论文带 eprint / archivePrefix
    assert "eprint = {2501.00042}," in bib
    assert "archivePrefix = {arXiv}," in bib
    # 缺省只导出 compiled/included
    assert "Excluded Paper" not in bib

    # status 过滤复用列表语义
    resp = await client.get(
        f"/api/projects/{project_id}/export/citations?format=bibtex&status=excluded",
        headers=headers,
    )
    assert "Excluded Paper" in resp.text and "Nature" not in resp.text


async def test_csl_json_export_structure_and_filters(client):
    project_id, headers, p1_id = await _setup(client)
    # starred 过滤（个人视角）
    await client.put(f"/api/papers/{p1_id}/my-meta", json={"starred": True}, headers=headers)

    resp = await client.get(
        f"/api/projects/{project_id}/export/citations?format=csl-json", headers=headers
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    items = json.loads(resp.text)
    by_title = {item["title"]: item for item in items}
    assert set(by_title) == {
        "The Great Agent Benchmark",
        "Great Expectations of LLMs",
        "Quantum Annealing Survey",
    }

    conf = by_title["The Great Agent Benchmark"]
    assert conf["id"] == "smith2024great"
    assert conf["type"] == "paper-conference"
    assert conf["author"][0] == {"family": "Smith", "given": "Alice"}
    assert conf["issued"] == {"date-parts": [[2024]]}
    assert conf["DOI"] == "10.1/one" and conf["URL"] == "https://example.org/one"
    assert conf["container-title"] == "Proceedings of NeurIPS"
    assert by_title["Great Expectations of LLMs"]["type"] == "article-journal"
    zh = by_title["Quantum Annealing Survey"]
    assert zh["author"] == [{"family": "张三"}]  # 中文名整个作 family，无 given

    resp = await client.get(
        f"/api/projects/{project_id}/export/citations?format=csl-json&starred=true",
        headers=headers,
    )
    assert [i["title"] for i in json.loads(resp.text)] == ["The Great Agent Benchmark"]

    # 非法 format → 422；非成员 404
    resp = await client.get(
        f"/api/projects/{project_id}/export/citations?format=ris", headers=headers
    )
    assert resp.status_code == 422
    other = await register_and_login(client, email="cite-outsider@example.com")
    resp = await client.get(
        f"/api/projects/{project_id}/export/citations",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404


def test_split_author_name_formats():
    assert split_author_name("Smith, Alice") == ("Smith", "Alice")
    assert split_author_name("Alice van Smith") == ("Smith", "Alice van")
    assert split_author_name("张三") == ("张三", "")
    assert split_author_name("Curie") == ("Curie", "")
