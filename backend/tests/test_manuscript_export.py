"""arXiv 清洁包导出：tar.gz 含源文件 + references.bib + .bbl，剔除 aux/log/pdf。"""

import io
import tarfile
from pathlib import Path

import pytest_asyncio

from app.services import latex_compile
from tests.test_manuscripts import _create_manuscript, _setup_project


@pytest_asyncio.fixture(autouse=True)
async def _clean_crdt():
    from app.services.crdt_rooms import reset_crdt_rooms

    yield
    await reset_crdt_rooms()


@pytest_asyncio.fixture(autouse=True)
def _stub_tectonic_bbl(monkeypatch):
    """假 tectonic：产出 main.pdf + main.bbl + main.aux（后两者用于验证收/剔）。"""

    def fake_run(binary: str, workdir: Path, main_name: str):
        (workdir / "main.pdf").write_bytes(b"%PDF")
        (workdir / "main.bbl").write_text("\\begin{thebibliography}{1}\n\\end{thebibliography}\n")
        (workdir / "main.aux").write_text("\\relax")
        return latex_compile.TectonicRun(0, "", "")

    monkeypatch.setattr(latex_compile, "_find_tectonic", lambda: "/usr/bin/tectonic")
    monkeypatch.setattr(latex_compile, "_run_tectonic_on", fake_run)


async def test_export_arxiv_clean_tarball(client):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]

    resp = await client.get(f"/api/manuscripts/{ms_id}/export/arxiv", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"
    assert "attachment" in resp.headers["content-disposition"]

    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        names = set(tar.getnames())
    # 源文件 + 自动生成的 references.bib + 生成的 .bbl 都在
    assert "main.tex" in names
    assert "references.bib" in names
    assert "main.bbl" in names
    # 编译副产物被剔除
    assert "main.pdf" not in names
    assert "main.aux" not in names
    assert not any(n.endswith(".log") for n in names)


async def test_export_arxiv_requires_membership(client):
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]
    _, other = await _setup_project(client, email="intruder@example.com")
    resp = await client.get(f"/api/manuscripts/{ms_id}/export/arxiv", headers=other)
    assert resp.status_code == 404
