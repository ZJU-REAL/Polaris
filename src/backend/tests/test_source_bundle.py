"""源码包（桌面端本地编译的输入）与编译产物剔除规则。

同时覆盖一个既有隐患的回归：arXiv 导出原先按后缀剔除 ``.pdf``，会把插图 PDF
一起删掉——而 PDF 是 LaTeX 最常见的插图格式。
"""

import io
import tarfile

from app.services import latex_compile
from app.services.latex_compile import _is_build_artifact
from tests.test_manuscript_files import _new_ms

# 最小合法 PDF（当作插图用，只要求是二进制且后缀为 .pdf）
_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def _members(data: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name for m in tar.getmembers() if m.isfile()}


async def _upload(client, headers, ms_id, path: str, blob: bytes, mime: str):
    return await client.post(
        f"/api/manuscripts/{ms_id}/files/upload",
        files={"file": (path.rsplit("/", 1)[-1], blob, mime)},
        data={"path": path},
        headers=headers,
    )


class TestBuildArtifactRules:
    """剔除规则是纯函数，直接单测——这里最容易一改就误伤用户文件。"""

    def test_drops_intermediate_suffixes(self):
        for name in ("main.aux", "main.log", "main.fdb_latexmk", "main.bcf"):
            assert _is_build_artifact(name, "main") is True

    def test_drops_compiled_pdf_only_at_root(self):
        assert _is_build_artifact("main.pdf", "main") is True
        # 同名文件出现在子目录里是插图，不是编译产物
        assert _is_build_artifact("figures/main.pdf", "main") is False

    def test_keeps_figure_pdfs(self):
        assert _is_build_artifact("img/plot.pdf", "main") is False
        assert _is_build_artifact("figures/exp-1.pdf", "main") is False

    def test_drops_synctex_and_biber_xml_by_name_not_suffix(self):
        assert _is_build_artifact("main.synctex.gz", "main") is True
        assert _is_build_artifact("main.run.xml", "main") is True
        # 后缀相同但不是编译产物的文件必须留下
        assert _is_build_artifact("data/results.xml", "main") is False
        assert _is_build_artifact("data/corpus.gz", "main") is False

    def test_no_main_stem_keeps_all_pdfs(self):
        assert _is_build_artifact("main.pdf", None) is False


async def test_source_bundle_contains_sources_and_figures(client):
    headers, ms_id = await _new_ms(client)
    resp = await _upload(client, headers, ms_id, "img/plot.pdf", _PDF, "application/pdf")
    assert resp.status_code == 201

    resp = await client.get(f"/api/manuscripts/{ms_id}/source-bundle", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/gzip"

    names = _members(resp.content)
    assert "main.tex" in names
    # 回归点：插图 PDF 必须在包里
    assert "img/plot.pdf" in names


async def test_source_bundle_filename_carries_digest(client):
    """digest 放在文件名里，客户端不必读自定义响应头（跨域读自定义头还要配 expose_headers）。"""
    headers, ms_id = await _new_ms(client)

    digest = (await client.get(f"/api/manuscripts/{ms_id}/source-digest", headers=headers)).json()[
        "digest"
    ]
    assert len(digest) == 64

    resp = await client.get(f"/api/manuscripts/{ms_id}/source-bundle", headers=headers)
    assert f'filename="source-{digest}.tar.gz"' in resp.headers["content-disposition"]


async def test_digest_is_stable_and_tracks_content(client):
    headers, ms_id = await _new_ms(client)

    async def digest() -> str:
        resp = await client.get(f"/api/manuscripts/{ms_id}/source-digest", headers=headers)
        assert resp.status_code == 200, resp.text
        return resp.json()["digest"]

    first = await digest()
    assert await digest() == first  # 源没动，指纹必须稳定（否则缓存永远失效）

    # 内容编辑走 CRDT，这里用「新增一个源文件」来改变源集合
    resp = await client.post(
        f"/api/manuscripts/{ms_id}/files",
        json={"path": "sections/extra.tex", "content": "\\section{Extra}"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text

    assert await digest() != first  # 源变了指纹必须变（digest 就是缓存 key）


async def test_arxiv_export_keeps_figure_pdfs(client, monkeypatch):
    """回归：arXiv 导出原先按 .pdf 后缀一刀切，会把插图 PDF 删掉。"""
    headers, ms_id = await _new_ms(client)
    resp = await _upload(client, headers, ms_id, "img/plot.pdf", _PDF, "application/pdf")
    assert resp.status_code == 201

    # 不实际调编译器：texbase 镜像里 tectonic 是装了的，真编一遍要几十秒
    monkeypatch.setattr(latex_compile, "_find_tectonic", lambda: None)

    resp = await client.get(f"/api/manuscripts/{ms_id}/export/arxiv", headers=headers)
    assert resp.status_code == 200, resp.text
    assert "img/plot.pdf" in _members(resp.content)


async def test_source_bundle_requires_membership(client):
    from tests.test_manuscripts import _setup_project

    headers, ms_id = await _new_ms(client)
    _, other_headers = await _setup_project(client, email="mallory@example.com")
    resp = await client.get(f"/api/manuscripts/{ms_id}/source-bundle", headers=other_headers)
    assert resp.status_code in (403, 404)
