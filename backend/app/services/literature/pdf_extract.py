"""PDF 落盘与全文抽取（PyMuPDF）。

文件存 settings.data_dir（默认 ./data，容器内挂 /srv/data）：
    <data_dir>/papers/<paper_id>.pdf / <paper_id>.txt
"""

import asyncio
from pathlib import Path

from app.core.config import get_settings


def papers_dir() -> Path:
    d = Path(get_settings().data_dir) / "papers"
    d.mkdir(parents=True, exist_ok=True)
    return d


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
