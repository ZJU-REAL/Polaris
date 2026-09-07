"""一键全量导出（#690，设计报告 §17/§18 信任设计②「随时可走」）。

把一个用户在平台上的全部数据面组装成一棵可读的目录树再打成 zip：

    libraries/<库名>/library.json + papers.bib + papers.csl.json + pdfs/
    notes/<论文名>.md            （笔记 + 划线，YAML frontmatter 带论文标识）
    wiki/<库名>/                 （复用 Obsidian vault 导出的目录结构）
    discovery/<run 名>/tree.json + proposal.md + disclosure.json
    experiments/<实验名>/run.json
    manuscripts/<题名>/source/ + compiled.pdf
    manifest.json + README.md

口径上的两条硬原则：
- 空面跳过不报错——没有数据的面不产生目录，导出永远能出包；
- 单项失败记 manifest.warnings 不拖垮整包——导出是「带走数据」的兜底通道，
  一篇论文的 PDF 丢了不该让全部数据都带不走。
"""

import io
import json
import logging
import re
import shutil
import uuid
import zipfile
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import __version__
from app.core.config import get_settings
from app.models.experiment import Experiment
from app.models.idea import Idea
from app.models.library_direction import DirectionLibrary, LibraryPaper, TopicSourceLibrary
from app.models.manuscript import Manuscript
from app.models.paper import Paper, PaperHighlight, PaperNote
from app.models.project import Project
from app.models.voyage import VoyageRun
from app.services.citations import (
    assign_citation_keys,
    build_bibtex_for,
    build_csl_json,
    papers_for_library_export,
)
from app.services.hypothesis_tree import tree_for_run
from app.services.latex_compile import latest_ok_pdf
from app.services.libraries import library_definition
from app.services.manuscripts import asset_path
from app.services.projects import in_my_projects
from app.services.wiki_export import build_obsidian_zip_for_libraries

logger = logging.getLogger(__name__)

# 进度回调：facet 名 + 当前累计计数（worker 侧转成 paper-task 事件发给前端）
ProgressFn = Callable[[str, dict[str, int]], Awaitable[None]]


def exports_dir() -> Path:
    """导出产物落盘位置（api 与 worker 同挂数据卷，下载端点直接读这里）。"""
    return Path(get_settings().data_dir) / "exports"


def export_zip_path(task_id: str) -> Path:
    return exports_dir() / f"{task_id}.zip"


# 并发锁与下载归属都放 Redis（与 paper-task 归属同思路，不建表）。
# 归属 key 存活 24h：zip 是一次性打包产物，给足下载窗口后连同鉴权一起过期。
EXPORT_OWNER_TTL_SECONDS = 24 * 3600
# 活动锁 TTL 兜底：worker 崩了没清锁时最多锁 2 小时，不至于永远导不了
EXPORT_ACTIVE_TTL_SECONDS = 2 * 3600


def export_owner_key(task_id: str) -> str:
    return f"full_export_owner:{task_id}"


def export_active_key(user_id: str) -> str:
    return f"full_export_active:{user_id}"


# 文件/目录名里剔除路径分隔与控制字符；库名/题名多为中文，zip 原生支持 UTF-8，
# 不做转拼音之类的破坏性处理（file-over-app：用户看到的名字就是他起的名字）
_UNSAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_name(name: str | None, used: set[str], fallback: str = "untitled") -> str:
    base = _UNSAFE_NAME_RE.sub(" ", (name or "").strip()).strip(" .")[:80] or fallback
    candidate, n = base, 2
    # 大小写不敏感去重：macOS/Windows 文件系统大小写不敏感，解 zip 时会互相覆盖
    while candidate.lower() in used:
        candidate = f"{base}-{n}"
        n += 1
    used.add(candidate.lower())
    return candidate


def _dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def _yaml_str(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)  # JSON 字符串是合法 YAML 标量


def _warn(manifest: dict[str, Any], facet: str, item: str, error: Exception) -> None:
    manifest["warnings"].append({"facet": facet, "item": item, "error": str(error)})
    logger.warning("full export: %s/%s failed: %s", facet, item, error)


async def _user_libraries(session: AsyncSession, user_id: uuid.UUID) -> list[DirectionLibrary]:
    """导出的库范围 = 我建的库 ∪ 我课题关联的库。

    刻意不用 library_visible_to：可见范围含全部公共库，把别人策展的公共大库
    整个塞进「我的数据」既臃肿也名不副实——带走的应当是自己的与自己在用的。
    """
    linked = (
        select(TopicSourceLibrary.library_id)
        .join(Project, Project.id == TopicSourceLibrary.topic_id)
        .where(Project.owner_id == user_id)
    )
    stmt = (
        select(DirectionLibrary)
        .where(
            or_(
                DirectionLibrary.submitted_by == user_id,
                DirectionLibrary.id.in_(linked),
            )
        )
        .order_by(DirectionLibrary.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _export_libraries(
    session: AsyncSession, user_id: uuid.UUID, root: Path, manifest: dict[str, Any]
) -> None:
    counts = manifest["counts"]
    used: set[str] = set()
    for library in await _user_libraries(session, user_id):
        name = safe_name(library.name, used, fallback=str(library.id)[:8])
        lib_dir = root / "libraries" / name
        try:
            member_rows = (
                await session.execute(
                    select(Paper, LibraryPaper)
                    .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
                    .where(LibraryPaper.library_id == library.id)
                    .order_by(LibraryPaper.created_at)
                )
            ).all()
            _dump_json(
                lib_dir / "library.json",
                {
                    "id": str(library.id),
                    "name": library.name,
                    "library_kind": library.library_kind,
                    "statement": library.statement,
                    "is_public": library.is_public,
                    "cadence": library.cadence,
                    "monthly_budget": library.monthly_budget,
                    "definition": library_definition(library),
                    "created_at": library.created_at,
                    "members": [
                        {
                            "paper_id": str(paper.id),
                            "title": paper.title,
                            "arxiv_id": paper.arxiv_id,
                            "doi": paper.doi,
                            "year": paper.year,
                            "status": member.status,
                            "relevance_score": member.relevance_score,
                            "tldr_note": member.tldr_note,
                        }
                        for paper, member in member_rows
                    ],
                },
            )
            counts["libraries"] += 1

            # 引用文件走既有 builder（与库页「导出引用」完全同口径）
            papers = await papers_for_library_export(
                session, library_id=library.id, user_id=user_id
            )
            if papers:
                keys = assign_citation_keys(papers)
                (lib_dir / "papers.bib").write_text(
                    build_bibtex_for(papers, keys), encoding="utf-8"
                )
                _dump_json(lib_dir / "papers.csl.json", build_csl_json(papers))
                counts["papers"] += len(papers)
                for paper in papers:
                    # PDF 按 citekey 命名：和 papers.bib 逐条对得上，离线也能引用
                    try:
                        if paper.pdf_path and Path(paper.pdf_path).is_file():
                            pdf_dir = lib_dir / "pdfs"
                            pdf_dir.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(paper.pdf_path, pdf_dir / f"{keys[paper.id]}.pdf")
                            counts["pdfs"] += 1
                    except OSError as e:
                        _warn(manifest, "libraries", f"{library.name}/pdf/{paper.title}", e)
        except Exception as e:  # noqa: BLE001 — 单库失败不拖垮整包
            _warn(manifest, "libraries", library.name, e)


def render_paper_notes_md(
    paper: Paper,
    notes: Sequence[PaperNote],
    highlights: Sequence[PaperHighlight],
) -> str:
    """一篇论文的笔记 + 划线 → 单个 Markdown 文本。

    全量导出与常驻文件投影（services/file_projection.py，#719）共用同一渲染器：
    两边各写一份必然漂移，而这份文件正是「离开平台也读得懂」的承诺本体。
    frontmatter 带论文标识（arxiv_id / doi / 标题），离线也能定位是哪篇论文。
    """
    lines = [
        "---",
        f"title: {_yaml_str(paper.title)}",
        f"paper_id: {_yaml_str(paper.id)}",
        f"arxiv_id: {_yaml_str(paper.arxiv_id)}",
        f"doi: {_yaml_str(paper.doi)}",
        f"year: {_yaml_str(paper.year)}",
        "---",
        "",
        f"# {paper.title}",
    ]
    if notes:
        lines += ["", "## 笔记 Notes"]
        for note in notes:
            lines += ["", f"### {note.created_at:%Y-%m-%d %H:%M}", "", note.content]
    if highlights:
        lines += ["", "## 划线 Highlights"]
        for hl in highlights:
            lines += ["", f"- p.{hl.page}: > {hl.selected_text}"]
            if hl.note:
                lines += [f"  - {hl.note}"]
    return "\n".join(lines) + "\n"


async def _export_notes(
    session: AsyncSession, user_id: uuid.UUID, root: Path, manifest: dict[str, Any]
) -> None:
    """笔记/划线按论文分组落 Markdown（file-over-app：frontmatter 带论文标识，
    离开平台后仍能凭 arxiv_id/doi/标题定位到是哪篇论文的笔记）。"""
    counts = manifest["counts"]
    note_rows = (
        await session.execute(
            select(PaperNote, Paper)
            .join(Paper, Paper.id == PaperNote.paper_id)
            .where(PaperNote.author_id == user_id)
            .order_by(PaperNote.created_at)
        )
    ).all()
    hl_rows = (
        await session.execute(
            select(PaperHighlight, Paper)
            .join(Paper, Paper.id == PaperHighlight.paper_id)
            .where(PaperHighlight.author_id == user_id)
            .order_by(PaperHighlight.page, PaperHighlight.created_at)
        )
    ).all()
    by_paper: dict[uuid.UUID, dict[str, Any]] = {}
    for note, paper in note_rows:
        entry = by_paper.setdefault(paper.id, {"paper": paper, "notes": [], "highlights": []})
        entry["notes"].append(note)
        counts["notes"] += 1
    for hl, paper in hl_rows:
        entry = by_paper.setdefault(paper.id, {"paper": paper, "notes": [], "highlights": []})
        entry["highlights"].append(hl)
        counts["highlights"] += 1

    used: set[str] = set()
    for entry in by_paper.values():
        paper: Paper = entry["paper"]
        try:
            name = safe_name(paper.title, used, fallback=str(paper.id)[:8])
            notes_dir = root / "notes"
            notes_dir.mkdir(parents=True, exist_ok=True)
            (notes_dir / f"{name}.md").write_text(
                render_paper_notes_md(paper, entry["notes"], entry["highlights"]),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            _warn(manifest, "notes", paper.title, e)


async def _export_wiki(
    session: AsyncSession, user_id: uuid.UUID, root: Path, manifest: dict[str, Any]
) -> None:
    """每库一个 Obsidian vault，复用既有 builder（zip bytes 原地解开成目录）。

    重造一份「wiki 页写 markdown」的逻辑必然与库页导出漂移（图链重写、双链、
    笔记小节都在 builder 里），所以宁可 zip→解包一次，也不复制那段逻辑。
    """
    counts = manifest["counts"]
    used: set[str] = set()
    for library in await _user_libraries(session, user_id):
        try:
            has_members = (
                await session.execute(
                    select(LibraryPaper.paper_id)
                    .where(
                        LibraryPaper.library_id == library.id,
                        LibraryPaper.status.in_(("compiled", "included")),
                    )
                    .limit(1)
                )
            ).first() is not None
            if not has_members:
                continue  # 空库的 vault 只剩占位 index，不值得占一个目录
            vault = await build_obsidian_zip_for_libraries(
                session, library_ids=[library.id], title=library.name, user_id=user_id
            )
            name = safe_name(library.name, used, fallback=str(library.id)[:8])
            target = root / "wiki" / name
            target.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(vault)) as zf:
                zf.extractall(target)  # 条目名都是 builder 生成的 slug，无穿越风险
                counts["wiki_pages"] += sum(
                    1 for n in zf.namelist() if n.startswith("papers/") and n.endswith(".md")
                )
        except Exception as e:  # noqa: BLE001
            _warn(manifest, "wiki", library.name, e)


def _parse_artifact(artifacts: dict[str, Any], key: str) -> dict[str, Any] | None:
    raw = artifacts.get(key)
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def _render_proposal_md(run: VoyageRun, artifact: dict[str, Any]) -> str:
    """研究方案的可读渲染：discovery-summary.json 是给前端的结构化产物，
    导出时铺成 Markdown——带走的文件要能直接读，不能要求先装个查看器。"""
    lines = [f"# 研究方案 · {run.goal}", ""]
    if artifact.get("direction"):
        lines += [f"> 方向：{artifact['direction']}", ""]
    if artifact.get("summary"):
        lines += [str(artifact["summary"]), ""]
    for i, hyp in enumerate(artifact.get("hypotheses") or [], start=1):
        lines += [f"## 假设 {i}：{hyp.get('statement', '')}", ""]
        if hyp.get("score") is not None:
            lines += [f"- 评分：{hyp['score']}"]
        if hyp.get("status"):
            lines += [f"- 状态：{hyp['status']}"]
        lines += [""]
    pruned = artifact.get("pruned_appendix") or []
    if pruned:
        lines += ["## 附录：被剪枝的分支", ""]
        for item in pruned:
            lines += [f"- {item.get('statement', '')} — {item.get('reason', '')}"]
        lines += [""]
    return "\n".join(lines)


async def _export_discovery(
    session: AsyncSession, user_id: uuid.UUID, root: Path, manifest: dict[str, Any]
) -> None:
    counts = manifest["counts"]
    runs = (
        (
            await session.execute(
                select(VoyageRun)
                .where(VoyageRun.kind == "discovery", VoyageRun.created_by == user_id)
                .order_by(VoyageRun.created_at)
            )
        )
        .scalars()
        .all()
    )
    used: set[str] = set()
    for run in runs:
        try:
            name = safe_name(run.goal, used, fallback=str(run.id)[:8])
            run_dir = root / "discovery" / name
            nodes = await tree_for_run(session, run.id)
            _dump_json(
                run_dir / "tree.json",
                {
                    "run_id": str(run.id),
                    "goal": run.goal,
                    "status": run.status,
                    "nodes": [
                        {
                            "id": str(n.id),
                            "parent_id": str(n.parent_id) if n.parent_id else None,
                            "kind": n.kind,
                            "statement": n.statement,
                            "status": n.status,
                            "score": n.score,
                            "grounding": n.grounding,
                            "novelty_report": n.novelty_report,
                            "feasibility": n.feasibility,
                        }
                        for n in nodes
                    ],
                },
            )
            # 产物存 checkpoint["artifacts"]（值是 JSON 字符串，同 api/hypotheses.py 口径）
            artifacts = (run.checkpoint or {}).get("artifacts") or {}
            summary = _parse_artifact(artifacts, "discovery-summary.json")
            if summary is not None:
                (run_dir / "proposal.md").write_text(
                    _render_proposal_md(run, summary), encoding="utf-8"
                )
            disclosure = _parse_artifact(artifacts, "discovery-disclosure.json")
            if disclosure is not None:
                _dump_json(run_dir / "disclosure.json", disclosure)
            counts["discovery_runs"] += 1
        except Exception as e:  # noqa: BLE001
            _warn(manifest, "discovery", run.goal, e)


async def _export_experiments(
    session: AsyncSession, user_id: uuid.UUID, root: Path, manifest: dict[str, Any]
) -> None:
    counts = manifest["counts"]
    rows = (
        await session.execute(
            select(Experiment, Idea.title)
            .join(Idea, Idea.id == Experiment.idea_id)
            .where(
                in_my_projects(Experiment.project_id, user_id),
                Experiment.trashed_at.is_(None),
            )
            .options(selectinload(Experiment.runs))
            .order_by(Experiment.created_at)
        )
    ).all()
    used: set[str] = set()
    for experiment, idea_title in rows:
        try:
            name = safe_name(idea_title, used, fallback=str(experiment.id)[:8])
            _dump_json(
                root / "experiments" / name / "run.json",
                {
                    "id": str(experiment.id),
                    "idea_title": idea_title,
                    "status": experiment.status,
                    "plan": experiment.plan,
                    "budget": experiment.budget,
                    "server_host": experiment.server_host,
                    "workdir": experiment.workdir,
                    "metrics": experiment.metrics,
                    "report": experiment.report,
                    "created_at": experiment.created_at,
                    "runs": [
                        {
                            "seq": r.seq,
                            "command": r.command,
                            "status": r.status,
                            "exit_code": r.exit_code,
                            "metrics": r.metrics,
                            "primary_value": r.primary_value,
                            "reflection": r.reflection,
                            "started_at": r.started_at,
                            "finished_at": r.finished_at,
                        }
                        for r in experiment.runs
                    ],
                    # 产物清单：图表按名字列出（文件在服务器数据卷，路径不外露）
                    "artifacts": {
                        "figures": [
                            {
                                "index": f.get("index"),
                                "name": f.get("name"),
                                "caption": f.get("caption"),
                            }
                            for f in experiment.figures or []
                        ],
                    },
                },
            )
            counts["experiments"] += 1
        except Exception as e:  # noqa: BLE001
            _warn(manifest, "experiments", idea_title, e)


async def _export_manuscripts(
    session: AsyncSession, user_id: uuid.UUID, root: Path, manifest: dict[str, Any]
) -> None:
    counts = manifest["counts"]
    manuscripts = (
        (
            await session.execute(
                select(Manuscript)
                .where(
                    in_my_projects(Manuscript.project_id, user_id),
                    Manuscript.trashed_at.is_(None),
                )
                .options(selectinload(Manuscript.files))
                .order_by(Manuscript.created_at)
            )
        )
        .scalars()
        .all()
    )
    used: set[str] = set()
    for manuscript in manuscripts:
        try:
            name = safe_name(manuscript.title, used, fallback=str(manuscript.id)[:8])
            ms_dir = root / "manuscripts" / name
            source_dir = ms_dir / "source"
            for file in manuscript.files:
                if file.is_folder:
                    continue
                rel = Path(file.path)
                # 文件路径来自 DB，仍防一手越界（".." / 绝对路径都拒收，记 warning）
                if rel.is_absolute() or ".." in rel.parts:
                    _warn(
                        manifest,
                        "manuscripts",
                        f"{manuscript.title}/{file.path}",
                        ValueError("unsafe path"),
                    )
                    continue
                target = source_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if file.is_binary:
                    src = asset_path(manuscript.id, file.path)
                    if src.is_file():
                        shutil.copyfile(src, target)
                else:
                    target.write_text(file.content or "", encoding="utf-8")
            pdf = latest_ok_pdf(manuscript)
            if pdf is not None:
                ms_dir.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(pdf, ms_dir / "compiled.pdf")
            counts["manuscripts"] += 1
        except Exception as e:  # noqa: BLE001
            _warn(manifest, "manuscripts", manuscript.title, e)


_README = """# Polaris 全量导出 / Full export

这是你在 Polaris 里全部数据的打包副本，随时可以离开平台继续使用：

- `libraries/<库名>/`：文献库设置与成员清单（library.json）、引用文件
  （papers.bib / papers.csl.json，可导入 Zotero 等工具）、已下载的 PDF（pdfs/，
  文件名与 papers.bib 的 citekey 一一对应）。
- `notes/`：你的论文笔记与 PDF 划线，按论文一篇一个 Markdown 文件，
  开头的 frontmatter 带论文标识（arxiv_id / doi / 标题）。
- `wiki/<库名>/`：各库的论文解读页，Obsidian vault 结构，可直接用 Obsidian 打开。
- `discovery/<任务名>/`：假设探索任务的完整留痕——假设树（tree.json）、
  研究方案（proposal.md）、过程披露（disclosure.json）。
- `experiments/<实验名>/run.json`：实验计划、逐次运行记录、指标与产物清单。
- `manuscripts/<题名>/`：论文稿件源文件（source/）与最近一次编译的 PDF（compiled.pdf）。
- `manifest.json`：导出时间、版本、各面计数；导出中跳过的单项记录在 warnings。

---

This archive is a portable copy of all your data in Polaris:

- `libraries/<name>/`: library settings and member list (library.json), citation
  files (papers.bib / papers.csl.json, importable into Zotero etc.), and downloaded
  PDFs (pdfs/, named after the citekeys in papers.bib).
- `notes/`: your paper notes and PDF highlights, one Markdown file per paper with a
  frontmatter identifying the paper (arxiv_id / doi / title).
- `wiki/<name>/`: compiled paper interpretations per library, as an Obsidian vault.
- `discovery/<run>/`: hypothesis-discovery runs — the hypothesis tree (tree.json),
  the research proposal (proposal.md), and the process disclosure (disclosure.json).
- `experiments/<name>/run.json`: experiment plan, per-run records, metrics and an
  artifact inventory.
- `manuscripts/<title>/`: manuscript sources (source/) plus the latest compiled PDF.
- `manifest.json`: export time, version and per-facet counts; items skipped during
  export are listed under warnings.
"""

# 导出的面（顺序即进度顺序）
_FACETS: list[tuple[str, Any]] = [
    ("libraries", _export_libraries),
    ("notes", _export_notes),
    ("wiki", _export_wiki),
    ("discovery", _export_discovery),
    ("experiments", _export_experiments),
    ("manuscripts", _export_manuscripts),
]

_COUNT_KEYS = (
    "libraries",
    "papers",
    "pdfs",
    "notes",
    "highlights",
    "wiki_pages",
    "discovery_runs",
    "experiments",
    "manuscripts",
)


def _zip_tree(tree_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(tree_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(tree_dir).as_posix())


async def run_full_export(
    session: AsyncSession,
    user_id: uuid.UUID,
    dest_dir: Path,
    *,
    progress: ProgressFn | None = None,
) -> tuple[Path, dict[str, Any]]:
    """组装目录树并打 zip；返回 (zip 路径, manifest)。

    树落 ``dest_dir/tree``，zip 落 ``dest_dir/export.zip``；打包完成后树目录
    即被清掉（zip 才是产物，树只是中间态）。facet 级异常也只记 warnings：
    「随时可走」的承诺不能被任何一面的数据伤破坏。
    """
    dest_dir = Path(dest_dir)
    tree = dest_dir / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "exported_at": datetime.now(UTC).isoformat(),
        "version": __version__,
        "user_id": str(user_id),
        "counts": dict.fromkeys(_COUNT_KEYS, 0),
        "warnings": [],
    }
    for facet, export_fn in _FACETS:
        try:
            await export_fn(session, user_id, tree, manifest)
        except Exception as e:  # noqa: BLE001
            _warn(manifest, facet, "*", e)
        if progress is not None:
            await progress(facet, dict(manifest["counts"]))
    (tree / "README.md").write_text(_README, encoding="utf-8")
    _dump_json(tree / "manifest.json", manifest)
    zip_path = dest_dir / "export.zip"
    _zip_tree(tree, zip_path)
    shutil.rmtree(tree, ignore_errors=True)
    return zip_path, manifest


async def run_full_export_task(redis: Redis, *, task_id: str, user_id: str) -> dict[str, Any]:
    """worker 入口：跑导出、把 zip 挪到下载位、发进度/完成事件。

    事件走 paper-task 通道（与 Zotero 导入同口径）：API 入队前已注册任务归属，
    前端订阅 /paper-tasks/{task_id}/events。「done」在 zip 就位**之后**才发——
    前端收到 done 即可下载，不能有窗口期。
    """
    from app.core.db import get_sessionmaker
    from app.core.events import EventBus, publish_paper_task_event

    bus = EventBus(redis)
    workdir = exports_dir() / task_id
    try:
        async with get_sessionmaker()() as session:

            async def _progress(facet: str, counts: dict[str, int]) -> None:
                await publish_paper_task_event(
                    bus, task_id, "export_progress", {"facet": facet, "counts": counts}
                )

            zip_path, manifest = await run_full_export(
                session, uuid.UUID(user_id), workdir, progress=_progress
            )
        final = export_zip_path(task_id)
        final.parent.mkdir(parents=True, exist_ok=True)
        zip_path.replace(final)
        shutil.rmtree(workdir, ignore_errors=True)
        await publish_paper_task_event(
            bus,
            task_id,
            "done",
            {
                "counts": manifest["counts"],
                "warnings": manifest["warnings"],
                "download_path": f"/export/full/{task_id}/download",
            },
        )
        return {"counts": manifest["counts"]}
    except Exception as e:  # noqa: BLE001 — 失败以 error 事件告知前端，不再重抛（重试无意义）
        logger.exception("full export failed: task=%s user=%s", task_id, user_id)
        shutil.rmtree(workdir, ignore_errors=True)
        await publish_paper_task_event(bus, task_id, "error", {"message": str(e)})
        return {"error": str(e)}
    finally:
        # 无论成败都放并发锁：锁的语义是「同一用户同时最多一个导出任务」
        await redis.delete(export_active_key(user_id))
