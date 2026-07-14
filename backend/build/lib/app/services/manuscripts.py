"""稿件业务逻辑（docs/api-m5-b.md §1/§2/§3/§5/§7，不 import fastapi）。

- 模板 pack：app/assets/templates/<key>/（meta.json + main.tex 骨架 + 简化 .sty）；
  创建稿件时展开为 ManuscriptFile（.sty/.cls/.bst 标记 readonly）；
- fact-pack：从 idea + experiment（假设/指标/图表）+ 项目文献库（compiled/included，
  bibkey 走 citations.assign_citation_keys）组装的防幻觉事实源；
- 写作 voyage（kind=paper_writing）：同 manuscript 互斥；
- submit：latest_compile.status=ok 前置，创建 paper_submission 闸门，
  审批通过 → status=submitted（gates API 联动 decide_submission_from_gate）。
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.activity import Activity
from app.models.experiment import Experiment
from app.models.gate import Gate
from app.models.idea import Idea
from app.models.manuscript import Manuscript, ManuscriptFile
from app.models.paper import Paper
from app.models.project import Project, ProjectMember
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.schemas.manuscript import ManuscriptCreate
from app.services.citations import DEFAULT_EXPORT_STATUSES, assign_citation_keys

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
# 编译时自动生成的只读虚拟文件（用户不可占用这些路径）
RESERVED_PATHS = frozenset({"references.bib"})
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


class CompileRequiredError(Exception):
    """submit 前置：最新编译不存在或未通过。"""


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
    stmt = (
        select(Paper)
        .where(Paper.project_id == project_id, Paper.status.in_(DEFAULT_EXPORT_STATUSES))
        .order_by(Paper.year.asc().nulls_last(), Paper.created_at.asc())
    )
    papers = (await session.execute(stmt)).scalars().all()
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
    manuscript.fact_pack = await build_fact_pack(session, manuscript)
    await session.commit()
    await session.refresh(manuscript)
    return manuscript.fact_pack


# ---- 创建 / 读取 / 更新 ----


async def create_manuscript(
    session: AsyncSession, *, project: Project, data: ManuscriptCreate, user_id: uuid.UUID
) -> Manuscript:
    template_meta(data.template)  # 先校验模板 key（TemplateNotFoundError）
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
        status="draft",
    )
    session.add(manuscript)
    await session.flush()
    for path, content, readonly in template_files(data.template, title=data.title):
        session.add(
            ManuscriptFile(
                manuscript_id=manuscript.id,
                path=path,
                content=content,
                readonly=readonly,
                updated_by=user_id,
            )
        )
    manuscript.fact_pack = await build_fact_pack(session, manuscript)
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


async def list_manuscripts(session: AsyncSession, *, project_id: uuid.UUID) -> list[Manuscript]:
    stmt = (
        select(Manuscript)
        .where(Manuscript.project_id == project_id)
        .order_by(Manuscript.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


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
    if file.readonly:
        raise FileReadonlyError(file.path)
    await session.delete(file)
    await session.commit()


# ---- 写作 voyage（docs/api-m5-b.md §5） ----


def resolve_sections(template: str, requested: list[str] | None) -> tuple[list[str], bool]:
    """请求节 → (固定顺序正文节列表, 是否写 related_work)。非法节名抛错。"""
    meta_sections = set(template_meta(template).get("sections") or [])
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
    if await find_active_writing_voyage(session, manuscript) is not None:
        raise WritingInProgressError(str(manuscript.id))
    body, related = resolve_sections(manuscript.template, sections)
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
    gate = Gate(
        project_id=manuscript.project_id,
        kind="paper_submission",
        payload={
            "manuscript_id": str(manuscript.id),
            "title": manuscript.title,
            "compile_version": latest.get("version"),
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


async def decide_submission_from_gate(
    session: AsyncSession, gate: Gate, *, approved: bool
) -> Manuscript | None:
    """gates 审批联动：paper_submission 批准 → submitted；驳回 → 回退 compiled。"""
    if gate.kind != "paper_submission":
        return None
    raw = (gate.payload or {}).get("manuscript_id")
    if not raw:
        return None
    try:
        manuscript_id = uuid.UUID(str(raw))
    except ValueError:
        return None
    manuscript = await session.get(Manuscript, manuscript_id)
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
