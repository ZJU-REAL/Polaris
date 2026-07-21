"""矢量图裁剪框扩展（文本块并入）几何逻辑单测——纯 Rect 运算，不需真 PDF。"""

import pymupdf

from app.services.literature.pdf_extract import (
    CAPTION_RE,
    _expand_clip_with_text_blocks,
    _rect_gap,
    _should_absorb,
)

PAGE = pymupdf.Rect(0, 0, 612, 792)  # US Letter
PAGE_AREA = 612 * 792


def test_rect_gap():
    a = pymupdf.Rect(100, 100, 200, 200)
    assert _rect_gap(a, pymupdf.Rect(150, 150, 180, 180)) == 0.0  # 重叠
    assert _rect_gap(a, pymupdf.Rect(210, 100, 260, 200)) == 10.0  # 右侧 10pt 间隙
    assert _rect_gap(a, pymupdf.Rect(100, 210, 200, 260)) == 10.0  # 下方 10pt 间隙


def test_caption_regex():
    for s in ["Figure 1: overview", "Fig. 2 shows", "图3 架构", "表 1", "Table 2:"]:
        assert CAPTION_RE.match(s), s
    for s in ["Figurative language", "we find that", "Figures are"]:
        assert not CAPTION_RE.match(s), s


def test_expand_absorbs_labels_legend_caption_but_not_body():
    cluster = pymupdf.Rect(100, 100, 300, 250)  # 图主体
    blocks = [
        (280, 120, 320, 160, "acc"),  # 压在右缘的标签 → 吸收，x1→320
        (110, 255, 250, 270, "0 1 2 3"),  # 紧邻下方短轴标（窄）→ 吸收，y1→270
        (60, 110, 98, 180, "legend"),  # 左侧紧邻窄图例 → 吸收，x0→60
        (40, 255, 572, 360, "long body text " * 20),  # 整栏正文（宽）→ 排除
        (100, 290, 320, 308, "Figure 3: the overview"),  # 图题（间隙 20>12 但匹配图题）→ 吸收
        (400, 600, 560, 650, "see appendix"),  # 远处无关 → 排除
    ]
    box = _expand_clip_with_text_blocks(cluster, blocks, PAGE, PAGE_AREA)
    assert box.x0 == 60 and box.y0 == 100
    assert box.x1 == 320
    assert box.y1 == 308  # 吸到图题底
    # 整栏正文没被吞（否则 x1 会到 572）
    assert box.x1 < 500
    # 仍在页面内
    assert box in PAGE or (box.x0 >= 0 and box.y1 <= 792)


def test_expand_skips_runaway_absorption():
    """若吸收某块会让框超过页面 EXPAND_MAX_PAGE_FRAC，则不吸收该块（防扩到整栏正文）。"""
    cluster = pymupdf.Rect(100, 100, 300, 250)
    blocks = [(0, 0, 612, 792, "full page overlay")]  # 与簇重叠但并入后=整页 → 越界跳过
    box = _expand_clip_with_text_blocks(cluster, blocks, PAGE, PAGE_AREA)
    assert box == cluster  # 保持原簇，未被撑到整页


def test_should_absorb_width_ratio_gate():
    box = pymupdf.Rect(100, 100, 300, 250)  # 宽 200
    near_narrow = pymupdf.Rect(110, 255, 250, 268)  # 宽 140 ≤ 200*1.4，紧邻 → 吸
    near_wide = pymupdf.Rect(60, 255, 560, 268)  # 宽 500 > 280，整栏正文 → 不吸
    assert _should_absorb(box, near_narrow, "0 1 2")
    assert not _should_absorb(box, near_wide, "body paragraph")
