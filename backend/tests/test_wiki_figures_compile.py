"""图文交织 wiki（docs/api-lit.md §6.6）：透明图铺白底、图文编译、recompile、导出重写。"""

import io
import uuid
import zipfile

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm.fake import FakeProvider
from app.core.llm.router import LLMRouter
from app.models.llm_config import LLMUsage
from app.models.paper import Paper
from app.services.literature.pdf_extract import (
    _flatten_png_white,
    extract_figures,
    figure_path,
    save_pdf,
)
from app.services.wiki_compile import compile_paper, strip_invalid_figure_markers
from tests.conftest import register_and_login


def _transparent_png_bytes(width: int = 400, height: int = 300) -> bytes:
    """RGBA PNG：整体透明（RGB 通道全黑），中心 100×100 不透明红块。"""
    from PIL import Image

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for x in range(width // 2 - 50, width // 2 + 50):
        for y in range(height // 2 - 50, height // 2 + 50):
            img.putpixel((x, y), (255, 0, 0, 255))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _opaque_png_bytes(width: int, height: int) -> bytes:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height))
    pix.clear_with(90)
    return pix.tobytes("png")


def _pdf_with_streams(streams: list[bytes]) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 60), "Full text: research agents with figures.")
    y = 80.0
    for stream in streams:
        page.insert_image(pymupdf.Rect(72, y, 272, y + 100), stream=stream)
        y += 110
    data = doc.tobytes()
    doc.close()
    return data


# ---- 1. 黑底图修复：SMask 合并 + 透明铺白底 ----


async def test_transparent_figure_flattened_to_white(tmp_path):
    """RGBA 图嵌 PDF 会拆成 base+SMask；提取须合并蒙版并铺白底，不得出黑底。"""
    from PIL import Image

    pdf_path = tmp_path / "alpha.pdf"
    pdf_path.write_bytes(_pdf_with_streams([_transparent_png_bytes()]))
    paper_id = str(uuid.uuid4())

    figures = await extract_figures(paper_id, pdf_path)
    assert [f["index"] for f in figures] == [0]
    with Image.open(figure_path(paper_id, 0)) as img:
        assert img.mode == "RGB"  # 无 alpha 通道
        # 透明角点铺白（未合并 SMask 时 RGB 通道为黑）
        assert img.getpixel((0, 0)) == (255, 255, 255)
        assert img.getpixel((img.width - 1, img.height - 1)) == (255, 255, 255)
        r, g, b = img.getpixel((img.width // 2, img.height // 2))
        assert r > 200 and g < 60 and b < 60  # 不透明红块保留


def test_flatten_png_white_handles_la_and_palette_transparency():
    """Pillow 铺白单元路径：LA / P+transparency 模式也归一化为白底 RGB。"""
    from PIL import Image

    la = Image.new("LA", (10, 10), (0, 0))  # 全透明灰度
    buf = io.BytesIO()
    la.save(buf, format="PNG")
    with Image.open(io.BytesIO(_flatten_png_white(buf.getvalue()))) as out:
        assert out.mode == "RGB" and out.getpixel((0, 0)) == (255, 255, 255)

    palette = Image.new("P", (10, 10), 0)
    palette.info["transparency"] = 0  # 调色板第 0 色透明
    buf = io.BytesIO()
    palette.save(buf, format="PNG", transparency=0)
    with Image.open(io.BytesIO(_flatten_png_white(buf.getvalue()))) as out:
        assert out.mode == "RGB" and out.getpixel((5, 5)) == (255, 255, 255)


# ---- 2. 图文编译：多模态标记插入 + 无效标记剥除 ----


def _paper_stub(**kwargs) -> Paper:
    return Paper(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        title="Interleaved Paper",
        abstract="A paper about agents.",
        status="fetched",
        **kwargs,
    )


async def test_compile_paper_multimodal_inserts_marker(app):
    paper = _paper_stub(
        figures=[
            {
                "index": 0,
                "page": 1,
                "width": 400,
                "height": 300,
                "caption": "架构图",
                "important": True,
            },
            {
                "index": 1,
                "page": 2,
                "width": 300,
                "height": 200,
                "caption": None,
                "important": False,
            },
        ]
    )
    figure_path(str(paper.id), 0).write_bytes(_opaque_png_bytes(40, 30))

    content = await compile_paper(paper, statement="研究方向", llm=LLMRouter())
    assert "![[fig:0]]" in content  # fake：多模态编译插入图片标记
    assert "[[Agent]]" in content  # 双链仍在


class _InvalidMarkerLibrarian(FakeProvider):
    """图文编译响应里夹带无效标记 ![[fig:99]]（应被整行剥除）。"""

    async def complete(self, messages, *, model, temperature=0.7, max_tokens=None, images=None):
        result = await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, images=images
        )
        if images:
            result.content += "\n看这张不存在的图：![[fig:99]]\n\n结尾段落。\n"
        return result


async def test_compile_paper_strips_invalid_markers(app):
    paper = _paper_stub(
        figures=[
            {"index": 0, "page": 1, "width": 400, "height": 300, "caption": None, "important": True}
        ]
    )
    figure_path(str(paper.id), 0).write_bytes(_opaque_png_bytes(40, 30))
    router = LLMRouter()
    router._providers[("fake", None, "")] = _InvalidMarkerLibrarian()

    content = await compile_paper(paper, statement="研究方向", llm=router)
    assert "![[fig:0]]" in content  # 有效标记保留
    assert "fig:99" not in content and "看这张不存在的图" not in content  # 整行剥除
    assert "结尾段落。" in content  # 其余行不受影响

    # 纯函数行为：只剥含无效标记的行
    stripped = strip_invalid_figure_markers("a\n![[fig:1]]\n![[fig:2]]\nb\n", {1})
    assert stripped == "a\n![[fig:1]]\nb\n"


# ---- 3. POST /papers/{id}/recompile ----


async def _setup_paper(client, *, status: str = "scored"):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "recompile-proj"}, headers=headers)
    project_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        paper = Paper(
            project_id=uuid.UUID(project_id),
            source="manual",
            title="Recompiled Paper",
            abstract="Agents doing research.",
            status=status,
        )
        session.add(paper)
        await session.commit()
        paper_id = str(paper.id)
    return project_id, headers, paper_id


async def test_recompile_with_pdf_full_flow(client):
    project_id, headers, paper_id = await _setup_paper(client, status="scored")
    pdf_path = save_pdf(paper_id, _pdf_with_streams([_opaque_png_bytes(400, 300)]))
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, uuid.UUID(paper_id))
        paper.pdf_path = str(pdf_path)
        await session.commit()

    resp = await client.post(f"/api/papers/{paper_id}/recompile", headers=headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert "![[fig:0]]" in detail["wiki_content"]  # 图文编译带标记
    assert detail["status"] == "compiled"  # scored 升为 compiled
    # figures：先 extract（原 null）再 annotate（fake VLM 图注）
    assert detail["figures"] == [
        {
            "index": 0,
            "page": 1,
            "width": 400,
            "height": 300,
            "caption": "（fake）图注",
            "important": True,
        }
    ]
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, uuid.UUID(paper_id))
        assert paper.compiled_at is not None
        # LLMUsage 记账归属 project（annotate + 编译各 1 次 librarian 调用）
        rows = (
            (await session.execute(select(LLMUsage).where(LLMUsage.stage == "librarian")))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert all(str(r.project_id) == project_id for r in rows)

    # 再跑一次：覆盖 wiki_content，仍成功（重跑 annotate + 编译）
    resp = await client.post(f"/api/papers/{paper_id}/recompile", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "compiled"


async def test_recompile_without_pdf_text_only_keeps_status(client):
    _, headers, paper_id = await _setup_paper(client, status="included")

    resp = await client.post(f"/api/papers/{paper_id}/recompile", headers=headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["wiki_content"].startswith("## TL;DR")  # 摘要降级纯文字编译
    assert "![[fig:" not in detail["wiki_content"]  # 无图不插标记
    assert detail["figures"] == [] and detail["status"] == "included"  # included 不动


async def test_recompile_non_member_404(client):
    _, _headers, paper_id = await _setup_paper(client)
    outsider = await register_and_login(client, email="outsider@example.com")
    resp = await client.post(
        f"/api/papers/{paper_id}/recompile", headers={"Authorization": f"Bearer {outsider}"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "PAPER_NOT_FOUND"


# ---- 4. Obsidian 导出：figure 打包 + 标记重写 ----


async def test_obsidian_export_rewrites_figure_markers(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "export-proj"}, headers=headers)
    project_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        paper = Paper(
            project_id=uuid.UUID(project_id),
            source="arxiv",
            arxiv_id="2406.20001",
            title="Figured Export Paper",
            abstract="With figures.",
            wiki_content=(
                "## 方法\n\n提出 [[Agent]] 方法。\n\n![[fig:0]]\n\n## 实验结论\n\n有效。\n"
            ),
            figures=[
                {
                    "index": 0,
                    "page": 1,
                    "width": 400,
                    "height": 300,
                    "caption": "架构图",
                    "important": True,
                },
                {
                    "index": 1,
                    "page": 2,
                    "width": 300,
                    "height": 200,
                    "caption": "结果图",
                    "important": True,
                },
            ],
            status="compiled",
        )
        session.add(paper)
        await session.commit()
        paper_id = str(paper.id)
    for index in (0, 1):
        figure_path(paper_id, index).write_bytes(_opaque_png_bytes(40, 30))

    resp = await client.get(f"/api/projects/{project_id}/export/obsidian", headers=headers)
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())
    slug = "figured-export-paper"
    assert f"papers/figures/{slug}-fig-0.png" in names
    assert f"papers/figures/{slug}-fig-1.png" in names

    paper_md = zf.read(f"papers/{slug}.md").decode("utf-8")
    assert f"![fig 0](figures/{slug}-fig-0.png)" in paper_md  # 标记重写为相对路径
    assert "![[fig:" not in paper_md  # 原标记不残留
    # 正文没引用但 important 的 fig 1 追加到「重要图片」小节（带图注）
    assert "## 重要图片" in paper_md
    assert f"![fig 1](figures/{slug}-fig-1.png)" in paper_md
    assert "*结果图*" in paper_md
    assert "[[Agent]]" in paper_md  # 双链保留
