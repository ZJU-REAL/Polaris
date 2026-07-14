"""PDF 落盘、全文抽取与嵌入图提取（PyMuPDF）。

文件存 settings.data_dir（默认 ./data，容器内挂 /srv/data）：
    <data_dir>/papers/<paper_id>.pdf / <paper_id>.txt
    <data_dir>/papers/<paper_id>/figures/fig_<index>.png
"""

import asyncio
import io
import logging
from pathlib import Path
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 嵌入图候选过滤（docs/api-lit.md §6.5）：尺寸下限 + 面积降序取前 N
FIGURE_MIN_WIDTH = 200
FIGURE_MIN_HEIGHT = 150
FIGURE_MAX_COUNT = 8


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
    txt_path.write_text(text, encoding="utf-8")
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


def _extract_figures_sync(paper_id: str, pdf_path: Path) -> list[dict[str, Any]]:
    import pymupdf  # 延迟导入：仅在真正抽取时需要

    # (页码, xref, Pixmap)：按 xref 去重（同一嵌入图跨页复用只取首次出现）
    candidates: list[tuple[int, int, Any]] = []
    seen_xrefs: set[int] = set()
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
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
                candidates.append((page.number + 1, xref, pix))

        # 面积降序取前 N，再按 (页码, xref) 恢复文中出现顺序编号
        candidates.sort(key=lambda c: (-(c[2].width * c[2].height), c[0], c[1]))
        selected = sorted(candidates[:FIGURE_MAX_COUNT], key=lambda c: (c[0], c[1]))

        out_dir = figures_dir(paper_id)
        for old in out_dir.glob("fig_*.png"):
            old.unlink()
        figures: list[dict[str, Any]] = []
        for index, (page_no, _xref, pix) in enumerate(selected):
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
