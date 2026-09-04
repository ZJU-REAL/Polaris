"""Zotero 库导入（#638）：解析 Zotero/BetterBibTeX 导出的 .bib（可选附件 zip），批量入库。

与批量手动添加（paper_enrich._run_batch_import）同一套事件口径（batch_item /
batch_progress / batch_enriched / done），前端直接复用 PaperBatchProgressModal；
建行与补全也复用同一入口（create_pool_paper_stub / launch_paper_enrichment），
不另起一条创建链路。

去重按「目标库现有论文」做三级匹配：DOI → arXiv id → 规范化标题（小写去标点，
口径 = services/dedup.normalize_title）。命中记 duplicates（事件 status="existing"，
reason 标注命中层级），不重复建行；三级都未命中才查全局内容池（池命中且不在本库
→ 只补成员行）。库内索引随导入即时更新，同一个 .bib 里自带的重复条目也会被挡住。
"""

import asyncio
import logging
import re
import uuid
import zipfile
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select

from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper
from app.services.dedup import normalize_title
from app.services.literature.arxiv import normalize_arxiv_id

# _clean 是 bibtex 字段清洗的既有口径（去大括号、折叠空白）；解析结果要和手动
# bibtex 添加落的字段长得一样，所以直接复用而不是再抄一份。
from app.services.paper_import import ParseFailedError, _clean

logger = logging.getLogger(__name__)

#: 单条附件解压上限，对齐 PDF 上传上限（api/papers.MAX_PDF_UPLOAD_BYTES）——
#: zip 里再大的成员也不该比直接上传能塞进更多字节（顺带挡 zip bomb）。
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024

_ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([a-zA-Z0-9.\-/]+?)(?:\.pdf)?(?:$|[\s}])")
_ARXIV_NOTE_RE = re.compile(r"arxiv[:\s]+(\d{4}\.\d{4,5}(?:v\d+)?)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\d{4}")


def _entry_arxiv_id(entry: dict[str, Any]) -> str | None:
    """eprint 优先；Zotero 常把 arXiv 线索丢在 url / note 里，作兜底扫一遍。"""
    eprint = _clean(entry.get("eprint"))
    if eprint:
        prefix = (entry.get("archiveprefix") or entry.get("eprinttype") or "arxiv").lower()
        if "arxiv" in prefix:
            return normalize_arxiv_id(eprint)
    for field in ("url", "note", "howpublished"):
        value = entry.get(field) or ""
        found = _ARXIV_URL_RE.search(value) or _ARXIV_NOTE_RE.search(value)
        if found:
            return normalize_arxiv_id(found.group(1))
    return None


def _pdf_hints(file_field: str | None) -> list[str]:
    """从 Zotero 的 file 字段提取 PDF 相对路径候选。

    Zotero 的格式是 ``描述:路径:MIME``、多个附件以 ``;`` 相连，路径里的 ``:`` ``;``
    ``\\`` 会转义；BetterBibTeX 也可能只写一段裸路径。这里不逐格解析三元组，
    而是把未转义分隔符切开后**凡以 .pdf 结尾的片段都算候选**——描述和 MIME 不会
    以 .pdf 结尾，这样对两种方言都稳。
    """
    if not file_field:
        return []
    hints: list[str] = []
    for chunk in re.split(r"(?<!\\)[;:]", file_field):
        part = chunk.replace("\\:", ":").replace("\\;", ";").replace("\\\\", "\\")
        part = part.replace("\\", "/").strip().strip("{}")
        if part.lower().endswith(".pdf"):
            hints.append(part)
    return hints


def parse_zotero_bib(text: str) -> list[dict[str, Any]]:
    """解析整个 .bib 文件 → 逐条字段 dict（title 缺失不在此处拦，交由导入记 invalid）。

    字段映射与 paper_import.parse_bibtex_entry 同口径，另补 Zotero/biblatex 方言：
    journaltitle（biblatex 期刊名）、date（biblatex 年份）、file（附件路径）、
    url/note 里的 arXiv 线索。LaTeX 转义（重音等）由 convert_to_unicode 还原。
    """
    import bibtexparser
    from bibtexparser.bparser import BibTexParser
    from bibtexparser.customization import convert_to_unicode

    try:
        parser = BibTexParser(common_strings=True)
        parser.ignore_nonstandard_types = False  # Zotero 会导出 @online/@software 等
        parser.customization = convert_to_unicode
        db = bibtexparser.loads(text, parser=parser)
    except Exception as e:  # noqa: BLE001 — 解析库的各种异常统一归为解析失败
        raise ParseFailedError(f"bib 文件解析出错（{type(e).__name__}）") from e
    if not db.entries:
        raise ParseFailedError("bib 文件里没有可识别的条目")

    results: list[dict[str, Any]] = []
    for entry in db.entries:
        year: int | None = None
        year_match = _YEAR_RE.search(entry.get("year") or entry.get("date") or "")
        if year_match:
            year = int(year_match.group())
        doi = _clean(entry.get("doi"))
        if doi:
            doi = doi.removeprefix("https://doi.org/").removeprefix("http://doi.org/")
        results.append(
            {
                "citekey": entry.get("ID") or "",
                "title": _clean(entry.get("title")),
                "authors": [
                    {"name": name}
                    for raw in (entry.get("author") or entry.get("editor") or "").split(" and ")
                    if (name := _clean(raw))
                ],
                "year": year,
                "venue": _clean(
                    entry.get("journal") or entry.get("journaltitle") or entry.get("booktitle")
                ),
                "doi": doi,
                "arxiv_id": _entry_arxiv_id(entry),
                "abstract": _clean(entry.get("abstract")),
                "url": _clean(entry.get("url")),
                "pdf_hints": _pdf_hints(entry.get("file")),
            }
        )
    return results


class ZipAttachmentIndex:
    """附件 zip 的查找索引：file 字段相对路径 → zip 成员（Zotero 的 files/ 结构）。

    匹配从严到宽：完整路径后缀 → 唯一同名文件 → 唯一同名（citekey / 规范化标题）。
    宽松层要求唯一命中，宁可漏挂也不错挂。
    """

    def __init__(self, zf: zipfile.ZipFile) -> None:
        self._zf = zf
        self._paths: dict[str, str] = {}  # 规范化小写全路径 → 成员名
        self._basenames: dict[str, list[str]] = {}  # 小写文件名 → 成员名列表
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".pdf"):
                continue
            normalized = info.filename.replace("\\", "/").lower()
            self._paths[normalized] = info.filename
            self._basenames.setdefault(normalized.rsplit("/", 1)[-1], []).append(info.filename)

    def _member_for(self, entry: dict[str, Any]) -> str | None:
        for hint in entry.get("pdf_hints") or []:
            target = hint.lower().lstrip("/")
            for path, member in self._paths.items():
                if path == target or path.endswith("/" + target):
                    return member
            candidates = self._basenames.get(target.rsplit("/", 1)[-1])
            if candidates and len(candidates) == 1:
                return candidates[0]
        # 无 file 字段（或没匹配上）：按 citekey / 标题同名兜底
        citekey = (entry.get("citekey") or "").lower()
        if citekey:
            candidates = self._basenames.get(f"{citekey}.pdf")
            if candidates and len(candidates) == 1:
                return candidates[0]
        title = entry.get("title")
        if title:
            wanted = normalize_title(title)
            matched = [
                members[0]
                for base, members in self._basenames.items()
                if len(members) == 1 and normalize_title(base.removesuffix(".pdf")) == wanted
            ]
            if len(matched) == 1:
                return matched[0]
        return None

    def find(self, entry: dict[str, Any]) -> bytes | None:
        member = self._member_for(entry)
        if member is None:
            return None
        if self._zf.getinfo(member).file_size > MAX_ATTACHMENT_BYTES:
            logger.warning("zotero attachment too large, skipped: %s", member)
            return None
        return self._zf.read(member)


class LibraryDedupIndex:
    """目标库现有论文的三级去重索引：DOI → arXiv id → 规范化标题。"""

    def __init__(self) -> None:
        self._by_doi: dict[str, uuid.UUID] = {}
        self._by_arxiv: dict[str, uuid.UUID] = {}
        self._by_title: dict[str, uuid.UUID] = {}

    def add(
        self,
        paper_id: uuid.UUID,
        *,
        doi: str | None,
        arxiv_id: str | None,
        title: str | None,
    ) -> None:
        if doi:
            self._by_doi.setdefault(doi.lower(), paper_id)
        if arxiv_id:
            self._by_arxiv.setdefault(arxiv_id.lower(), paper_id)
        if title:
            self._by_title.setdefault(normalize_title(title), paper_id)

    def match(self, fields: dict[str, Any]) -> tuple[uuid.UUID, str] | None:
        """命中返回 (已有 paper_id, 命中层级 doi|arxiv|title)。"""
        doi = fields.get("doi")
        if doi and (hit := self._by_doi.get(doi.lower())) is not None:
            return hit, "doi"
        arxiv_id = fields.get("arxiv_id")
        if arxiv_id and (hit := self._by_arxiv.get(arxiv_id.lower())) is not None:
            return hit, "arxiv"
        title = fields.get("title")
        if title and (hit := self._by_title.get(normalize_title(title))) is not None:
            return hit, "title"
        return None


async def build_library_dedup_index(session: Any, library_id: uuid.UUID) -> LibraryDedupIndex:
    """扫目标库全部成员（含回收站——在库即算「已有」）建索引。"""
    index = LibraryDedupIndex()
    rows = await session.execute(
        select(Paper.id, Paper.doi, Paper.arxiv_id, Paper.title)
        .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
        .where(LibraryPaper.library_id == library_id)
    )
    for paper_id, doi, arxiv_id, title in rows:
        index.add(paper_id, doi=doi, arxiv_id=arxiv_id, title=title)
    return index


def _staged_root() -> Path:
    from app.core.config import get_settings

    return Path(get_settings().data_dir) / "zotero_imports"


def _cleanup_staged(bib_path: Path) -> None:
    """删掉这次导入落盘的暂存目录；只清 zotero_imports 之下的，测试临时目录不动。"""
    import shutil

    staged_dir = bib_path.parent
    try:
        if staged_dir.parent.resolve() == _staged_root().resolve():
            shutil.rmtree(staged_dir, ignore_errors=True)
    except OSError:
        logger.warning("failed to clean staged zotero import dir: %s", staged_dir)


async def run_zotero_import(
    redis: Redis,
    *,
    task_id: str,
    bib_path: str,
    zip_path: str | None,
    library_id: uuid.UUID,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None,
) -> dict[str, int]:
    """执行一次 Zotero 导入：逐条独立提交，一条失败不影响其它条目。

    进度与结果走 paper-task 事件通道（与批量手动添加同口径），API 侧已注册任务
    归属，前端订阅 /paper-tasks/{task_id}/events 即可。返回汇总计数（arq 结果可查）：
    created=导入、existing=重复（reason 标注 doi/arxiv/title/pool）、invalid=缺 title、
    failed=异常。
    """
    from app.core.db import get_sessionmaker
    from app.core.events import EventBus, publish_paper_task_event
    from app.services import paper_enrich
    from app.services import papers as papers_service
    from app.services.dedup import pool_dedup_key
    from app.services.libraries import ensure_membership, find_pool_paper, get_membership
    from app.services.paper_import import create_pool_paper_stub

    bus = EventBus(redis)
    totals = {"created": 0, "existing": 0, "invalid": 0, "failed": 0}
    enrichment_tasks: list[tuple[int, str]] = []
    zf: zipfile.ZipFile | None = None

    try:
        try:
            entries = parse_zotero_bib(Path(bib_path).read_text(encoding="utf-8-sig"))
        except (OSError, ParseFailedError) as e:
            await publish_paper_task_event(bus, task_id, "error", {"message": str(e)})
            return totals

        attachments: ZipAttachmentIndex | None = None
        if zip_path:
            try:
                zf = zipfile.ZipFile(zip_path)
                attachments = ZipAttachmentIndex(zf)
            except (OSError, zipfile.BadZipFile):
                # 附件包坏了不拦导入本身：条目照进，只是都不带 PDF
                logger.warning("zotero attachment zip unusable: %s", zip_path, exc_info=True)

        async with get_sessionmaker()() as session:
            if await session.get(DirectionLibrary, library_id) is None:
                await publish_paper_task_event(
                    bus, task_id, "error", {"message": "library not found"}
                )
                return totals
            index = await build_library_dedup_index(session, library_id)

        for i, fields in enumerate(entries):
            event: dict[str, Any] = {
                "index": i,
                "source": "zotero",
                "input": fields.get("citekey") or (fields.get("title") or "")[:120],
                "status": "failed",
                "processing": False,
            }
            try:
                if not fields.get("title"):
                    event.update(status="invalid", error="条目缺少 title")
                elif (hit := index.match(fields)) is not None:
                    matched_id, reason = hit
                    async with get_sessionmaker()() as session:
                        paper = await session.get(Paper, matched_id)
                    event.update(
                        status="existing",
                        reason=reason,
                        paper_id=str(matched_id),
                        title=paper.title if paper is not None else fields["title"],
                    )
                else:
                    async with get_sessionmaker()() as session:
                        pooled = await find_pool_paper(
                            session,
                            arxiv_id=fields.get("arxiv_id"),
                            doi=fields.get("doi"),
                            dedup_key=pool_dedup_key(
                                arxiv_id=fields.get("arxiv_id"),
                                doi=fields.get("doi"),
                                title=fields["title"],
                                year=fields.get("year"),
                                authors=fields.get("authors"),
                            ),
                        )
                        if pooled is not None and (
                            await get_membership(
                                session, library_id=library_id, paper_id=pooled.id
                            )
                        ) is not None:
                            # 三级索引漏网的库内命中（如库行 doi 为空、池键靠年份+首作者对上）
                            index.add(
                                pooled.id,
                                doi=fields.get("doi"),
                                arxiv_id=fields.get("arxiv_id"),
                                title=fields.get("title"),
                            )
                            event.update(
                                status="existing",
                                reason="pool",
                                paper_id=str(pooled.id),
                                title=pooled.title,
                            )
                        else:
                            paper = pooled or await create_pool_paper_stub(
                                session, fields=fields
                            )
                            await ensure_membership(
                                session,
                                library_id=library_id,
                                paper_id=paper.id,
                                status="included",
                            )
                            await session.commit()
                            await session.refresh(paper)
                            paper_id, title = paper.id, paper.title
                            # 挂附件走 PDF 上传的同一入口（校验 + 全文 + 分块 + 向量），
                            # 失败不推翻导入：论文已在库里，只是没带上 PDF
                            content = attachments.find(fields) if attachments else None
                            if content is not None and not paper.pdf_path:
                                try:
                                    await papers_service.upload_pdf(
                                        session,
                                        paper,
                                        content,
                                        user_id=user_id,
                                        project_id=project_id,
                                    )
                                    await session.commit()
                                    event["attachment"] = True
                                except asyncio.CancelledError:
                                    raise
                                except Exception as e:  # noqa: BLE001
                                    await session.rollback()
                                    logger.warning(
                                        "zotero attachment failed for %s",
                                        paper_id,
                                        exc_info=True,
                                    )
                                    event["attachment_error"] = f"{type(e).__name__}: {e}"
                            index.add(
                                paper_id,
                                doi=fields.get("doi"),
                                arxiv_id=fields.get("arxiv_id"),
                                title=fields.get("title"),
                            )
                            paper = await session.get(Paper, paper_id)
                            done_already = await paper_enrich.paper_processing_complete(
                                session, paper, library_id=library_id
                            )
                            child_task_id: str | None = None
                            if not done_already:
                                child_task_id = await paper_enrich.launch_paper_enrichment(
                                    redis=redis,
                                    paper_id=paper_id,
                                    user_id=user_id,
                                    library_id=library_id,
                                    project_id=project_id,
                                )
                            if child_task_id:
                                enrichment_tasks.append((i, child_task_id))
                            event.update(
                                status="created",
                                paper_id=str(paper_id),
                                title=title,
                                processing=bool(child_task_id),
                            )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 单条隔离，继续处理后续条目
                logger.exception("zotero import entry %d failed", i)
                event.update(status="failed", error=f"{type(e).__name__}: {e}")

            totals[str(event["status"])] += 1
            await publish_paper_task_event(bus, task_id, "batch_item", event)
            await publish_paper_task_event(
                bus,
                task_id,
                "batch_progress",
                {"completed": i + 1, "total": len(entries), **totals},
            )

        async def _wait(item_index: int, child_id: str) -> int:
            await paper_enrich.await_task(child_id)
            return item_index

        waits = [_wait(item_index, child_id) for item_index, child_id in enrichment_tasks]
        for completed in asyncio.as_completed(waits):
            finished = await completed
            await publish_paper_task_event(bus, task_id, "batch_enriched", {"index": finished})

        await publish_paper_task_event(
            bus, task_id, "done", {"total": len(entries), **totals}
        )
        return totals
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("zotero import task failed: %s", task_id)
        try:
            await publish_paper_task_event(
                bus, task_id, "error", {"message": f"{type(e).__name__}: {e}"}
            )
        except Exception:  # noqa: BLE001
            logger.warning("failed to publish zotero import error event", exc_info=True)
        return totals
    finally:
        if zf is not None:
            zf.close()
        _cleanup_staged(Path(bib_path))
