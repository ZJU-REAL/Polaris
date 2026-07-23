"""引用导出：BibTeX / CSL-JSON（docs/api-lit.md §6，不 import fastapi）。

citation key = {第一作者姓小写}{year}{标题首个实义词小写}（冲突加 a/b/c 后缀）。
entry 类型：venue 含 proceedings/conference → inproceedings；有 venue → article；否则 misc。
"""

import re
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import LibraryPaper
from app.models.paper import Paper
from app.services.libraries import (
    dedupe_member_rows,
    get_source_library_ids,
    member_papers_stmt,
)
from app.services.papers import apply_paper_filters

# 缺省导出的论文状态（未显式指定 status 时）
DEFAULT_EXPORT_STATUSES = ("compiled", "included")

# 标题实义词提取用的英文虚词表
_STOPWORDS = {
    "a", "an", "the", "on", "of", "for", "in", "with", "at", "to", "and", "or",
    "from", "via", "by", "as", "is", "are", "be", "do", "does", "toward", "towards",
    "into", "onto", "over", "under", "about",
}  # fmt: skip

_CJK_RE = re.compile(r"[぀-ヿ一-鿿가-힯]")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def split_author_name(name: str) -> tuple[str, str]:
    """姓名拆 (family, given)：兼容 "Last, First" 与 "First Last"；中日韩名整个作 family。"""
    name = name.strip()
    if _CJK_RE.search(name):
        return name, ""
    if "," in name:
        family, _, given = name.partition(",")
        return family.strip(), given.strip()
    parts = name.split()
    if len(parts) <= 1:
        return name, ""
    return parts[-1], " ".join(parts[:-1])


def _first_author_family(paper: Paper) -> str:
    authors = paper.authors or []
    first = authors[0] if authors else None
    name = (first.get("name") if isinstance(first, dict) else str(first or "")) or ""
    family, _ = split_author_name(name)
    if _CJK_RE.search(family):
        return family
    cleaned = re.sub(r"[^a-z0-9]", "", family.lower())
    return cleaned or "anon"


def _first_substantive_word(title: str) -> str:
    for word in _WORD_RE.findall(title):
        if word.lower() not in _STOPWORDS:
            return word.lower()
    return "paper"


def citation_key_base(paper: Paper) -> str:
    year = str(paper.year) if paper.year else ""
    return f"{_first_author_family(paper)}{year}{_first_substantive_word(paper.title or '')}"


def citation_key_for(*, title: str, author_names: Sequence[str], year: int | None) -> str:
    """按同一规则为非库内文献（如 S2 检索命中）生成 citation key base（M5-B 论文撰写）。"""
    first = author_names[0] if author_names else ""
    family, _ = split_author_name(first)
    if not _CJK_RE.search(family):
        family = re.sub(r"[^a-z0-9]", "", family.lower()) or "anon"
    return f"{family}{year or ''}{_first_substantive_word(title or '')}"


def assign_citation_keys(papers: Sequence[Paper]) -> dict[uuid.UUID, str]:
    """按顺序分配 citation key；同 base 冲突时从第二个起加 a/b/c… 后缀。"""
    keys: dict[uuid.UUID, str] = {}
    used: dict[str, int] = {}
    for paper in papers:
        base = citation_key_base(paper)
        count = used.get(base, 0)
        used[base] = count + 1
        if count == 0:
            keys[paper.id] = base
        else:
            # 第二个冲突加 a、第三个加 b……超过 26 个回退数字后缀
            suffix = chr(ord("a") + count - 1) if count <= 26 else str(count)
            keys[paper.id] = f"{base}{suffix}"
    return keys


def entry_type_of(paper: Paper) -> str:
    venue = (paper.venue or "").lower()
    if venue and ("proceedings" in venue or "conference" in venue):
        return "inproceedings"
    if venue:
        return "article"
    return "misc"


def _author_names(paper: Paper) -> list[str]:
    names: list[str] = []
    for item in paper.authors or []:
        name = item.get("name") if isinstance(item, dict) else str(item)
        if name and str(name).strip():
            names.append(str(name).strip())
    return names


def _bibtex_author_field(paper: Paper) -> str:
    parts = []
    for name in _author_names(paper):
        family, given = split_author_name(name)
        parts.append(f"{family}, {given}" if given else family)
    return " and ".join(parts)


def build_bibtex(papers: Sequence[Paper]) -> str:
    """生成 .bib 文本；arxiv 论文带 eprint / archivePrefix=arXiv。"""
    return build_bibtex_for(papers, assign_citation_keys(papers))


def build_bibtex_for(papers: Sequence[Paper], keys: dict[uuid.UUID, str]) -> str:
    """用调用方给定的 citation key 映射生成 .bib 文本（M5-B 编译用 fact-pack 固定 key）。"""
    blocks: list[str] = []
    for paper in papers:
        entry_type = entry_type_of(paper)
        fields: list[tuple[str, str]] = [("title", paper.title or "")]
        author = _bibtex_author_field(paper)
        if author:
            fields.append(("author", author))
        if paper.year:
            fields.append(("year", str(paper.year)))
        if paper.venue:
            venue_field = "booktitle" if entry_type == "inproceedings" else "journal"
            fields.append((venue_field, paper.venue))
        if paper.doi:
            fields.append(("doi", paper.doi))
        if paper.url:
            fields.append(("url", paper.url))
        if paper.arxiv_id:
            fields.append(("eprint", paper.arxiv_id))
            fields.append(("archivePrefix", "arXiv"))
        lines = [f"@{entry_type}{{{keys[paper.id]},"]
        lines += [f"  {name} = {{{value}}}," for name, value in fields]
        lines.append("}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


_CSL_TYPES = {"inproceedings": "paper-conference", "article": "article-journal", "misc": "article"}


def build_csl_json(papers: Sequence[Paper]) -> list[dict[str, Any]]:
    """生成 CSL-JSON 数组（Zotero 可直接导入）。"""
    keys = assign_citation_keys(papers)
    items: list[dict[str, Any]] = []
    for paper in papers:
        item: dict[str, Any] = {
            "id": keys[paper.id],
            "type": _CSL_TYPES[entry_type_of(paper)],
            "title": paper.title or "",
        }
        authors = []
        for name in _author_names(paper):
            family, given = split_author_name(name)
            author: dict[str, str] = {"family": family}
            if given:
                author["given"] = given
            authors.append(author)
        if authors:
            item["author"] = authors
        if paper.year:
            item["issued"] = {"date-parts": [[paper.year]]}
        if paper.doi:
            item["DOI"] = paper.doi
        if paper.url:
            item["URL"] = paper.url
        if paper.venue:
            item["container-title"] = paper.venue
        items.append(item)
    return items


async def papers_for_export(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    status: str | None = None,
    tag: str | None = None,
    starred: bool | None = None,
    paper_ids: list[uuid.UUID] | None = None,
) -> Sequence[Paper]:
    """导出对象：过滤参数与论文列表一致；缺省 status in (compiled, included)。

    paper_ids 指定时按 id 精确导出（多选导出），忽略状态缺省过滤。
    """
    library_ids = await get_source_library_ids(session, project_id)
    if not library_ids:
        return []  # 课题无关联库 = 无可导出语料
    stmt = apply_paper_filters(
        member_papers_stmt(library_ids),
        project_id=project_id,
        status=status,
        tag=tag,
        starred=starred,
        user_id=user_id,
    )
    if paper_ids:
        stmt = stmt.where(Paper.id.in_(paper_ids))
    elif not status:
        stmt = stmt.where(LibraryPaper.status.in_(DEFAULT_EXPORT_STATUSES))
    stmt = stmt.order_by(Paper.year.asc().nulls_last(), LibraryPaper.created_at.asc())
    # 跨库同一论文按确定性视角归并（有 wiki 优先），保持年份升序
    deduped = dedupe_member_rows((await session.execute(stmt)).all())
    deduped.sort(key=lambda pm: (pm[0].year is None, pm[0].year or 0, pm[1].created_at))
    return [paper for paper, _ in deduped]
