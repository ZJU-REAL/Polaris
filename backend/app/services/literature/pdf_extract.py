"""PDF 落盘、全文抽取与嵌入图提取（PyMuPDF）。

文件存 settings.data_dir（默认 ./data，容器内挂 /srv/data）：
    <data_dir>/papers/<paper_id>.pdf / <paper_id>.txt
    <data_dir>/papers/<paper_id>/figures/fig_<index>.png
"""

import asyncio
import io
import logging
import re
from pathlib import Path
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 嵌入图候选过滤（docs/api-lit.md §6.5）：尺寸下限 + 按页轮转取前 N（重要图由 VLM 再筛）
FIGURE_MIN_WIDTH = 200
FIGURE_MIN_HEIGHT = 150
FIGURE_MAX_COUNT = 12
# 同一页最多取的候选数：论文页里成组出现的大量嵌入图（如轨迹截图素材）不该霸占全部名额
FIGURE_MAX_PER_PAGE = 3

# 矢量图渲染兜底：学术论文的架构图/流程图/曲线图多为矢量绘图，get_images 抓不到。
# 用 cluster_drawings 找矢量绘图簇，按区域渲染成 PNG 参与候选。
VECTOR_MIN_W_PT = 140.0  # 簇最小宽（pt）
VECTOR_MIN_H_PT = 90.0  # 簇最小高（pt）
VECTOR_MAX_PAGE_FRAC = 0.85  # 簇面积超过页面 85% 视为背景/整页边框，跳过
VECTOR_RENDER_DPI = 150
VECTOR_CLIP_MARGIN_PT = 6.0  # 渲染时四周留白，把坐标轴刻度/图例框进来


def papers_dir() -> Path:
    d = Path(get_settings().data_dir) / "papers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def figures_dir(paper_id: str) -> Path:
    d = papers_dir() / paper_id / "figures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def figure_path(paper_id: str, index: int) -> Path:
    return figures_dir(paper_id) / f"fig_{index}.png"


def save_pdf(paper_id: str, content: bytes) -> Path:
    path = papers_dir() / f"{paper_id}.pdf"
    path.write_bytes(content)
    return path


# NUL 及除 \n\t 外的 C0 控制字符（PDF 文本层常见，postgres UTF8 不接受 0x00）
_CTRL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(text: str) -> str:
    """清洗抽取文本使其可安全入库：统一换行、剔除控制字符、丢弃非法代理对。"""
    cleaned = _CTRL_CHARS_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    # PyMuPDF 偶尔产出孤立代理对，同样会让 UTF8 编码失败
    return cleaned.encode("utf-8", "ignore").decode("utf-8", "ignore")


def _extract_text_sync(pdf_path: Path) -> str:
    import pymupdf  # 延迟导入：仅在真正抽取时需要

    parts: list[str] = []
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            parts.append(page.get_text())
    return "\n".join(parts)


async def extract_full_text(paper_id: str, pdf_path: Path) -> Path:
    """抽取全文文本并落盘，返回 txt 路径（PyMuPDF 为同步库，丢线程池跑）。"""
    text = await asyncio.to_thread(_extract_text_sync, pdf_path)
    txt_path = papers_dir() / f"{paper_id}.txt"
    txt_path.write_text(sanitize_text(text), encoding="utf-8")
    return txt_path


def _flatten_png_white(png_bytes: bytes) -> bytes:
    """Pillow 归一化：透明区域铺白底（RGBA/LA/P+transparency），CMYK 转 RGB，杜绝黑底图。"""
    from PIL import Image  # 延迟导入：仅在真正抽取时需要

    with Image.open(io.BytesIO(png_bytes)) as img:
        img.load()
        if img.mode == "P":
            img = img.convert("RGBA" if "transparency" in img.info else "RGB")
        if img.mode in ("RGBA", "LA"):
            rgba = img.convert("RGBA")
            white = Image.new("RGB", rgba.size, (255, 255, 255))
            white.paste(rgba, mask=rgba.getchannel("A"))
            img = white
        elif img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()


def _vector_figure_pixmaps(page: Any) -> list[Any]:
    """矢量图兜底：把页面上的矢量绘图簇渲染为 Pixmap（架构图/曲线图通常是矢量的）。"""
    import pymupdf

    page_rect = page.rect
    page_area = max(1.0, page_rect.width * page_rect.height)
    pixmaps: list[Any] = []
    try:
        clusters = page.cluster_drawings()
    except Exception:  # noqa: BLE001 — 个别页绘图指令损坏，跳过该页
        logger.warning("cluster_drawings failed on page %d", page.number + 1, exc_info=True)
        return []
    for rect in clusters:
        if rect.width < VECTOR_MIN_W_PT or rect.height < VECTOR_MIN_H_PT:
            continue
        if rect.width * rect.height / page_area > VECTOR_MAX_PAGE_FRAC:
            continue  # 整页背景/页面边框
        clip = pymupdf.Rect(
            max(page_rect.x0, rect.x0 - VECTOR_CLIP_MARGIN_PT),
            max(page_rect.y0, rect.y0 - VECTOR_CLIP_MARGIN_PT),
            min(page_rect.x1, rect.x1 + VECTOR_CLIP_MARGIN_PT),
            min(page_rect.y1, rect.y1 + VECTOR_CLIP_MARGIN_PT),
        )
        try:
            pixmaps.append(page.get_pixmap(clip=clip, dpi=VECTOR_RENDER_DPI))
        except Exception:  # noqa: BLE001
            logger.warning("vector clip render failed on page %d", page.number + 1, exc_info=True)
    return pixmaps


def _extract_figures_sync(paper_id: str, pdf_path: Path) -> list[dict[str, Any]]:
    import pymupdf  # 延迟导入：仅在真正抽取时需要

    # (页码, 页内序号, Pixmap)：嵌入图按 xref 去重（跨页复用只取首次出现）+ 矢量簇渲染
    candidates: list[tuple[int, int, Any]] = []
    seen_xrefs: set[int] = set()
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            order = 0
            for img in page.get_images(full=True):
                xref, smask = int(img[0]), int(img[1])
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                pix = pymupdf.Pixmap(doc, xref)
                if pix.width < FIGURE_MIN_WIDTH or pix.height < FIGURE_MIN_HEIGHT:
                    continue
                if pix.colorspace is None:
                    continue  # 掩膜等无色彩空间对象，跳过
                if smask > 0 and pix.alpha == 0:
                    # 合并 SMask 软蒙版为 alpha 通道；失败退回原图（后续仍铺白）
                    try:
                        pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, smask))
                    except Exception:  # noqa: BLE001 — 蒙版损坏等，保底用无 alpha 原图
                        logger.warning(
                            "smask merge failed for paper %s xref %d", paper_id, xref, exc_info=True
                        )
                if pix.n - pix.alpha >= 4:  # CMYK 等 → RGB（保留 alpha）
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                candidates.append((page.number + 1, order, pix))
                order += 1
            # 矢量图兜底（尺寸下限与嵌入图一致，按渲染后像素判）
            for pix in _vector_figure_pixmaps(page):
                if pix.width < FIGURE_MIN_WIDTH or pix.height < FIGURE_MIN_HEIGHT:
                    continue
                candidates.append((page.number + 1, order, pix))
                order += 1

        # 按页轮转选优：每页先取面积最大的一张，轮完所有页再取第二张……
        # 保证动机图（前几页）/实验图（中后页）都有名额，不被单页大图或附录霸占；
        # 最终按 (页码, 页内序号) 恢复文中出现顺序编号
        by_page: dict[int, list[tuple[int, int, Any]]] = {}
        for cand in candidates:
            by_page.setdefault(cand[0], []).append(cand)
        for lst in by_page.values():
            lst.sort(key=lambda c: (-(c[2].width * c[2].height), c[1]))
        picked: list[tuple[int, int, Any]] = []
        rank = 0
        while len(picked) < FIGURE_MAX_COUNT and rank < FIGURE_MAX_PER_PAGE:
            advanced = False
            for page_no in sorted(by_page):
                lst = by_page[page_no]
                if rank < len(lst):
                    picked.append(lst[rank])
                    advanced = True
                    if len(picked) >= FIGURE_MAX_COUNT:
                        break
            if not advanced:
                break
            rank += 1
        selected = sorted(picked, key=lambda c: (c[0], c[1]))

        out_dir = figures_dir(paper_id)
        for old in out_dir.glob("fig_*.png"):
            old.unlink()
        figures: list[dict[str, Any]] = []
        for index, (page_no, _order, pix) in enumerate(selected):
            (out_dir / f"fig_{index}.png").write_bytes(_flatten_png_white(pix.tobytes("png")))
            figures.append(
                {"index": index, "page": page_no, "width": pix.width, "height": pix.height}
            )
    return figures


async def extract_figures(paper_id: str, pdf_path: Path) -> list[dict[str, Any]]:
    """提取 PDF 嵌入图为 PNG 落盘，返回 [{index, page, width, height}]。

    过滤：宽 ≥200 且高 ≥150、按 xref 去重、面积降序取前 8（编号按文中页码顺序）；
    PyMuPDF 为同步库，丢线程池跑。
    """
    return await asyncio.to_thread(_extract_figures_sync, paper_id, pdf_path)
