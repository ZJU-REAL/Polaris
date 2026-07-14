"""论文评审业务逻辑（docs/api-m5-c.md，不 import fastapi）。

- 评审 voyage（kind=paper_review）：同 manuscript 互斥；前置 latest_compile ok；
- 引用核验（§2）：LaTeX \\cite 解析（确定性）→ 库内精确匹配 → S2/OpenAlex
  title 模糊匹配（相似度 + 年份容差 1）三态 exact|minor|fabricated；
- 事实查错（§2）：数字 ↔ fact-pack metrics 确定性比对（白名单启发式与写作
  静态校验一致）、\\ref/图存在性检查；claim 抽查走 LLM（actions_review.py）；
- 评审员聚合（§3）：rating 中位数；|rating−中位| > 3 或 confidence ≤ 2 降权 0.5，
  unreliable 不计入；
- 通过判定（§4）：meta.rating ≥ 6 且无 fabricated → review_passed=true；
  不通过 → weaknesses + 查错表生成修订说明写 fact_pack.revision_notes。
"""

import difflib
import re
import statistics
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import Activity
from app.models.manuscript import Manuscript, ManuscriptFile
from app.models.paper import Paper
from app.models.review import ReviewMessage, ReviewSession
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.services.literature.openalex import OpenAlexClient
from app.services.literature.semantic_scholar import SemanticScholarClient
from app.services.manuscripts import CompileRequiredError

REVIEW_VOYAGE_KIND = "paper_review"

# 默认三人设（docs/api-m5-c.md §3）
DEFAULT_REVIEW_PERSONAS: list[dict[str, str]] = [
    {"name": "苛刻方法论者", "stance": "专挑方法缺陷与实验设计漏洞，质疑一切未消融的设计选择"},
    {"name": "建设性领域专家", "stance": "熟悉领域脉络，指出与现有工作的关系并给出可行的改进建议"},
    {"name": "严格实验复现者", "stance": "只认可可复现的结果，逐条核对实验设置、数字与图表证据"},
]

PASS_RATING = 6.0  # meta.rating ≥ 6 且无 fabricated → 通过
NUMBER_TOLERANCE = 0.01 + 1e-9  # 数字命中 metrics 的容差（与写作静态校验一致）
# title 模糊匹配阈值（S2/OpenAlex）
EXACT_SIMILARITY = 0.92
MINOR_SIMILARITY = 0.75
YEAR_TOLERANCE = 1
_REVIEW_MAX_TOKENS = 150_000

# LaTeX 解析正则（与 actions_writing 的写作静态校验保持同一形态；
# 此处独立定义避免 services → agents 的反向依赖）
CITE_RE = re.compile(r"\\cite[tp]?\*?(?:\[[^\]]*\])?(?:\[[^\]]*\])?\{([^{}]*)\}")
GRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^{}]*)\}")
LABEL_RE = re.compile(r"\\label\{([^{}]*)\}")
REF_RE = re.compile(r"\\(?:auto|eq|page|c|C)?ref\*?\{([^{}]*)\}")
_PERCENT_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)(?:\\%|%)")
_DECIMAL_RE = re.compile(r"\d+\.\d+")
_REF_CONTEXT_RE = re.compile(
    r"(?:Section|Sections|Table|Tables|Figure|Figures|Fig\.|Chapter|Appendix|Eq\.|"
    r"Equation|§)\s*~?\s*$",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")


class ReviewInProgressError(Exception):
    """同一稿件已有评审 voyage 在跑。"""


# ---- voyage 创建 ----


async def find_active_review_voyage(
    session: AsyncSession, manuscript: Manuscript
) -> VoyageRun | None:
    """同稿件未完结的评审 voyage（互斥判定）。"""
    stmt = (
        select(VoyageRun)
        .where(
            VoyageRun.project_id == manuscript.project_id,
            VoyageRun.kind == REVIEW_VOYAGE_KIND,
            VoyageRun.status.not_in(tuple(TERMINAL_STATUSES)),
        )
        .order_by(VoyageRun.created_at.desc())
    )
    for run in (await session.execute(stmt)).scalars().all():
        params = (run.checkpoint or {}).get("params") or {}
        if params.get("manuscript_id") == str(manuscript.id):
            return run
    return None


def resolve_review_personas(raw: Any) -> list[dict[str, str]]:
    """自定义人设不足三个用默认补齐，超出取前三（评审员固定 ×3）。"""
    personas = []
    if isinstance(raw, list):
        personas = [
            {"name": str(p["name"]), "stance": str(p.get("stance") or "")}
            for p in raw
            if isinstance(p, dict) and p.get("name")
        ]
    return (personas + DEFAULT_REVIEW_PERSONAS[len(personas) :])[:3]


async def create_review_voyage(
    session: AsyncSession,
    *,
    manuscript: Manuscript,
    personas: list[dict[str, str]] | None,
    created_by: uuid.UUID,
) -> VoyageRun:
    if (manuscript.latest_compile or {}).get("status") != "ok":
        raise CompileRequiredError(str(manuscript.id))
    if await find_active_review_voyage(session, manuscript) is not None:
        raise ReviewInProgressError(str(manuscript.id))
    run = VoyageRun(
        kind=REVIEW_VOYAGE_KIND,
        goal=f"论文评审：{manuscript.title}",
        status="planning",
        cursor=0,
        checkpoint={
            "params": {
                "manuscript_id": str(manuscript.id),
                "personas": resolve_review_personas(personas),
            }
        },
        budget={"max_tokens": _REVIEW_MAX_TOKENS},
        project_id=manuscript.project_id,
        created_by=created_by,
    )
    session.add(run)
    session.add(
        Activity(
            project_id=manuscript.project_id,
            actor=f"user:{created_by}",
            kind="manuscript.review_started",
            message=f"论文同行评审已启动：{manuscript.title}",
            payload={"manuscript_id": str(manuscript.id)},
        )
    )
    await session.commit()
    await session.refresh(run)
    return run


# ---- LaTeX 源解析（确定性） ----


def strip_tex_comments(line: str) -> str:
    """去掉 % 注释；紧跟数字的 % 视为百分号（与写作静态校验一致）。"""
    return re.sub(r"(?<![\d\\])%.*$", " ", line)


async def load_tex_files(session: AsyncSession, manuscript_id: uuid.UUID) -> list[tuple[str, str]]:
    """非只读 .tex 文件 → [(path, content)]（引用核验 / 事实查错的扫描对象）。"""
    stmt = (
        select(ManuscriptFile)
        .where(
            ManuscriptFile.manuscript_id == manuscript_id,
            ManuscriptFile.readonly.is_(False),
        )
        .order_by(ManuscriptFile.path)
    )
    files = (await session.execute(stmt)).scalars().all()
    return [(f.path, f.content) for f in files if f.path.endswith(".tex")]


def _context_snippet(text: str, start: int, end: int) -> str:
    """引用语境：cite 命中处前后各 2 句（跨行折叠空白）。"""
    window = text[max(0, start - 500) : min(len(text), end + 500)]
    window = re.sub(r"\s+", " ", window).strip()
    marker = re.sub(r"\s+", " ", text[start:end]).strip()
    pos = window.find(marker)
    if pos == -1:
        return window[:300]
    before = _SENTENCE_SPLIT_RE.split(window[:pos])
    after = _SENTENCE_SPLIT_RE.split(window[pos + len(marker) :])
    return (
        " ".join(before[-2:]).strip() + " " + marker + " " + " ".join(after[:2]).strip()
    ).strip()[:600]


def extract_citations(files: list[tuple[str, str]]) -> list[dict[str, str]]:
    """全部 \\cite 解析 → 去重 bibkey 列表（保留首次出现的位置与语境）。"""
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for path, content in files:
        for match in CITE_RE.finditer(content):
            for key in match.group(1).split(","):
                key = key.strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                line = content.count("\n", 0, match.start()) + 1
                items.append(
                    {
                        "bibkey": key,
                        "location": f"{path}:{line}",
                        "context_snippet": _context_snippet(content, match.start(), match.end()),
                    }
                )
    return items


# ---- 引用存在性：库内精确 → S2/OpenAlex 模糊三态 ----


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def title_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio()


def _year_ok(expected: Any, actual: Any) -> bool:
    if not isinstance(expected, int) or not isinstance(actual, int):
        return True  # 缺年份不作为否证依据
    return abs(expected - actual) <= YEAR_TOLERANCE


def classify_fuzzy_hits(
    title: str, year: Any, hits: list[dict[str, Any]]
) -> tuple[str, str | None]:
    """模糊命中三态：(existence, matched_title)。

    相似度 ≥ 0.92 且年份容差 1 内 → exact；相似度 ≥ 0.75（或标题吻合但年份超差）
    → minor；否则 fabricated。
    """
    best_sim, best_title, best_year = 0.0, None, None
    for hit in hits:
        hit_title = str(hit.get("title") or "").strip()
        if not hit_title:
            continue
        sim = title_similarity(title, hit_title)
        if sim > best_sim:
            best_sim, best_title, best_year = sim, hit_title, hit.get("year")
    if best_title is None or best_sim < MINOR_SIMILARITY:
        return "fabricated", None
    if best_sim >= EXACT_SIMILARITY and _year_ok(year, best_year):
        return "exact", best_title
    return "minor", best_title


async def match_citation_remote(
    entry: dict[str, Any],
    *,
    s2: SemanticScholarClient,
    openalex: OpenAlexClient,
) -> tuple[str, str | None, str]:
    """非库内条目远程核验：(existence, matched_title, source)。

    S2 title 检索优先，未命中/不可达降级 OpenAlex；两边都不可达时保守判
    minor（网络故障不诬告 fabricated），source=none。
    """
    title = str(entry.get("title") or "")
    year = entry.get("year")
    if not title:
        return "fabricated", None, "none"
    s2_failed = False
    try:
        hits = await s2.search_papers(title, limit=5)
    except Exception:  # noqa: BLE001 — 外部 API 失败降级 OpenAlex
        hits, s2_failed = [], True
    if hits:
        existence, matched = classify_fuzzy_hits(title, year, hits)
        if existence != "fabricated":
            return existence, matched, "s2"
    try:
        oa_hits = await openalex.search_works(title, limit=5)
    except Exception:  # noqa: BLE001 — OpenAlex 也不可达
        oa_hits = None
    if oa_hits:
        existence, matched = classify_fuzzy_hits(title, year, oa_hits)
        if existence != "fabricated":
            return existence, matched, "openalex"
    if s2_failed and not oa_hits:
        return "minor", None, "none"  # 双端不可达：保守降级，不判 fabricated
    return "fabricated", None, "none"


async def check_citation_existence(
    session: AsyncSession,
    cited: list[dict[str, str]],
    fact_citations: list[dict[str, Any]],
    *,
    s2: SemanticScholarClient,
    openalex: OpenAlexClient,
) -> list[dict[str, Any]]:
    """存在性核验（不含支撑性，support 由 LLM 步骤补写）。"""
    by_key = {str(c.get("bibkey")): c for c in fact_citations if c.get("bibkey")}
    items: list[dict[str, Any]] = []
    for cite in cited:
        entry = by_key.get(cite["bibkey"])
        item: dict[str, Any] = {
            "bibkey": cite["bibkey"],
            "existence": "fabricated",
            "matched_title": None,
            "source": "none",
            "support": "not_checked",
            "context_snippet": cite["context_snippet"],
            "location": cite["location"],
        }
        if entry is not None:
            paper = None
            raw_pid = entry.get("paper_id")
            if raw_pid:
                try:
                    paper = await session.get(Paper, uuid.UUID(str(raw_pid)))
                except ValueError:
                    paper = None
            if paper is not None:  # 库内精确匹配
                item |= {"existence": "exact", "matched_title": paper.title, "source": "library"}
            else:  # bib 条目在但非库内（如 S2 追加）→ 远程模糊核验
                existence, matched, source = await match_citation_remote(
                    entry, s2=s2, openalex=openalex
                )
                item |= {"existence": existence, "matched_title": matched, "source": source}
        items.append(item)
    return items


def relevant_excerpt(full_text: str, query: str, max_chars: int = 1200) -> str:
    """全文中与语境词重叠最高的段落（支撑性判定的「相关段」，确定性）。"""
    query_tokens = set(re.findall(r"\w+", query.lower()))
    best, best_score = "", -1.0
    for para in re.split(r"\n\s*\n", full_text):
        para = para.strip()
        if len(para) < 40:
            continue
        tokens = set(re.findall(r"\w+", para.lower()))
        score = len(query_tokens & tokens) / (len(tokens) or 1)
        if score > best_score:
            best, best_score = para, score
    return (best or full_text.strip())[:max_chars]


# ---- 事实查错（确定性部分） ----


def _allowed_metric_values(fact_pack: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for metric in fact_pack.get("metrics") or []:
        for run in metric.get("runs") or []:
            if isinstance(run.get("value"), int | float):
                values.append(float(run["value"]))
        if isinstance(metric.get("best"), int | float):
            values.append(float(metric["best"]))
    return values


def _number_ok(token: str, is_percent: bool, allowed: list[float], context: str) -> bool:
    """白名单启发式（年份/小于 10 的整数/章节引用）或 metrics ±0.01 命中。"""
    value = float(token)
    is_integer = "." not in token
    if is_integer and value < 10:
        return True
    if is_integer and not is_percent and 1900 <= value <= 2099:
        return True
    if _REF_CONTEXT_RE.search(context):
        return True
    candidates = (value, value / 100.0) if is_percent else (value,)
    return any(abs(c - v) <= NUMBER_TOLERANCE for c in candidates for v in allowed)


def _fact_item(
    location: str, issue: str, evidence: str, kind: str, severity: str
) -> dict[str, str]:
    return {
        "location": location,
        "issue": issue,
        "evidence": evidence,
        "kind": kind,
        "severity": severity,
    }


def scan_fact_issues(
    files: list[tuple[str, str]], fact_pack: dict[str, Any]
) -> list[dict[str, str]]:
    """确定性查错：数字 ↔ metrics 比对、\\ref 悬空、图表存在性。"""
    items: list[dict[str, str]] = []
    allowed = _allowed_metric_values(fact_pack)
    fig_ids = {str(f.get("fig_id")) for f in fact_pack.get("figures") or [] if f.get("fig_id")}
    labels: set[str] = set()
    for _path, content in files:
        labels |= {m.group(1).strip() for m in LABEL_RE.finditer(content)}

    for path, content in files:
        for line_no, raw_line in enumerate(content.splitlines(), start=1):
            line = strip_tex_comments(raw_line)
            location = f"{path}:{line_no}"

            # 图表存在性：\includegraphics 只准引 fact-pack figures 的 fig_id
            for match in GRAPHICS_RE.finditer(line):
                target = match.group(1).strip()
                stem = target.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                if stem not in fig_ids:
                    items.append(
                        _fact_item(
                            location,
                            f"图表 {target} 不在事实包 figures 中",
                            raw_line.strip()[:200],
                            "missing_figure",
                            "major",
                        )
                    )

            # \ref 悬空：无对应 \label
            for match in REF_RE.finditer(line):
                for key in match.group(1).split(","):
                    key = key.strip()
                    if key and key not in labels:
                        items.append(
                            _fact_item(
                                location,
                                f"\\ref{{{key}}} 无对应 \\label，交叉引用悬空",
                                raw_line.strip()[:200],
                                "other",
                                "minor",
                            )
                        )

            # 数字比对：先剥离 cite/includegraphics 再扫描（bibkey 年份、宽度参数不误报）
            stripped = CITE_RE.sub(" ", line)
            stripped = GRAPHICS_RE.sub(" ", stripped)
            checked_spans: list[tuple[int, int]] = []
            for match in _PERCENT_NUM_RE.finditer(stripped):
                checked_spans.append(match.span(1))
                token = match.group(1)
                context = stripped[max(0, match.start() - 24) : match.start()]
                if not _number_ok(token, True, allowed, context):
                    items.append(
                        _fact_item(
                            location,
                            f"数字 {token}% 未命中事实包 metrics（±0.01）",
                            raw_line.strip()[:200],
                            "number_mismatch",
                            "major",
                        )
                    )
            for match in _DECIMAL_RE.finditer(stripped):
                if any(s <= match.start() < e for s, e in checked_spans):
                    continue  # 已按百分数校验
                token = match.group(0)
                context = stripped[max(0, match.start() - 24) : match.start()]
                if not _number_ok(token, False, allowed, context):
                    items.append(
                        _fact_item(
                            location,
                            f"数字 {token} 未命中事实包 metrics（±0.01）",
                            raw_line.strip()[:200],
                            "number_mismatch",
                            "major",
                        )
                    )
    return items


# ---- 评审员 JSON 校验 / 聚合（纯函数，docs/api-m5-c.md §3） ----

_SCORE_FIELDS = (("soundness", 1, 4), ("presentation", 1, 4), ("contribution", 1, 4))
_LIST_FIELDS = ("strengths", "weaknesses", "questions")


def validate_reviewer_json(data: Any) -> dict[str, Any]:
    """严格校验单个评审员输出；非法抛 ValueError。"""
    if not isinstance(data, dict):
        raise ValueError("reviewer payload is not an object")
    out: dict[str, Any] = {}
    for field, lo, hi in _SCORE_FIELDS:
        value = data.get(field)
        if not isinstance(value, int | float) or not lo <= float(value) <= hi:
            raise ValueError(f"{field} must be a number in [{lo}, {hi}]")
        out[field] = float(value)
    rating = data.get("rating")
    if not isinstance(rating, int | float) or not 1 <= float(rating) <= 10:
        raise ValueError("rating must be a number in [1, 10]")
    out["rating"] = float(rating)
    confidence = data.get("confidence")
    if not isinstance(confidence, int | float) or not 1 <= float(confidence) <= 5:
        raise ValueError("confidence must be a number in [1, 5]")
    out["confidence"] = float(confidence)
    for field in _LIST_FIELDS:
        raw = data.get(field)
        if not isinstance(raw, list):
            raise ValueError(f"{field} must be a list")
        out[field] = [str(x).strip() for x in raw if str(x).strip()]
    if not out["strengths"] and not out["weaknesses"]:
        raise ValueError("strengths/weaknesses 不能同时为空")
    return out


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    total = sum(weights)
    if total <= 0:
        return 0.0
    return sum(v * w for v, w in zip(values, weights, strict=True)) / total


def aggregate_reviews(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合（§3）：rating 取中位数为基准；|rating−中位| > 3 降权 0.5、
    confidence ≤ 2 降权 0.5（两规则叠乘）；unreliable 不计入。
    四维分数为降权加权平均（保留 2 位）。"""
    reliable = [r for r in reviews if not r.get("unreliable")]
    if not reliable:
        return {
            "soundness": 0.0,
            "presentation": 0.0,
            "contribution": 0.0,
            "rating": 0.0,
            "aggregation": {
                "ratings": [],
                "weights": [],
                "median": None,
                "method": "median-outlier-suppressed",
            },
        }
    ratings = [float(r["rating"]) for r in reliable]
    median = float(statistics.median(ratings))
    weights: list[float] = []
    for r in reliable:
        weight = 1.0
        if abs(float(r["rating"]) - median) > 3:
            weight *= 0.5
        if float(r.get("confidence") or 5) <= 2:
            weight *= 0.5
        weights.append(weight)
    result: dict[str, Any] = {
        dim: round(_weighted_mean([float(r[dim]) for r in reliable], weights), 2)
        for dim, _lo, _hi in _SCORE_FIELDS
    }
    result["rating"] = round(_weighted_mean(ratings, weights), 2)
    result["aggregation"] = {
        "ratings": ratings,
        "weights": weights,
        "median": median,
        "method": "median-outlier-suppressed",
    }
    return result


def decision_hint(rating: float, *, has_fabricated: bool, has_reliable: bool) -> str:
    """accept | borderline | reject；fabricated 或无可信评审员强制不 accept。"""
    if has_fabricated or not has_reliable:
        return "reject"
    if rating >= PASS_RATING:
        return "accept"
    if rating >= PASS_RATING - 1:
        return "borderline"
    return "reject"


def review_passed(meta: dict[str, Any], citation_check: dict[str, Any]) -> bool:
    """通过判定（§4）：meta.rating ≥ 6 且无 fabricated 引用。"""
    has_fabricated = any(
        i.get("existence") == "fabricated" for i in citation_check.get("items") or []
    )
    return float(meta.get("rating") or 0) >= PASS_RATING and not has_fabricated


def build_revision_notes(
    reviews: list[dict[str, Any]],
    fact_items: list[dict[str, Any]],
    citation_items: list[dict[str, Any]],
) -> str:
    """weaknesses + 查错表 + 可疑引用 → 修订说明 markdown（写 fact_pack.revision_notes）。"""
    lines = ["# 评审修订说明", ""]
    weaknesses = [
        (r.get("persona") or "评审员", w)
        for r in reviews
        if not r.get("unreliable")
        for w in r.get("weaknesses") or []
    ]
    if weaknesses:
        lines.append("## 评审员指出的不足")
        lines += [f"- （{persona}）{w}" for persona, w in weaknesses]
        lines.append("")
    if fact_items:
        lines.append("## 事实查错清单")
        lines += [
            f"- [{i.get('severity')}] {i.get('location')}：{i.get('issue')}" for i in fact_items
        ]
        lines.append("")
    suspicious = [i for i in citation_items if i.get("existence") != "exact"]
    if suspicious:
        lines.append("## 需要复核的引用")
        lines += [
            f"- \\cite{{{i.get('bibkey')}}}：existence={i.get('existence')}，"
            f"support={i.get('support')}"
            for i in suspicious
        ]
        lines.append("")
    lines.append(f"（由论文评审自动生成于 {datetime.now(UTC).isoformat()}）")
    return "\n".join(lines)


# ---- 评审历史（§4） ----


async def list_manuscript_reviews(
    session: AsyncSession, manuscript_id: uuid.UUID
) -> list[tuple[ReviewSession, int]]:
    """该稿件全部评审会话（新→旧）+ 消息数。"""
    stmt = (
        select(ReviewSession, func.count(ReviewMessage.id))
        .outerjoin(ReviewMessage, ReviewMessage.session_id == ReviewSession.id)
        .where(
            ReviewSession.target_type == "manuscript",
            ReviewSession.target_id == manuscript_id,
        )
        .group_by(ReviewSession.id)
        .order_by(ReviewSession.created_at.desc())
    )
    return [(row[0], int(row[1])) for row in (await session.execute(stmt)).all()]
