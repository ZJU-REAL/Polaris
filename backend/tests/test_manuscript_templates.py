"""模板库测试：zip 上传建模板 / 列表合并内置 / 下载 / 用模板建稿（含二进制）/ 删除。"""

import io
import zipfile

from app.services import manuscript_templates as templates
from tests.test_manuscripts import _setup_project


def test_select_subdir_flattens_and_filters():
    """一个仓库塞了多套模板时，只取指定子目录并拍平到根（ICLR 各年份场景）。"""
    members = {
        "repo-abc/iclr2026/main.tex": b"x",
        "repo-abc/iclr2026/style.sty": b"y",
        "repo-abc/iclr2019/old.tex": b"z",
        "repo-abc/README.md": b"r",
    }
    out = templates._select_subdir(members, "iclr2026")
    assert set(out) == {"main.tex", "style.sty"}


def test_select_subdir_missing_falls_back():
    """子目录不存在 → 退回全量（剥离顶层包裹目录），不至于清空模板。"""
    members = {"repo-abc/a.tex": b"x", "repo-abc/b.sty": b"y"}
    out = templates._select_subdir(members, "nope")
    assert set(out) == {"a.tex", "b.sty"}


def test_drop_output_pdfs_keeps_figure_pdfs():
    """编译产物样例 PDF（与 .tex 同名）剔除；图片素材 PDF 保留（ICML icml_numpapers.pdf）。"""
    members = {
        "example_paper.tex": b"x",
        "example_paper.pdf": b"compiled-sample",  # 与 .tex 同名 → 丢
        "icml_numpapers.pdf": b"figure-asset",  # 无同名 .tex → 留（\includegraphics 素材）
        "icml2026.sty": b"style",
    }
    out = templates._drop_output_pdfs(members)
    assert set(out) == {"example_paper.tex", "icml_numpapers.pdf", "icml2026.sty"}


def test_substitute_unavailable_fonts():
    """Windows/mac 中文字体名（zjuthesis 用）→ Linux 可用自由字体；二进制/无关文件不动。"""
    members = {
        "config/fonts.tex": (
            b"\\setCJKmainfont{FangSong}\n\\setCJKfamilyfont{zhhei}{SimHei}\n"
            b"\\setmainfont{Times New Roman}\n"
        ),
        "main.tex": b"\\documentclass{article}\\begin{document}x\\end{document}",
        "logo.png": _PNG,  # 二进制不动
    }
    out = templates._substitute_unavailable_fonts(members)
    fonts = out["config/fonts.tex"].decode("utf-8")
    assert "{Noto Serif CJK SC}" in fonts  # FangSong →
    assert "{Noto Sans CJK SC}" in fonts  # SimHei →
    assert "{Liberation Serif}" in fonts  # Times New Roman →
    assert "FangSong" not in fonts and "SimHei" not in fonts
    assert out["main.tex"] == members["main.tex"]  # 无字体名的文件不动
    assert out["logo.png"] == _PNG  # 二进制原样


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    return buf.getvalue()


# 1x1 PNG（二进制资源，验证 is_binary 落盘）
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
)


async def test_upload_list_download_and_create(client):
    project_id, headers = await _setup_project(client)

    tex = (
        b"\\documentclass{article}\n\\usepackage{demo}\n\\begin{document}\n"
        b"{{POLARIS_TITLE}}\n\\includegraphics{logo.png}\n\\end{document}\n"
    )
    zip_bytes = _make_zip(
        {"paper/main.tex": tex, "paper/demo.sty": b"% style", "paper/logo.png": _PNG}
    )

    # 上传为项目私有模板
    resp = await client.post(
        "/api/manuscripts/templates",
        files={"file": ("tpl.zip", zip_bytes, "application/zip")},
        data={"name": "My Custom Template", "project_id": project_id, "engine": "pdflatex"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    tpl = resp.json()
    assert tpl["source"] == "uploaded"
    assert tpl["scope"] == "project"
    assert tpl["downloadable"] is True
    assert tpl["file_count"] == 3
    tpl_id = tpl["id"]

    # 列表：项目私有模板 + 官方 manifest 伪条目都在（顶层包裹目录 paper/ 已被剥离）
    resp = await client.get(f"/api/manuscripts/templates?project_id={project_id}", headers=headers)
    ids = {t["id"] for t in resp.json()}
    assert tpl_id in ids
    assert "seed:neurips2026-official" in ids  # 官方未下载伪条目
    assert "neurips2026" not in ids  # 内置简化模板不再显示

    # 不带 project_id：看不到项目私有模板
    resp = await client.get("/api/manuscripts/templates", headers=headers)
    assert tpl_id not in {t["id"] for t in resp.json()}

    # 下载回 zip
    resp = await client.get(f"/api/manuscripts/templates/{tpl_id}/download", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    dl = zipfile.ZipFile(io.BytesIO(resp.content))
    assert set(dl.namelist()) == {"main.tex", "demo.sty", "logo.png"}
    assert dl.read("logo.png") == _PNG

    # 用该模板建稿：标题注入 + 二进制文件 is_binary 落盘
    resp = await client.post(
        f"/api/projects/{project_id}/manuscripts",
        json={"title": "Paper X", "template": tpl_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    ms_id = resp.json()["id"]
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    files = {f["path"]: f for f in detail["files"]}
    # 建稿时自动补一个可见可编辑的 references.bib
    assert set(files) == {"main.tex", "demo.sty", "logo.png", "references.bib"}
    assert files["logo.png"]["readonly"] is True
    assert files["demo.sty"]["readonly"] is True
    assert files["main.tex"]["readonly"] is False
    # main.tex 标题注入
    main = (
        await client.get(
            f"/api/manuscripts/{ms_id}/files/{files['main.tex']['id']}", headers=headers
        )
    ).json()
    assert "Paper X" in main["content"]
    assert "{{POLARIS_TITLE}}" not in main["content"]

    # 删除模板（创建者可删）
    resp = await client.delete(f"/api/manuscripts/templates/{tpl_id}", headers=headers)
    assert resp.status_code == 204
    resp = await client.get(f"/api/manuscripts/templates/{tpl_id}/download", headers=headers)
    assert resp.status_code == 404


async def test_upload_global_requires_admin(client):
    # 第二个注册用户不是平台 admin（首个注册者才是）
    await _setup_project(client, email="first@example.com")
    _, headers2 = await _setup_project(client, email="second@example.com")
    zip_bytes = _make_zip(
        {"main.tex": b"\\documentclass{article}\\begin{document}x\\end{document}"}
    )
    resp = await client.post(
        "/api/manuscripts/templates",
        files={"file": ("t.zip", zip_bytes, "application/zip")},
        data={"name": "Global"},
        headers=headers2,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "ADMIN_REQUIRED_FOR_GLOBAL"


async def test_upload_rejects_zip_without_tex(client):
    project_id, headers = await _setup_project(client)
    zip_bytes = _make_zip({"readme.md": b"no tex here"})
    resp = await client.post(
        "/api/manuscripts/templates",
        files={"file": ("t.zip", zip_bytes, "application/zip")},
        data={"name": "Bad", "project_id": project_id},
        headers=headers,
    )
    assert resp.status_code == 422
