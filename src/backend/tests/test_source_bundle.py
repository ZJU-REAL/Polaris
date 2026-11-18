"""arXiv 源码包的编译产物剔除规则。

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
