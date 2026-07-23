"""稿件业务逻辑（docs/api-m5-b.md §1/§2/§3/§5/§7，不 import fastapi）。

- 模板 pack：app/assets/templates/<key>/（meta.json + main.tex 骨架 + 简化 .sty）；
  创建稿件时展开为 ManuscriptFile（.sty/.cls/.bst 标记 readonly）；
- fact-pack：从 idea + experiment（假设/指标/图表）+ 项目文献库（compiled/included，
  bibkey 走 citations.assign_citation_keys）组装的防幻觉事实源；
- 写作 voyage（kind=paper_writing）：同 manuscript 互斥；
- submit：latest_compile.status=ok + review_passed（M5-C）双前置，创建
  paper_submission 闸门，审批通过 → status=submitted（gates API 联动
  decide_submission_from_gate；review_passed 缺失时管理员可用 override 审批跳过）。
"""

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.models.activity import Activity
from app.models.experiment import Experiment
from app.models.gate import Gate
from app.models.idea import Idea
from app.models.library_direction import LibraryPaper
from app.models.manuscript import Manuscript, ManuscriptFile
from app.models.project import Project, ProjectMember
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.schemas.manuscript import ManuscriptCreate
from app.services.citations import DEFAULT_EXPORT_STATUSES, assign_citation_keys
from app.services.libraries import (
    dedupe_member_rows,
    get_source_library_ids,
    member_papers_stmt,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "assets" / "templates"
TEMPLATE_KEYS = ("neurips2026", "iclr2026", "acl")
WRITING_VOYAGE_KIND = "paper_writing"
# 分节固定顺序（related_work 延后到编译之后单独写，docs/api-m5-b.md §5）
SECTION_ORDER = (
    "introduction",
    "method",
    "experimental_setup",
    "results",
    "conclusion",
    "abstract",
)
# 编译时自动生成的保留路径前缀（figures/ 为实验图目录，用户不可占用）
# 注：references.bib 现为可见可编辑的真实文件（建稿时从事实包生成），不再保留
RESERVED_PATHS = frozenset()
RESERVED_PREFIXES = ("figures/",)
READONLY_SUFFIXES = (".sty", ".cls", ".bst")

_TITLE_PLACEHOLDER = "{{POLARIS_TITLE}}"
_WRITING_TOKENS_PER_SECTION = 30_000


class TemplateNotFoundError(Exception):
    """未知模板 key。"""


class IdeaNotFoundError(Exception):
    """idea 不存在或不属于该项目。"""


class ExperimentNotFoundError(Exception):
    """experiment 不存在或不属于该项目。"""


class FilePathInvalidError(Exception):
    """文件路径非法（绝对路径 / .. / 保留路径）或重名。"""


class FileReadonlyError(Exception):
    """对只读模板文件执行改/删。"""


class WritingInProgressError(Exception):
    """同一稿件已有写作 voyage 在跑。"""


class InvalidSectionsError(Exception):
    """draft 请求包含模板之外的节名。"""


class StructureError(Exception):
    """主文件缺少 document 环境，无法初始化为结构化文章。"""


class CompileRequiredError(Exception):
    """submit / review 前置：最新编译不存在或未通过。"""


class ReviewRequiredError(Exception):
    """submit 前置（M5-C）：评审未通过（review_passed=false）。"""


# ---- 模板 pack ----


def list_templates() -> list[dict[str, Any]]:
    metas = []
    for key in TEMPLATE_KEYS:
        meta_path = TEMPLATES_DIR / key / "meta.json"
        if meta_path.is_file():
            metas.append(json.loads(meta_path.read_text(encoding="utf-8")))
    return metas


def template_meta(key: str) -> dict[str, Any]:
    meta_path = TEMPLATES_DIR / key / "meta.json"
    if key not in TEMPLATE_KEYS or not meta_path.is_file():
        raise TemplateNotFoundError(key)
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _escape_latex(text: str) -> str:
    for src, dst in (
        ("\\", r"\textbackslash{}"),
        ("&", r"\&"),
        ("%", r"\%"),
        ("$", r"\$"),
        ("#", r"\#"),
        ("_", r"\_"),
        ("{", r"\{"),
        ("}", r"\}"),
        ("~", r"\textasciitilde{}"),
        ("^", r"\textasciicircum{}"),
    ):
        text = text.replace(src, dst)
    return text


def template_files(key: str, *, title: str) -> list[tuple[str, str, bool]]:
    """展开模板 pack → [(path, content, readonly)]；main.tex 注入稿件标题。"""
    template_meta(key)  # 校验存在性
    files: list[tuple[str, str, bool]] = []
    for path in sorted((TEMPLATES_DIR / key).iterdir()):
        if path.name == "meta.json" or not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        if path.name == "main.tex":
            content = content.replace(_TITLE_PLACEHOLDER, _escape_latex(title))
        readonly = path.suffix in READONLY_SUFFIXES
        files.append((path.name, content, readonly))
    return files


# ---- 二进制资源（图片/字体/PDF 等）：字节落磁盘，ManuscriptFile 只留元数据 ----


def manuscript_assets_dir(manuscript_id: uuid.UUID | str) -> Path:
    return Path(get_settings().data_dir) / "manuscripts" / str(manuscript_id) / "assets"


def asset_path(manuscript_id: uuid.UUID | str, path: str) -> Path:
    return manuscript_assets_dir(manuscript_id) / path


def write_binary_asset(manuscript_id: uuid.UUID | str, path: str, data: bytes) -> None:
    target = asset_path(manuscript_id, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def read_binary_asset(manuscript_id: uuid.UUID | str, path: str) -> bytes | None:
    target = asset_path(manuscript_id, path)
    return target.read_bytes() if target.is_file() else None


# ---- fact-pack 组装（docs/api-m5-b.md §3） ----


def _last_value(series: Any) -> float | None:
    if not isinstance(series, list) or not series:
        return None
    last = series[-1]
    value = last.get("value") if isinstance(last, dict) else None
    return float(value) if isinstance(value, int | float) else None


def _metrics_pack(experiment: Experiment) -> list[dict[str, Any]]:
    """全 run 指标：[{name, runs: [{seq, value}], best}]；best 对主指标 direction 感知。"""
    plan = experiment.plan or {}
    pm = plan.get("primary_metric") or {}
    pm_name = str(pm.get("name") or "")
    pm_direction = str(pm.get("direction") or "maximize")

    names: list[str] = []
    per_metric: dict[str, list[dict[str, Any]]] = {}
    for run in experiment.runs:
        for name, series in (run.metrics or {}).items():
            value = _last_value(series)
            if value is None:
                continue
            if name not in per_metric:
                per_metric[name] = []
                names.append(name)
            per_metric[name].append({"seq": run.seq, "value": value})

    metrics: list[dict[str, Any]] = []
    for name in names:
        values = [entry["value"] for entry in per_metric[name]]
        minimize = name == pm_name and pm_direction == "minimize"
        best = min(values) if minimize else max(values)
        metrics.append({"name": name, "runs": per_metric[name], "best": best})
    return metrics


def _hypotheses_pack(experiment: Experiment) -> list[dict[str, Any]]:
    hyps = (experiment.plan or {}).get("hypotheses") or []
    return [
        {
            "text": str(h.get("text", "")),
            "status": str(h.get("status", "testing")),
            "evidence": h.get("evidence"),
        }
        for h in hyps
        if isinstance(h, dict)
    ]


def _figures_pack(experiment: Experiment) -> list[dict[str, Any]]:
    return [
        {
            "fig_id": f"exp_fig_{int(f['index'])}",
            "caption": f.get("caption"),
            "source": "experiment",
        }
        for f in experiment.figures or []
        if isinstance(f, dict) and f.get("index") is not None
    ]


async def _citations_pack(session: AsyncSession, *, project_id: uuid.UUID) -> list[dict[str, Any]]:
    """项目库 compiled/included 全部论文（排序与引用导出一致，bibkey 同规则）。

    条目含契约字段 {bibkey, title, year}，附加内部字段 paper_id / source
    供编译时按固定 key 生成 references.bib（避免库变动导致 key 漂移）。
    """
    library_ids = await get_source_library_ids(session, project_id)
    if not library_ids:
        return []  # 课题无关联库 = 无引用语料
    rows = dedupe_member_rows(
        (
            await session.execute(
                member_papers_stmt(library_ids).where(
                    LibraryPaper.status.in_(DEFAULT_EXPORT_STATUSES)
                )
            )
        ).all()
    )
    rows.sort(key=lambda pm: (pm[0].year is None, pm[0].year or 0, pm[1].created_at))
    papers = [p for p, _ in rows]
    keys = assign_citation_keys(papers)
    return [
        {
            "bibkey": keys[p.id],
            "title": p.title,
            "year": p.year,
            "paper_id": str(p.id),
            "source": "library",
        }
        for p in papers
    ]


async def build_fact_pack(session: AsyncSession, manuscript: Manuscript) -> dict[str, Any]:
    idea_pack = None
    if manuscript.idea_id is not None:
        idea = await session.get(Idea, manuscript.idea_id)
        if idea is not None:
            idea_pack = {"title": idea.title, "summary": idea.summary}

    hypotheses: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    if manuscript.experiment_id is not None:
        stmt = (
            select(Experiment)
            .where(Experiment.id == manuscript.experiment_id)
            .options(selectinload(Experiment.runs))
        )
        experiment = (await session.execute(stmt)).scalar_one_or_none()
        if experiment is not None:
            hypotheses = _hypotheses_pack(experiment)
            metrics = _metrics_pack(experiment)
            figures = _figures_pack(experiment)

    return {
        "idea": idea_pack,
        "hypotheses": hypotheses,
        "metrics": metrics,
        "figures": figures,
        "citations": await _citations_pack(session, project_id=manuscript.project_id),
        "generated_at": datetime.now(UTC).isoformat(),
    }


async def refresh_fact_pack(session: AsyncSession, manuscript: Manuscript) -> dict[str, Any]:
    fresh = await build_fact_pack(session, manuscript)
    # 评审修订说明不是事实源本身，重建时保留（供下次 AI 起草参考）
    old = manuscript.fact_pack or {}
    if old.get("revision_notes"):
        fresh["revision_notes"] = old["revision_notes"]
    manuscript.fact_pack = fresh
    await session.commit()
    await session.refresh(manuscript)
    return manuscript.fact_pack


# ---- 创建 / 读取 / 更新 ----


async def create_manuscript(
    session: AsyncSession, *, project: Project, data: ManuscriptCreate, user_id: uuid.UUID
) -> Manuscript:
    from app.services import manuscript_templates  # 延迟导入避免循环

    # 展开模板文件（builtin key 或库内模板 id/key；未知 → TemplateNotFoundError）
    files = await manuscript_templates.expand_files(session, data.template, title=data.title)
    # 编译入口主文件与编译器（Overleaf 式，建稿后用户可改）
    main_tex, engine = await manuscript_templates.build_config(session, data.template)
    if data.idea_id is not None:
        idea = await session.get(Idea, data.idea_id)
        if idea is None or idea.project_id != project.id:
            raise IdeaNotFoundError(str(data.idea_id))
    if data.experiment_id is not None:
        experiment = await session.get(Experiment, data.experiment_id)
        if experiment is None or experiment.project_id != project.id:
            raise ExperimentNotFoundError(str(data.experiment_id))

    manuscript = Manuscript(
        project_id=project.id,
        idea_id=data.idea_id,
        experiment_id=data.experiment_id,
        title=data.title,
        template=data.template,  # template_files 内部校验 key
        main_tex=main_tex,
        engine=engine,
        status="draft",
    )
    session.add(manuscript)
    await session.flush()
    has_bib = any(p == "references.bib" for p, _, _, _ in files)
    for path, data_or_text, readonly, is_binary in files:
        if is_binary:
            write_binary_asset(manuscript.id, path, data_or_text)
            session.add(
                ManuscriptFile(
                    manuscript_id=manuscript.id,
                    path=path,
                    content="",
                    readonly=True,
                    is_binary=True,
                    updated_by=user_id,
                )
            )
        else:
            session.add(
                ManuscriptFile(
                    manuscript_id=manuscript.id,
                    path=path,
                    content=data_or_text,
                    readonly=readonly,
                    updated_by=user_id,
                )
            )
    manuscript.fact_pack = await build_fact_pack(session, manuscript)
    # 若模板未自带 references.bib，从事实包生成一份可见可编辑的（文件列表里能看到、能改）
    if not has_bib:
        from app.services import latex_compile  # 延迟导入避免循环

        session.add(
            ManuscriptFile(
                manuscript_id=manuscript.id,
                path="references.bib",
                content=await latex_compile.build_references_bib(session, manuscript),
                readonly=False,
                updated_by=user_id,
            )
        )
    session.add(
        Activity(
            project_id=project.id,
            actor=f"user:{user_id}",
            kind="manuscript.created",
            message=f"论文草稿已创建：{data.title}",
            payload={"manuscript_id": str(manuscript.id), "template": data.template},
        )
    )
    await session.commit()
    await session.refresh(manuscript)
    return manuscript


# ---- 结构化初始化（AI 起草前置：把 document 环境内容换成带 POLARIS_SECTION 标记的骨架）----

# (POLARIS_SECTION key, LaTeX 章节标题)；abstract 落在 abstract 环境，其余为 \section
_STRUCTURE_SECTIONS: tuple[tuple[str, str], ...] = (
    ("introduction", "Introduction"),
    ("related_work", "Related Work"),
    ("method", "Method"),
    ("experimental_setup", "Experimental Setup"),
    ("results", "Results"),
    ("conclusion", "Conclusion"),
)
_STRUCTURE_PLACEHOLDER = "To be drafted."
_BIB_LINE_RE = re.compile(r"^[ \t]*\\bibliography(?:style)?\{[^{}]*\}[ \t]*$", re.MULTILINE)


def _section_marker_block(key: str) -> str:
    return f"% POLARIS_SECTION: {key}\n{_STRUCTURE_PLACEHOLDER}\n% POLARIS_SECTION_END: {key}\n"


def build_structured_document(content: str) -> str:
    """把 \\begin{document}…\\end{document} 之间换成带 POLARIS_SECTION 标记的研究骨架，
    保留 preamble、\\maketitle（若有标题）与 \\bibliography 声明。
    缺 document 环境 → StructureError。"""
    if "\\begin{document}" not in content or "\\end{document}" not in content:
        raise StructureError("MAIN_TEX_NO_DOCUMENT")
    preamble, rest = content.split("\\begin{document}", 1)
    old_body, tail = rest.split("\\end{document}", 1)

    parts: list[str] = []
    if "\\title" in preamble or "\\maketitle" in old_body:
        parts.append("\\maketitle\n")
    parts.append("\n\\begin{abstract}\n" + _section_marker_block("abstract") + "\\end{abstract}\n")
    for key, heading in _STRUCTURE_SECTIONS:
        parts.append(f"\n\\section{{{heading}}}\\label{{sec:{key}}}\n" + _section_marker_block(key))
    # 保留原有的 \bibliographystyle / \bibliography 声明（顺序不变），否则兜底指向 references
    bib_lines = _BIB_LINE_RE.findall(old_body)
    if bib_lines:
        parts.append("\n" + "\n".join(m.strip() for m in bib_lines) + "\n")
    else:
        parts.append("\n\\bibliographystyle{plainnat}\n\\bibliography{references}\n")
    middle = "".join(parts)
    return f"{preamble}\\begin{{document}}\n{middle}\\end{{document}}{tail}"


DRAFT_TEX = "draft.tex"


async def initialize_structure(
    session: AsyncSession, manuscript: Manuscript, *, user_id: uuid.UUID
) -> tuple[ManuscriptFile, str]:
    """基于「当前编译主文件」新建/更新 draft.tex：保留其 preamble，把 document 正文换成
    POLARIS_SECTION 骨架（供 AI 分节起草与编译）。原主文件保持不变，并把编译主文件切到
    draft.tex。返回 (draft 文件, 内容)。
    源文件不存在 → FilePathInvalidError；源文件无 document 环境 → StructureError。"""
    from app.services import crdt_rooms  # 延迟导入避免循环

    src_path = manuscript.main_tex or "main.tex"
    rows = (
        (
            await session.execute(
                select(ManuscriptFile).where(ManuscriptFile.manuscript_id == manuscript.id)
            )
        )
        .scalars()
        .all()
    )
    by_path = {f.path: f for f in rows}
    src = by_path.get(src_path)
    if src is None or src.is_binary or src.is_folder:
        raise FilePathInvalidError(src_path)

    rooms = crdt_rooms.get_crdt_rooms()
    src_content = rooms.room_content(src.id)
    if src_content is None:
        src_content = src.content
    new_content = build_structured_document(src_content)  # 无 document → StructureError

    draft = by_path.get(DRAFT_TEX)
    if draft is None:
        draft = ManuscriptFile(
            manuscript_id=manuscript.id,
            path=DRAFT_TEX,
            content=new_content,
            readonly=False,
            updated_by=user_id,
        )
        session.add(draft)
        await session.flush()  # 拿 id
    else:
        # 已有 draft.tex：整文件替换（活跃房间实时可见，否则写库 + 存初始化前快照）
        wrote = await rooms.set_content(draft.id, new_content)
        if not wrote:
            from app.services import manuscript_versions  # 延迟导入避免循环

            await manuscript_versions.snapshot_file(
                session, draft, origin="pre_ai", label="结构化初始化前", content=draft.content
            )
            draft.content = new_content
            draft.updated_by = user_id

    manuscript.main_tex = DRAFT_TEX  # 编译主文件切到草稿
    await session.commit()
    await session.refresh(draft)
    return draft, new_content


async def list_manuscripts(
    session: AsyncSession, *, project_id: uuid.UUID, trashed: bool = False
) -> list[Manuscript]:
    """项目下的稿件；trashed=False 只列未删除（置顶优先），True 只列垃圾箱。"""
    cond = Manuscript.trashed_at.is_not(None) if trashed else Manuscript.trashed_at.is_(None)
    stmt = select(Manuscript).where(Manuscript.project_id == project_id, cond)
    if trashed:
        stmt = stmt.order_by(Manuscript.trashed_at.desc())
    else:
        # 置顶（pinned_at 非空）排前面，其次按创建时间倒序
        stmt = stmt.order_by(Manuscript.pinned_at.desc().nulls_last(), Manuscript.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def _owned_manuscripts(
    session: AsyncSession, *, project_id: uuid.UUID, ids: list[uuid.UUID]
) -> list[Manuscript]:
    if not ids:
        return []
    stmt = select(Manuscript).where(Manuscript.project_id == project_id, Manuscript.id.in_(ids))
    return list((await session.execute(stmt)).scalars().all())


async def trash_manuscripts(
    session: AsyncSession, *, project_id: uuid.UUID, ids: list[uuid.UUID]
) -> int:
    """移入垃圾箱（软删除）；返回受影响数量。"""
    rows = await _owned_manuscripts(session, project_id=project_id, ids=ids)
    now = datetime.now(UTC)
    n = 0
    for m in rows:
        if m.trashed_at is None:
            m.trashed_at = now
            n += 1
    await session.commit()
    return n


async def restore_manuscripts(
    session: AsyncSession, *, project_id: uuid.UUID, ids: list[uuid.UUID]
) -> int:
    """从垃圾箱恢复；返回受影响数量。"""
    rows = await _owned_manuscripts(session, project_id=project_id, ids=ids)
    n = 0
    for m in rows:
        if m.trashed_at is not None:
            m.trashed_at = None
            n += 1
    await session.commit()
    return n


async def purge_manuscripts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    ids: list[uuid.UUID] | None = None,
) -> int:
    """永久删除。ids=None → 清空该项目垃圾箱（删所有已在垃圾箱的稿件）；
    否则只永久删除指定 id 中已在垃圾箱的稿件（避免误删未删除的）。返回删除数量。"""
    if ids is None:
        rows = await list_manuscripts(session, project_id=project_id, trashed=True)
    else:
        rows = [
            m
            for m in await _owned_manuscripts(session, project_id=project_id, ids=ids)
            if m.trashed_at is not None
        ]
    n = len(rows)
    for m in rows:
        await session.delete(m)
    await session.commit()
    return n


async def get_manuscript_for_user(
    session: AsyncSession,
    *,
    manuscript_id: uuid.UUID,
    user_id: uuid.UUID,
    with_files: bool = False,
) -> Manuscript | None:
    """取稿件；非项目成员视为不存在（返回 None）。"""
    stmt = (
        select(Manuscript)
        .join(ProjectMember, ProjectMember.project_id == Manuscript.project_id)
        .where(Manuscript.id == manuscript_id, ProjectMember.user_id == user_id)
    )
    if with_files:
        stmt = stmt.options(selectinload(Manuscript.files))
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_file_for_user(
    session: AsyncSession, *, file_id: uuid.UUID, user_id: uuid.UUID
) -> ManuscriptFile | None:
    """取稿件文件；非项目成员视为不存在（返回 None）。CRDT WS on_connect 也用。"""
    stmt = (
        select(ManuscriptFile)
        .join(Manuscript, Manuscript.id == ManuscriptFile.manuscript_id)
        .join(ProjectMember, ProjectMember.project_id == Manuscript.project_id)
        .where(ManuscriptFile.id == file_id, ProjectMember.user_id == user_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_active_writing_voyage(
    session: AsyncSession, manuscript: Manuscript
) -> VoyageRun | None:
    """同稿件未完结的写作 voyage（互斥判定 + detail.writing_voyage_id）。"""
    stmt = (
        select(VoyageRun)
        .where(
            VoyageRun.project_id == manuscript.project_id,
            VoyageRun.kind == WRITING_VOYAGE_KIND,
            VoyageRun.status.not_in(tuple(TERMINAL_STATUSES)),
        )
        .order_by(VoyageRun.created_at.desc())
    )
    for run in (await session.execute(stmt)).scalars().all():
        params = (run.checkpoint or {}).get("params") or {}
        if params.get("manuscript_id") == str(manuscript.id):
            return run
    return None


# ---- 文件管理（docs/api-m5-b.md §2） ----


def _validate_file_path(path: str) -> str:
    path = path.strip().replace("\\", "/")
    if not path or path.startswith("/") or ".." in path.split("/") or path.endswith("/"):
        raise FilePathInvalidError(path)
    if path in RESERVED_PATHS or any(path.startswith(p) for p in RESERVED_PREFIXES):
        raise FilePathInvalidError(path)  # 编译时自动生成的虚拟文件路径，不可占用
    return path


async def _assert_path_free(session: AsyncSession, manuscript_id: uuid.UUID, path: str) -> None:
    stmt = select(ManuscriptFile.id).where(
        ManuscriptFile.manuscript_id == manuscript_id, ManuscriptFile.path == path
    )
    if (await session.execute(stmt)).first() is not None:
        raise FilePathInvalidError(path)


async def create_file(
    session: AsyncSession,
    *,
    manuscript: Manuscript,
    path: str,
    content: str,
    user_id: uuid.UUID,
) -> ManuscriptFile:
    path = _validate_file_path(path)
    await _assert_path_free(session, manuscript.id, path)
    file = ManuscriptFile(
        manuscript_id=manuscript.id,
        path=path,
        content=content,
        readonly=False,
        updated_by=user_id,
    )
    session.add(file)
    await session.commit()
    await session.refresh(file)
    return file


async def create_folder(
    session: AsyncSession, *, manuscript: Manuscript, path: str, user_id: uuid.UUID
) -> ManuscriptFile:
    """新建文件夹占位（is_folder=True，无内容）；供文件树显示空目录。"""
    path = _validate_file_path(path).rstrip("/")
    await _assert_path_free(session, manuscript.id, path)
    folder = ManuscriptFile(
        manuscript_id=manuscript.id,
        path=path,
        content="",
        readonly=False,
        is_folder=True,
        updated_by=user_id,
    )
    session.add(folder)
    await session.commit()
    await session.refresh(folder)
    return folder


async def upload_file(
    session: AsyncSession,
    *,
    manuscript: Manuscript,
    path: str,
    data: bytes,
    user_id: uuid.UUID,
) -> ManuscriptFile:
    """上传文件：文本入 content（可编辑）；二进制字节落磁盘（is_binary，只读）。"""
    path = _validate_file_path(path)
    await _assert_path_free(session, manuscript.id, path)
    binary = b"\x00" in data
    text = ""
    if not binary:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            binary = True
    if binary:
        write_binary_asset(manuscript.id, path, data)
    file = ManuscriptFile(
        manuscript_id=manuscript.id,
        path=path,
        content="" if binary else text,
        readonly=binary,
        is_binary=binary,
        updated_by=user_id,
    )
    session.add(file)
    await session.commit()
    await session.refresh(file)
    return file


async def rename_file(
    session: AsyncSession, *, file: ManuscriptFile, path: str, user_id: uuid.UUID
) -> ManuscriptFile:
    if file.readonly:
        raise FileReadonlyError(file.path)
    path = _validate_file_path(path)
    if path != file.path:
        await _assert_path_free(session, file.manuscript_id, path)
        file.path = path
        file.updated_by = user_id
        await session.commit()
        await session.refresh(file)
    return file


async def delete_file(session: AsyncSession, *, file: ManuscriptFile) -> None:
    if file.readonly and not file.is_binary:
        raise FileReadonlyError(file.path)  # 模板样式只读文件不可删；上传的二进制可删
    mid = file.manuscript_id
    if file.is_binary:
        asset_path(mid, file.path).unlink(missing_ok=True)
    if file.is_folder:
        # 连带删除该目录下的所有文件（含二进制资源）
        prefix = file.path.rstrip("/") + "/"
        stmt = select(ManuscriptFile).where(
            ManuscriptFile.manuscript_id == mid, ManuscriptFile.path.startswith(prefix)
        )
        for child in (await session.execute(stmt)).scalars().all():
            if child.is_binary:
                asset_path(mid, child.path).unlink(missing_ok=True)
            await session.delete(child)
    await session.delete(file)
    await session.commit()


# ---- 写作 voyage（docs/api-m5-b.md §5） ----


def resolve_sections(
    known_sections: list[str], requested: list[str] | None
) -> tuple[list[str], bool]:
    """请求节 → (固定顺序正文节列表, 是否写 related_work)。非法节名抛错。

    known_sections 为模板声明的可写分节（内置模板有值；官方上传模板通常为空，
    此时全量起草得到空 body、AI 起草降级为只编译，用户改用内联 AI/手写）。
    """
    meta_sections = set(known_sections)
    if requested is None:
        selected = meta_sections
    else:
        unknown = [s for s in requested if s not in meta_sections]
        if unknown:
            raise InvalidSectionsError(", ".join(unknown))
        selected = set(requested)
    body = [s for s in SECTION_ORDER if s in selected]
    related = "related_work" in selected
    return body, related


async def create_writing_voyage(
    session: AsyncSession,
    *,
    manuscript: Manuscript,
    sections: list[str] | None,
    notes: str | None,
    created_by: uuid.UUID,
) -> VoyageRun:
    from app.services import manuscript_templates  # 延迟导入避免循环

    if await find_active_writing_voyage(session, manuscript) is not None:
        raise WritingInProgressError(str(manuscript.id))
    known = await manuscript_templates.template_section_keys(session, manuscript.template)
    body, related = resolve_sections(known, sections)
    # 起草永远基于最新事实源：先自动重建 fact-pack（库/实验更新后不必手动刷新）
    await refresh_fact_pack(session, manuscript)
    # 事实包刷新后同步 references.bib 文件，保证 AI 起草的 \cite 能解析
    from app.services import latex_compile  # 延迟导入避免循环

    await latex_compile.sync_references_bib(session, manuscript)
    run = VoyageRun(
        kind=WRITING_VOYAGE_KIND,
        goal=f"论文撰写：{manuscript.title}",
        status="planning",
        cursor=0,
        checkpoint={
            "params": {
                "manuscript_id": str(manuscript.id),
                "sections": body,
                "related_work": related,
                "notes": notes,
            }
        },
        budget={"max_tokens": (len(body) + int(related) + 1) * _WRITING_TOKENS_PER_SECTION},
        project_id=manuscript.project_id,
        created_by=created_by,
    )
    session.add(run)
    session.add(
        Activity(
            project_id=manuscript.project_id,
            actor=f"user:{created_by}",
            kind="manuscript.draft_started",
            message=f"AI 起草已启动：{manuscript.title}",
            payload={"manuscript_id": str(manuscript.id), "sections": body},
        )
    )
    await session.commit()
    await session.refresh(run)
    return run


# ---- 投稿（docs/api-m5-b.md §7） ----


async def submit_manuscript(
    session: AsyncSession, *, manuscript: Manuscript, user_id: uuid.UUID
) -> Gate:
    latest = manuscript.latest_compile or {}
    if latest.get("status") != "ok":
        raise CompileRequiredError(str(manuscript.id))
    # M5-C：前置从 compile-ok 升级为 review_passed（管理员可在 gate 审批时 override）
    if not manuscript.review_passed:
        raise ReviewRequiredError(str(manuscript.id))
    gate = Gate(
        project_id=manuscript.project_id,
        kind="paper_submission",
        payload={
            "manuscript_id": str(manuscript.id),
            "title": manuscript.title,
            "compile_version": latest.get("version"),
            "review_passed": manuscript.review_passed,
        },
        requested_by=f"user:{user_id}",
    )
    session.add(gate)
    manuscript.status = "under_review"
    session.add(
        Activity(
            project_id=manuscript.project_id,
            actor=f"user:{user_id}",
            kind="manuscript.submitted_for_review",
            message=f"论文投稿待审批：{manuscript.title}",
            payload={"manuscript_id": str(manuscript.id)},
        )
    )
    await session.commit()
    await session.refresh(gate)
    return gate


async def gate_manuscript(session: AsyncSession, gate: Gate) -> Manuscript | None:
    """paper_submission 闸门指向的稿件（payload.manuscript_id）。"""
    if gate.kind != "paper_submission":
        return None
    raw = (gate.payload or {}).get("manuscript_id")
    if not raw:
        return None
    try:
        manuscript_id = uuid.UUID(str(raw))
    except ValueError:
        return None
    return await session.get(Manuscript, manuscript_id)


async def decide_submission_from_gate(
    session: AsyncSession, gate: Gate, *, approved: bool
) -> Manuscript | None:
    """gates 审批联动：paper_submission 批准 → submitted；驳回 → 回退 compiled。"""
    manuscript = await gate_manuscript(session, gate)
    if manuscript is None or manuscript.status != "under_review":
        return None
    manuscript.status = "submitted" if approved else "compiled"
    session.add(
        Activity(
            project_id=manuscript.project_id,
            actor=f"user:{gate.decided_by}" if gate.decided_by else "system",
            kind="manuscript.submitted" if approved else "manuscript.submit_rejected",
            message=(
                f"论文《{manuscript.title}》投稿审批"
                + ("通过，已标记为 submitted" if approved else "被驳回")
            ),
            payload={"manuscript_id": str(manuscript.id), "gate_id": str(gate.id)},
        )
    )
    await session.commit()
    await session.refresh(manuscript)
    return manuscript
