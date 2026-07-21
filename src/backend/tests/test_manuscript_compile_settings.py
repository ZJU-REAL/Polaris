"""Overleaf 式编译设置（入口主文件 + 编译器）与结构化初始化。"""

import pytest

from app.services import latex_compile
from app.services import manuscripts as manuscripts_service
from tests.test_manuscripts import _create_manuscript, _setup_project


def test_build_structured_document_adds_markers():
    src = (
        "\\documentclass{article}\n\\usepackage{neurips_2026}\n\\title{X}\n"
        "\\begin{document}\nold body text\n\\bibliographystyle{plainnat}\n"
        "\\bibliography{references}\n\\end{document}\n"
    )
    out = manuscripts_service.build_structured_document(src)
    assert "\\usepackage{neurips_2026}" in out  # preamble 保留
    assert "\\maketitle" in out  # 有 \title → 保留 \maketitle
    for key in ("abstract", "introduction", "method", "conclusion"):
        assert f"% POLARIS_SECTION: {key}" in out
    assert "\\bibliography{references}" in out  # 原 bib 声明保留
    assert "old body text" not in out  # document 环境正文被替换成骨架


def test_build_structured_document_requires_document_env():
    with pytest.raises(manuscripts_service.StructureError):
        manuscripts_service.build_structured_document("\\documentclass{article} no doc env")


def test_engine_argv_dispatch():
    assert latex_compile._engine_argv("tectonic", "/t", "main.tex")[-1] == "main.tex"
    assert "-pdf" in latex_compile._engine_argv("pdflatex", "/latexmk", "paper.tex")
    assert "-pdfxe" in latex_compile._engine_argv("xelatex", "/latexmk", "paper.tex")
    assert "-pdflua" in latex_compile._engine_argv("lualatex", "/latexmk", "paper.tex")


async def test_manuscript_carries_main_tex_and_engine(client):
    project_id, headers = await _setup_project(client)
    ms_id = (await _create_manuscript(client, headers, project_id)).json()["id"]
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert detail["main_tex"] == "main.tex"
    assert detail["engine"] in ("tectonic", "pdflatex", "xelatex", "lualatex")


async def test_update_main_tex_and_engine(client):
    project_id, headers = await _setup_project(client)
    ms_id = (await _create_manuscript(client, headers, project_id)).json()["id"]

    # 切换编译器
    r = await client.patch(f"/api/manuscripts/{ms_id}", json={"engine": "xelatex"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["engine"] == "xelatex"

    # 不存在的主文件 → 422 MAIN_TEX_NOT_FOUND
    r = await client.patch(
        f"/api/manuscripts/{ms_id}", json={"main_tex": "nope.tex"}, headers=headers
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "MAIN_TEX_NOT_FOUND"

    # 合法主文件（main.tex 由模板展开而来）
    r = await client.patch(
        f"/api/manuscripts/{ms_id}", json={"main_tex": "main.tex"}, headers=headers
    )
    assert r.status_code == 200 and r.json()["main_tex"] == "main.tex"

    # 非法编译器 → 422（schema Literal 拦截）
    r = await client.patch(f"/api/manuscripts/{ms_id}", json={"engine": "word"}, headers=headers)
    assert r.status_code == 422


async def test_initialize_structure_creates_draft(client):
    project_id, headers = await _setup_project(client)
    ms_id = (await _create_manuscript(client, headers, project_id)).json()["id"]

    # 记录原主文件内容（初始化后应保持不变）
    detail0 = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    main0 = next(f for f in detail0["files"] if f["path"] == "main.tex")
    main_before = (
        await client.get(f"/api/manuscripts/{ms_id}/files/{main0['id']}", headers=headers)
    ).json()["content"]

    # 初始化 → 新建 draft.tex，原主文件不动，编译主文件切到 draft.tex
    r = await client.post(f"/api/manuscripts/{ms_id}/initialize-structure", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "draft.tex"
    for key in (
        "abstract",
        "introduction",
        "related_work",
        "method",
        "experimental_setup",
        "results",
        "conclusion",
    ):
        assert f"% POLARIS_SECTION: {key}" in body["content"]

    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    paths = {f["path"] for f in detail["files"]}
    assert "draft.tex" in paths and "main.tex" in paths  # 原主文件仍在
    assert detail["main_tex"] == "draft.tex"  # 编译主文件已切换

    # 原 main.tex 内容未被改动
    main_after = (
        await client.get(f"/api/manuscripts/{ms_id}/files/{main0['id']}", headers=headers)
    ).json()["content"]
    assert main_after == main_before

    # 幂等：再次初始化仍成功、仍只有一个 draft.tex、仍有标记
    r2 = await client.post(f"/api/manuscripts/{ms_id}/initialize-structure", headers=headers)
    assert r2.status_code == 200 and r2.json()["path"] == "draft.tex"
    assert "% POLARIS_SECTION: method" in r2.json()["content"]
    detail2 = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert [f["path"] for f in detail2["files"]].count("draft.tex") == 1
