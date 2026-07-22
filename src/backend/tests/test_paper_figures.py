"""论文图片（docs/api-lit.md §6.5）：提取过滤/落盘、figures API、注释与降级、多模态 payload。"""

import base64
import io
import json
import uuid

import httpx
import respx
from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.core.llm.fake import FakeProvider
from app.core.llm.openai_compat import OpenAICompatProvider
from app.core.llm.router import LLMRouter
from app.models.llm_config import LLMUsage
from app.models.paper import Paper
from app.services.figure_annotate import annotate_figures
from app.services.literature.pdf_extract import extract_figures, figure_path, save_pdf
from tests.conftest import add_paper, register_and_login


def _image_bytes(width: int, height: int, value: int = 90) -> bytes:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height))
    pix.clear_with(value)
    return pix.tobytes("png")


def _pdf_with_images(pages: list[list[tuple[int, int]]]) -> bytes:
    """按 pages 生成 PDF：每页嵌入若干 (width, height) 的图。"""
    import pymupdf

    doc = pymupdf.open()
    for page_specs in pages:
        page = doc.new_page()
        y = 72.0
        for w, h in page_specs:
            page.insert_image(pymupdf.Rect(72, y, 172, y + 60), stream=_image_bytes(w, h))
            y += 70
    data = doc.tobytes()
    doc.close()
    return data


# ---- extract_figures：过滤 / 去重 / 落盘 ----


async def test_extract_figures_filters_dedupes_and_saves(tmp_path):
    import pymupdf

    # 第 1 页：大图 400×300 + 小图 100×80（被尺寸过滤）；第 2 页复用同一 xref（去重）
    doc = pymupdf.open()
    page1 = doc.new_page()
    xref = page1.insert_image(pymupdf.Rect(72, 72, 272, 222), stream=_image_bytes(400, 300))
    page1.insert_image(pymupdf.Rect(72, 240, 122, 280), stream=_image_bytes(100, 80))
    page2 = doc.new_page()
    page2.insert_image(pymupdf.Rect(72, 72, 272, 222), xref=xref)
    pdf_path = tmp_path / "figures.pdf"
    pdf_path.write_bytes(doc.tobytes())
    doc.close()

    paper_id = str(uuid.uuid4())
    figures = await extract_figures(paper_id, pdf_path)
    assert figures == [{"index": 0, "page": 1, "width": 400, "height": 300}]
    png = figure_path(paper_id, 0)
    assert png.exists()
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert not figure_path(paper_id, 1).exists()


async def test_extract_figures_drops_near_blank(tmp_path):
    # 近空白图（铺白后白底占比过高）应被丢弃，只保留有内容的图
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    # 有内容（灰底 value=90）+ 近空白（value=252，几乎全白）
    page.insert_image(pymupdf.Rect(72, 72, 272, 222), stream=_image_bytes(400, 300, value=90))
    page.insert_image(pymupdf.Rect(72, 320, 272, 470), stream=_image_bytes(400, 300, value=252))
    pdf_path = tmp_path / "blank.pdf"
    pdf_path.write_bytes(doc.tobytes())
    doc.close()

    paper_id = str(uuid.uuid4())
    figures = await extract_figures(paper_id, pdf_path)
    # 只剩有内容那张；近空白被过滤
    assert figures == [{"index": 0, "page": 1, "width": 400, "height": 300}]


async def test_extract_figures_caption_anchored_region(tmp_path):
    # 有「Figure N」图注时，走图注锚定：渲染图注正上方那块图区域（矢量整块，不碎不空）
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()  # 612x792
    # 图注正上方一块矢量矩形当作"图"
    page.draw_rect(
        pymupdf.Rect(100, 120, 460, 340), color=(0.1, 0.2, 0.6), fill=(0.5, 0.6, 0.85), width=2
    )
    page.insert_text((100, 380), "Figure 1: A synthetic vector figure.")
    pdf_path = tmp_path / "caption.pdf"
    pdf_path.write_bytes(doc.tobytes())
    doc.close()

    paper_id = str(uuid.uuid4())
    figures = await extract_figures(paper_id, pdf_path)
    assert len(figures) == 1
    assert figures[0]["page"] == 1
    # 区域覆盖整块矩形（约 360x220pt @200dpi），远大于图注文字本身
    assert figures[0]["width"] > 300 and figures[0]["height"] > 200
    assert figure_path(paper_id, 0).exists()


async def test_extract_figures_page_round_robin_cap(tmp_path):
    # 14 页各 1 图 → 上限 12：按页轮转选优，前 12 页各 1 张；编号仍按页码顺序
    pdf_path = tmp_path / "many.pdf"
    pdf_path.write_bytes(_pdf_with_images([[(200 + 10 * i, 150 + 5 * i)] for i in range(14)]))
    paper_id = str(uuid.uuid4())

    figures = await extract_figures(paper_id, pdf_path)
    assert len(figures) == 12
    assert [f["index"] for f in figures] == list(range(12))
    assert [f["page"] for f in figures] == list(range(1, 13))
    for f in figures:
        assert figure_path(paper_id, f["index"]).exists()


async def test_extract_figures_per_page_limit(tmp_path):
    # 单页 5 张（尺寸各异，防 digest 去重）+ 另一页 1 张 → 单页最多取 3 张（面积降序）
    pdf_path = tmp_path / "crowded.pdf"
    pdf_path.write_bytes(_pdf_with_images([[(300 + 2 * i, 200) for i in range(5)], [(250, 180)]]))
    paper_id = str(uuid.uuid4())

    figures = await extract_figures(paper_id, pdf_path)
    pages = [f["page"] for f in figures]
    assert pages.count(1) == 3 and pages.count(2) == 1
    # 取的是面积最大的 3 张（300/302 被淘汰），编号仍按文中出现顺序
    page1_widths = [f["width"] for f in figures if f["page"] == 1]
    assert page1_widths == [304, 306, 308]


# ---- figures API：三端点 / 幂等 / force / 越界 / 成员校验 ----


async def _setup_paper(client, *, email: str = "alice@example.com"):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "fig-proj"}, headers=headers)
    project_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        paper = await add_paper(session,
            project_id=uuid.UUID(project_id),
            source="manual",
            title="Figured Paper",
            abstract="A paper with figures.",
            status="included",
        )
        session.add(paper)
        await session.commit()
        paper_id = str(paper.id)
    return project_id, headers, paper_id


async def _librarian_usage_count() -> int:
    async with get_sessionmaker()() as session:
        stmt = select(func.count()).where(LLMUsage.stage == "librarian")
        return int((await session.execute(stmt)).scalar_one())


async def test_figures_api_extract_annotate_idempotent_force(client):
    project_id, headers, paper_id = await _setup_paper(client)

    # 无 figures → []；无 PDF → 404
    resp = await client.get(f"/api/papers/{paper_id}/figures", headers=headers)
    assert resp.status_code == 200 and resp.json() == []
    resp = await client.post(f"/api/papers/{paper_id}/extract-figures", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "PDF_NOT_AVAILABLE"

    # 落盘 PDF（2 张有效图 + 1 张过小被滤）
    pdf_path = save_pdf(paper_id, _pdf_with_images([[(400, 300), (100, 80)], [(360, 240)]]))
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, uuid.UUID(paper_id))
        paper.pdf_path = str(pdf_path)
        await session.commit()

    resp = await client.post(f"/api/papers/{paper_id}/extract-figures", headers=headers)
    assert resp.status_code == 200, resp.text
    figures = resp.json()["figures"]
    assert [(f["index"], f["page"], f["width"], f["height"]) for f in figures] == [
        (0, 1, 400, 300),
        (1, 2, 360, 240),
    ]
    # fake VLM：选 index 0/1 配确定性图注
    assert all(f["important"] is True and f["caption"] == "（fake）图注" for f in figures)
    assert await _librarian_usage_count() == 1

    # GET figures / PaperDetail.figures 一致
    resp = await client.get(f"/api/papers/{paper_id}/figures", headers=headers)
    assert resp.json() == figures
    resp = await client.get(f"/api/papers/{paper_id}", headers=headers)
    assert resp.json()["figures"] == figures

    # 图片文件端点：PNG / 越界 404
    resp = await client.get(f"/api/papers/{paper_id}/figures/0/image", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    resp = await client.get(f"/api/papers/{paper_id}/figures/5/image", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "FIGURE_NOT_FOUND"

    # 幂等：已有 figures 且非 force 不再调 LLM
    resp = await client.post(f"/api/papers/{paper_id}/extract-figures", headers=headers)
    assert resp.status_code == 200 and resp.json()["figures"] == figures
    assert await _librarian_usage_count() == 1
    # force：重提 + 重新注释
    resp = await client.post(f"/api/papers/{paper_id}/extract-figures?force=true", headers=headers)
    assert resp.status_code == 200 and resp.json()["figures"] == figures
    assert await _librarian_usage_count() == 2

    # 非项目成员 404
    other = await register_and_login(client, email="fig-outsider@example.com")
    other_headers = {"Authorization": f"Bearer {other}"}
    for method, url in (
        ("GET", f"/api/papers/{paper_id}/figures"),
        ("GET", f"/api/papers/{paper_id}/figures/0/image"),
        ("POST", f"/api/papers/{paper_id}/extract-figures"),
    ):
        resp = await client.request(method, url, headers=other_headers)
        assert resp.status_code == 404, url


# ---- annotate_figures：fake 路径 + 失败降级 ----


def _paper_stub() -> Paper:
    return Paper(id=uuid.uuid4(), title="Stub Paper", abstract="stub")


def _candidate(index: int, *, page: int = 1, width: int = 400, height: int = 300) -> dict:
    return {"index": index, "page": page, "width": width, "height": height}


async def test_annotate_figures_fake_selection_and_oversize_skip(app):
    paper = _paper_stub()
    candidates = [_candidate(0), _candidate(1, page=2), _candidate(2, page=3, width=500)]
    for c in candidates[:2]:
        figure_path(str(paper.id), c["index"]).write_bytes(_image_bytes(40, 30))
    # 第 3 张 >4MB：不送 LLM，但仍保留在 figures 里
    figure_path(str(paper.id), 2).write_bytes(b"\x89PNG" + b"\x00" * (4 * 1024 * 1024 + 1))

    merged = await annotate_figures(paper, candidates, llm=LLMRouter())
    assert paper.figures == merged
    assert [f["index"] for f in merged] == [0, 1, 2]
    assert [f["important"] for f in merged] == [True, True, False]
    assert merged[0]["caption"] == "（fake）图注" and merged[2]["caption"] is None
    assert merged[1]["page"] == 2  # 候选元信息原样保留


class _FailingVLM(FakeProvider):
    """收到图片时抛错（触发重试 + 降级；纯文本请求仍走 fake 正常路径）。"""

    def __init__(self) -> None:
        self.image_calls = 0

    async def complete(self, messages, *, model, temperature=0.7, max_tokens=None, images=None):
        if images:
            self.image_calls += 1
            raise RuntimeError("vision model unavailable")
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens
        )


async def test_annotate_figures_degrades_to_top4_by_area(app):
    paper = _paper_stub()
    # 6 张候选，面积随 index 递减 → 降级应标前 4 张 important
    candidates = [_candidate(i, width=500 - 50 * i, height=300) for i in range(6)]
    for c in candidates:
        figure_path(str(paper.id), c["index"]).write_bytes(_image_bytes(40, 30))

    provider = _FailingVLM()
    router = LLMRouter()
    router._providers[("fake", None, "")] = provider
    merged = await annotate_figures(paper, candidates, llm=router)

    assert provider.image_calls == 2  # 失败重试 1 次
    assert [f["important"] for f in merged] == [True] * 4 + [False] * 2
    assert all(f["caption"] is None for f in merged)
    assert paper.figures == merged


def test_prepare_image_for_llm_downscales_oversized():
    from PIL import Image

    from app.services.figure_annotate import _MAX_IMAGE_DIM, prepare_image_for_llm

    # 已在限制内：原样返回
    small = _image_bytes(400, 300)
    assert prepare_image_for_llm(small) == small

    # 单边超 8000px：降采样到 ≤7600px，仍是可解码的图
    big = _image_bytes(9000, 200)
    out = prepare_image_for_llm(big)
    assert out is not None and out != big
    with Image.open(io.BytesIO(out)) as im:
        assert max(im.size) <= _MAX_IMAGE_DIM

    # 无法解码且超体积：丢弃（None），调用方跳过而非把坏图发出去触发 400
    assert prepare_image_for_llm(b"\x89PNG" + b"\x00" * (4 * 1024 * 1024)) is None


# ---- openai_compat：images → data-url content parts ----


@respx.mock
async def test_openai_compat_images_payload():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "vlm-1",
                "choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    respx.post("https://llm.test/v1/chat/completions").mock(side_effect=handler)
    provider = OpenAICompatProvider(base_url="https://llm.test/v1", api_key="k")
    result = await provider.complete(
        [Message(role="system", content="挑图"), Message(role="user", content="候选图")],
        model="vlm-1",
        images=[b"png-one", b"png-two"],
    )
    assert result.content == "[]"

    msgs = captured["messages"]
    assert msgs[0] == {"role": "system", "content": "挑图"}  # system 不动
    parts = msgs[1]["content"]
    assert parts[0] == {"type": "text", "text": "候选图"}
    assert [p["type"] for p in parts[1:]] == ["image_url", "image_url"]
    urls = [p["image_url"]["url"] for p in parts[1:]]
    assert all(u.startswith("data:image/png;base64,") for u in urls)
    assert base64.b64decode(urls[0].split(",", 1)[1]) == b"png-one"
    assert base64.b64decode(urls[1].split(",", 1)[1]) == b"png-two"

    # 不带 images 时 content 保持纯字符串
    await provider.complete([Message(role="user", content="纯文本")], model="vlm-1")
    assert captured["messages"] == [{"role": "user", "content": "纯文本"}]


def test_figure_files_do_not_leak_paths():
    """契约：文件路径不出 API —— figure dict 只含 index/page/width/height/caption/important。"""
    from app.schemas.paper import PaperFigure

    fig = PaperFigure(index=0, page=1, width=400, height=300)
    assert set(fig.model_dump()) == {
        "index",
        "page",
        "width",
        "height",
        "caption",
        "kind",
        "important",
    }
    assert fig.caption is None and fig.important is False
