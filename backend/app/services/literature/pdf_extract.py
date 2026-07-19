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
# 近空白过滤：铺白后白底像素占比超过此值视为空白（空矢量簇/白色卡片框/空白页），丢弃。
# 真实图（含稀疏曲线/小提琴图）实测白底 ≤~0.90；空白矢量簇实测 0.95~0.99。取 0.94 留余量。
FIGURE_MAX_WHITE_FRAC = 0.94

# 矢量图渲染兜底：学术论文的架构图/流程图/曲线图多为矢量绘图，get_images 抓不到。
# 用 cluster_drawings 找矢量绘图簇，按区域渲染成 PNG 参与候选。
VECTOR_MIN_W_PT = 140.0  # 簇最小宽（pt）
VECTOR_MIN_H_PT = 90.0  # 簇最小高（pt）
VECTOR_MAX_PAGE_FRAC = 0.85  # 簇面积超过页面 85% 视为背景/整页边框，跳过
VECTOR_RENDER_DPI = 300  # 矢量簇渲染 DPI（原 150 太糊；300 让烤出的 PNG 像素翻倍）
VECTOR_CLIP_MARGIN_PT = 6.0  # 渲染时四周留白，把坐标轴刻度/图例框进来

# 裁剪框扩展：cluster_drawings 只圈矢量线条，坐标轴刻度/图例/图题等文本对象在簇外，
# 会被裁掉。渲染前把「压在簇上 or 紧邻簇的短文本块」并进裁剪框（正文整栏段落除外）。
EXPAND_GAP_PT = 12.0  # 文本块与簇的最大间隙，超出视为不相关
EXPAND_MAX_WIDTH_RATIO = 1.4  # 邻近文本块宽度上限（相对当前框），滤掉整栏正文
EXPAND_CAPTION_GAP_PT = 24.0  # 图题（Figure N…）允许的更大间隙
EXPAND_MAX_PASSES = 3  # 定点扩展最多轮数（吸收后可能带来新的重叠/邻近）
EXPAND_MAX_PAGE_FRAC = 0.75  # 扩展后框面积上限（占页面比），越界则不吸收该块，防跑飞
# 图题起始模式：Figure 1 / Fig. 2 / 图 3 / Table 1 / 表 2
CAPTION_RE = re.compile(r"^\s*(fig(?:ure)?\.?|图|表|tab(?:le)?\.?)\s*\.?\s*\d", re.IGNORECASE)


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


def _flatten_to_white_rgb(png_bytes: bytes) -> Any:
    """Pillow 归一化为 RGB：透明区域铺白底（RGBA/LA/P+transparency），CMYK 转 RGB，杜绝黑底图。

    返回一个独立的 PIL Image（不再绑定已关闭的文件），供落盘与空白检测共用。
    """
    from PIL import Image  # 延迟导入：仅在真正抽取时需要

    with Image.open(io.BytesIO(png_bytes)) as img:
        img.load()
        if img.mode == "P":
            img = img.convert("RGBA" if "transparency" in img.info else "RGB")
        if img.mode in ("RGBA", "LA"):
            rgba = img.convert("RGBA")
            white = Image.new("RGB", rgba.size, (255, 255, 255))
            white.paste(rgba, mask=rgba.getchannel("A"))
            return white
        return img.convert("RGB")


def _png_bytes(img: Any) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _white_fraction(img: Any) -> float:
    """铺白 RGB 图里近白像素的占比（降采样到 ≤200px 提速）；用于近空白过滤。"""
    gray = img.convert("L")
    w, h = gray.size
    if w > 200 or h > 200:
        gray = gray.resize((min(w, 200) or 1, min(h, 200) or 1))
    pixels = gray.tobytes()  # 模式 L：每像素 1 字节
    if not pixels:
        return 1.0
    return sum(1 for v in pixels if v >= 245) / len(pixels)


def _pix_white_fraction(pix: Any) -> float:
    """Pixmap 铺白后的近白占比（判断 SMask 合并是否把图压成空白 / 候选是否空白）。"""
    return _white_fraction(_flatten_to_white_rgb(pix.tobytes("png")))


def _flatten_png_white(png_bytes: bytes) -> bytes:
    """透明/CMYK 归一化为白底 RGB PNG，杜绝黑底图。"""
    return _png_bytes(_flatten_to_white_rgb(png_bytes))


def _rect_gap(a: Any, b: Any) -> float:
    """两个轴对齐矩形的最小间隙（重叠为 0）。"""
    dx = max(0.0, a.x0 - b.x1, b.x0 - a.x1)
    dy = max(0.0, a.y0 - b.y1, b.y0 - a.y1)
    return max(dx, dy)


def _should_absorb(box: Any, bb: Any, text: str) -> bool:
    """文本块是否属于该图（应并进裁剪框）：图题、或压在图上/紧邻的短文本。"""
    if bb.is_empty or bb.width <= 0 or bb.height <= 0:
        return False
    # 图题（Figure N…）：匹配模式 + 间隙够近即收（图题可能较宽，不受宽度闸门约束）
    if _rect_gap(box, bb) <= EXPAND_CAPTION_GAP_PT and CAPTION_RE.match(text):
        return True
    # 其余文本必须"够窄"才像标签（轴标/刻度/图例/节点标签），否则视为整栏正文段落跳过。
    # 宽度闸门对「重叠」也生效——框向下长到轴标后会与下方正文竖直重叠，不加约束会把正文吞掉。
    if bb.width > box.width * EXPAND_MAX_WIDTH_RATIO:
        return False
    if box.intersects(bb):  # 压在图上的文字
        return True
    return _rect_gap(box, bb) <= EXPAND_GAP_PT  # 紧邻的短文本


def _expand_clip_with_text_blocks(
    rect: Any,
    blocks: list[tuple[float, float, float, float, str]],
    page_rect: Any,
    page_area: float,
) -> Any:
    """把与矢量簇重叠 / 紧邻的文本块并进裁剪框（轴标/图例/图题），整栏正文与越界吸收除外。

    定点扩展：吸收一块可能让框变大而带来新的重叠/邻近，故多轮直到不再增长；
    每次吸收前校验不越界（不超过页面 EXPAND_MAX_PAGE_FRAC），防扩到整栏正文。纯几何，便于单测。
    """
    import pymupdf

    box = pymupdf.Rect(rect)
    cap = page_area * EXPAND_MAX_PAGE_FRAC
    remaining = [(pymupdf.Rect(b[0], b[1], b[2], b[3]), b[4]) for b in blocks]
    for _ in range(EXPAND_MAX_PASSES):
        grew = False
        keep: list[tuple[Any, str]] = []
        for bb, text in remaining:
            if _should_absorb(box, bb, text):
                cand = box | bb
                if cand.width * cand.height <= cap:  # 不越界才吸收
                    box = cand
                    grew = True
                    continue
            keep.append((bb, text))
        remaining = keep
        if not grew:
            break
    return box & pymupdf.Rect(page_rect)


def _page_text_blocks(page: Any) -> list[tuple[float, float, float, float, str]]:
    """页面上的文本块 (x0,y0,x1,y1,text)；图片块（block_type=1）与空块剔除。"""
    out: list[tuple[float, float, float, float, str]] = []
    for b in page.get_text("blocks"):
        if len(b) >= 7 and b[6] == 0 and isinstance(b[4], str) and b[4].strip():
            out.append((b[0], b[1], b[2], b[3], b[4]))
    return out


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
    text_blocks = _page_text_blocks(page)
    for rect in clusters:
        if rect.width < VECTOR_MIN_W_PT or rect.height < VECTOR_MIN_H_PT:
            continue
        if rect.width * rect.height / page_area > VECTOR_MAX_PAGE_FRAC:
            continue  # 整页背景/页面边框
        # 先把簇外的轴标/图例/图题并进来，再按当前框加渲染留白并裁回页面
        expanded = _expand_clip_with_text_blocks(rect, text_blocks, page_rect, page_area)
        clip = pymupdf.Rect(
            max(page_rect.x0, expanded.x0 - VECTOR_CLIP_MARGIN_PT),
            max(page_rect.y0, expanded.y0 - VECTOR_CLIP_MARGIN_PT),
            min(page_rect.x1, expanded.x1 + VECTOR_CLIP_MARGIN_PT),
            min(page_rect.y1, expanded.y1 + VECTOR_CLIP_MARGIN_PT),
        )
        try:
            pixmaps.append(page.get_pixmap(clip=clip, dpi=VECTOR_RENDER_DPI))
        except Exception:  # noqa: BLE001
            logger.warning("vector clip render failed on page %d", page.number + 1, exc_info=True)
    return pixmaps


def _candidate_from_pix(page_no: int, order: int, pix: Any) -> tuple | None:
    """把 Pixmap 铺白编码为 PNG；近空白（白底占比过高）返回 None 丢弃。

    返回 (页码, 页内序号, 宽, 高, PNG bytes)——预编码好落盘用的字节，避免二次编码。
    """
    flat = _flatten_to_white_rgb(pix.tobytes("png"))
    if _white_fraction(flat) > FIGURE_MAX_WHITE_FRAC:
        return None  # 空矢量簇 / 白色卡片框 / 空白页
    return (page_no, order, pix.width, pix.height, _png_bytes(flat))


def _extract_figures_sync(paper_id: str, pdf_path: Path) -> list[dict[str, Any]]:
    import pymupdf  # 延迟导入：仅在真正抽取时需要

    # (页码, 页内序号, 宽, 高, PNG bytes)：嵌入图按 xref 去重 + 矢量簇渲染；近空白已过滤
    candidates: list[tuple[int, int, int, int, bytes]] = []
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
                        merged = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, smask))
                    except Exception:  # noqa: BLE001 — 蒙版损坏等，保底用无 alpha 原图
                        logger.warning(
                            "smask merge failed for paper %s xref %d", paper_id, xref, exc_info=True
                        )
                        merged = None
                    # 个别论文 SMask 语义异常（镂空/反转掩膜），合并会把有内容的图压成近空白。
                    # 合并后近空白但原图不空 → 判为异常，保留未合并原图（宁可白底也不丢真图）。
                    if merged is not None:
                        if (
                            _pix_white_fraction(merged) > FIGURE_MAX_WHITE_FRAC
                            and _pix_white_fraction(pix) <= FIGURE_MAX_WHITE_FRAC
                        ):
                            logger.warning(
                                "smask merge blanked image, keeping raw for paper %s xref %d",
                                paper_id,
                                xref,
                            )
                        else:
                            pix = merged
                if pix.n - pix.alpha >= 4:  # CMYK 等 → RGB（保留 alpha）
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                cand = _candidate_from_pix(page.number + 1, order, pix)
                if cand is None:
                    continue  # 近空白，丢弃
                candidates.append(cand)
                order += 1
            # 矢量图兜底（尺寸下限与嵌入图一致，按渲染后像素判）
            for pix in _vector_figure_pixmaps(page):
                if pix.width < FIGURE_MIN_WIDTH or pix.height < FIGURE_MIN_HEIGHT:
                    continue
                cand = _candidate_from_pix(page.number + 1, order, pix)
                if cand is None:
                    continue  # 空矢量簇（如整块白色卡片框），丢弃
                candidates.append(cand)
                order += 1

        # 按页轮转选优：每页先取面积最大的一张，轮完所有页再取第二张……
        # 保证动机图（前几页）/实验图（中后页）都有名额，不被单页大图或附录霸占；
        # 最终按 (页码, 页内序号) 恢复文中出现顺序编号
        by_page: dict[int, list[tuple[int, int, int, int, bytes]]] = {}
        for cand in candidates:
            by_page.setdefault(cand[0], []).append(cand)
        for lst in by_page.values():
            lst.sort(key=lambda c: (-(c[2] * c[3]), c[1]))
        picked: list[tuple[int, int, int, int, bytes]] = []
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
        for index, (page_no, _order, width, height, png) in enumerate(selected):
            (out_dir / f"fig_{index}.png").write_bytes(png)
            figures.append({"index": index, "page": page_no, "width": width, "height": height})
    return figures


async def extract_figures(paper_id: str, pdf_path: Path) -> list[dict[str, Any]]:
    """提取 PDF 嵌入图为 PNG 落盘，返回 [{index, page, width, height}]。

    过滤：宽 ≥200 且高 ≥150、按 xref 去重、面积降序取前 8（编号按文中页码顺序）；
    PyMuPDF 为同步库，丢线程池跑。
    """
    return await asyncio.to_thread(_extract_figures_sync, paper_id, pdf_path)
