"""paper_writing voyage 动作（kind ``paper_writing`` 固定管线执行体，docs/api-m5-b.md §5）。

流水线（navigator.writing_plan）：
    writing.section × N（固定顺序 Intro→Method→Experimental Setup→Results→
    Conclusion→Abstract）→ writing.compile(mid) → writing.related_work →
    writing.compile(final，编译 ok 才算完成)

约定：
- 每节 LLM 产出经三类静态校验（\\cite ∈ fact_pack.citations、\\includegraphics 只准
  引 fact_pack.figures 的 fig_id、正文数字须命中 metrics ±0.01 或白名单启发式豁免），
  违规回 LLM 重写 ≤2 次；仍违规不判整单失败——降级写入最后一稿并加 TODO 注释
  （needs_review），记 Activity 提醒人工核对，继续写后续节；
- 每节一轮 self-reflection 精修（精修稿再过校验，不过则保留原稿）；
- **流式直播**：撰写时把 LLM 增量经 redis ``crdt:stream`` 发布，API 进程订阅后
  写进活跃房间，编辑器实时看到 AI「打字」；相位（typing/revising/done/compiling）
  经 ``manuscript.ai_writing`` 通知前端画 AI 光标（worker 与 API 独立容器，故经
  redis 跨进程，见 services/crdt_stream.py）；
- 权威落库经 crdt_rooms.apply_ai_edit：worker 侧无活跃房间直接写库（+ 版本快照），
  房间的防抖快照会把并发人工编辑合并落库；
- Related Work 延后：候选集 = fact_pack.citations 全集 + S2 title 检索 top10，
  只准从候选集内选引；被引用的 S2 命中追加进 fact_pack.citations（source=s2，
  编译时生成 @misc 条目）；S2 不可达时降级为仅库内候选；
- Manuscript.status 联动 draft→writing→compiled，流转发 WS ``manuscript.status``；
  动作失败把（未编译成功的）稿件回退 draft。
"""

import asyncio
import functools
import json
import re
import uuid
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.actions import ActionContext, register
from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.models.activity import Activity
from app.models.manuscript import Manuscript, ManuscriptFile
from app.services import latex_compile
from app.services.citations import citation_key_for
from app.services.crdt_rooms import get_crdt_rooms
from app.services.literature.semantic_scholar import SemanticScholarClient

MAX_SECTION_REWRITES = 2  # 静态校验违规重写次数上限
S2_RELATED_LIMIT = 10  # Related Work 的 S2 title 检索 top-N
NUMBER_TOLERANCE = 0.01 + 1e-9  # 数字命中 metrics 的容差
_FACT_PACK_CHARS = 8000
_SKELETON_CHARS = 3000

SECTION_TITLES = {
    "abstract": "Abstract",
    "introduction": "Introduction",
    "related_work": "Related Work",
    "method": "Method",
    "experimental_setup": "Experimental Setup",
    "results": "Results",
    "conclusion": "Conclusion",
}

# 模板骨架已在各 \section 后预置这些标签（分节独立起草时跨节 \ref 的唯一合法目标）
STANDARD_SECTION_LABELS = {
    "sec:introduction",
    "sec:related_work",
    "sec:method",
    "sec:experimental_setup",
    "sec:results",
    "sec:conclusion",
}

# fake provider 识别标记：POLARIS_WRITING_SECTION / POLARIS_RELATED_WORK /
# POLARIS_WRITING_REFLECT（core/llm/fake.py 对齐）
SECTION_SYSTEM_PROMPT = """\
你是 Polaris 的论文撰写工程师（POLARIS_WRITING_SECTION），为指定小节撰写正式的
英文 LaTeX 正文。直接输出该小节的 LaTeX 内容（不含 \\section 标题行、不含
POLARIS_SECTION 标记、不要 Markdown 代码块）。
硬约束（违反会被拒绝并要求重写）：
- \\cite{key} 只准使用事实包 citations 列表中的 bibkey；
- \\includegraphics 只准引用事实包 figures 列表中的 fig_id，
  路径写 figures/<fig_id>.pdf；
- 正文中的百分数与小数必须来自事实包 metrics 的数值（不得编造数字）；
  年份、章节/图表编号等小整数不受限；
- 交叉引用 \\ref/\\autoref 只准指向标准章节标签（sec:introduction、sec:related_work、
  sec:method、sec:experimental_setup、sec:results、sec:conclusion）或本节内
  自己 \\label 定义的标签；
- 事实包中没有的结论不要写。
"""

RELATED_WORK_SYSTEM_PROMPT = """\
你是 Polaris 的论文撰写工程师（POLARIS_RELATED_WORK），撰写 Related Work 小节的
英文 LaTeX 正文。直接输出小节内容（不含 \\section 标题行、不要 Markdown 代码块）。
硬约束（违反会被拒绝并要求重写）：
- \\cite{key} 只准使用下方候选文献列表中的 bibkey，不得引用列表之外的任何文献；
- 不要使用 \\includegraphics；不要编造数字。
"""

REFLECT_SYSTEM_PROMPT = """\
你是 Polaris 的论文润色编辑（POLARIS_WRITING_REFLECT），对给定小节做一轮精修：
改善行文与逻辑衔接，不改变事实、引用与数字。直接输出精修后的完整 LaTeX 小节内容
（不要 Markdown 代码块）。硬约束与撰写时相同（引用/图表/数字不得越界）。
"""

_CITE_RE = re.compile(r"\\cite[tp]?\*?(?:\[[^\]]*\])?(?:\[[^\]]*\])?\{([^{}]*)\}")
_GRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^{}]*)\}")
_PERCENT_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)(?:\\%|%)")
_DECIMAL_RE = re.compile(r"\d+\.\d+")
_REF_RE = re.compile(r"\\(?:auto|c|C)?ref\*?\{([^{}]*)\}")
_LABEL_RE = re.compile(r"\\label\{([^{}]*)\}")
# 章节/图表编号引用启发式：Section 3 / Table 2 / Figure 1 / Appendix A.1 等
_REF_CONTEXT_RE = re.compile(
    r"(?:Section|Sections|Table|Tables|Figure|Figures|Fig\.|Chapter|Appendix|Eq\.|"
    r"Equation|§)\s*~?\s*$",
    re.IGNORECASE,
)


# ---- 公共小件 ----


def _params(ctx: ActionContext) -> dict[str, Any]:
    params = (ctx.checkpoint or {}).get("params")
    return params if isinstance(params, dict) else {}


def _manuscript_id(ctx: ActionContext) -> uuid.UUID:
    raw = _params(ctx).get("manuscript_id")
    if not raw:
        raise ValueError("paper_writing voyage 缺少 checkpoint.params.manuscript_id")
    return uuid.UUID(str(raw))


async def _get_manuscript(session: AsyncSession, ctx: ActionContext) -> Manuscript:
    manuscript = await session.get(Manuscript, _manuscript_id(ctx))
    if manuscript is None:
        raise ValueError(f"manuscript not found: {_manuscript_id(ctx)}")
    return manuscript


async def _set_status(
    ctx: ActionContext, session: AsyncSession, manuscript: Manuscript, status: str
) -> None:
    if manuscript.status == status:
        return
    manuscript.status = status
    await session.commit()
    await ctx.notify(
        {"type": "manuscript.status", "manuscript_id": str(manuscript.id), "status": status}
    )


async def _mark_failed(ctx: ActionContext, reason: str) -> None:
    """异常路径：未编译成功的稿件回退 draft + Activity。"""
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, _manuscript_id(ctx))
        if manuscript is None or manuscript.status != "writing":
            return
        session.add(
            Activity(
                project_id=manuscript.project_id,
                actor="agent:writing",
                kind="manuscript.draft_failed",
                message=f"AI 起草失败：{reason[:300]}",
                payload={"manuscript_id": str(manuscript.id), "reason": reason[:1000]},
            )
        )
        await _set_status(ctx, session, manuscript, "draft")


def _guarded(func):
    """动作异常时先回退稿件状态再抛给 helm（helm 记 observation.error）。"""

    @functools.wraps(func)
    async def wrapper(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await func(ctx, params)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await _mark_failed(ctx, f"{type(e).__name__}: {e}")
            raise

    return wrapper


async def _section_file(
    session: AsyncSession, manuscript: Manuscript, section: str
) -> ManuscriptFile:
    """含该节标记的文件（通常 main.tex）；找不到标记时回退 main.tex。"""
    stmt = select(ManuscriptFile).where(
        ManuscriptFile.manuscript_id == manuscript.id,
        ManuscriptFile.readonly.is_(False),
    )
    files = (await session.execute(stmt)).scalars().all()
    marker = f"% POLARIS_SECTION: {section}"
    for file in files:
        if marker in file.content:
            return file
    # 回退到稿件的编译入口主文件（官方模板可能不叫 main.tex）
    main_path = manuscript.main_tex or latex_compile.MAIN_TEX
    main = next((f for f in files if f.path == main_path), None)
    if main is None:
        raise ValueError(f"稿件缺少主文件 {main_path}，无法写入 {section} 节")
    return main


# ---- 静态校验（docs/api-m5-b.md §5 三类） ----


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
        return True  # 小整数：章节号 / 列表计数等
    if is_integer and not is_percent and 1900 <= value <= 2099:
        return True  # 年份
    if _REF_CONTEXT_RE.search(context):
        return True  # Section 12 / Table 10 等编号引用
    candidates = (value, value / 100.0) if is_percent else (value,)
    return any(abs(c - v) <= NUMBER_TOLERANCE for c in candidates for v in allowed)


def validate_section_text(text: str, fact_pack: dict[str, Any]) -> list[str]:
    """三类静态校验，返回违规说明列表（空列表 = 通过）。"""
    violations: list[str] = []

    allowed_keys = {
        str(c.get("bibkey")) for c in fact_pack.get("citations") or [] if c.get("bibkey")
    }
    for match in _CITE_RE.finditer(text):
        for key in match.group(1).split(","):
            key = key.strip()
            if key and key not in allowed_keys:
                violations.append(f"非法引用 \\cite{{{key}}}：不在事实包 citations 中")

    fig_ids = {str(f.get("fig_id")) for f in fact_pack.get("figures") or [] if f.get("fig_id")}
    for match in _GRAPHICS_RE.finditer(text):
        stem = PurePosixPath(match.group(1).strip()).stem
        if stem not in fig_ids:
            violations.append(
                f"非法图表 \\includegraphics{{{match.group(1).strip()}}}：不在事实包 figures 中"
            )

    # 交叉引用：分节独立起草，跨节 \ref 只准指向模板预置的标准章节标签或本节自定义标签
    own_labels = {m.group(1).strip() for m in _LABEL_RE.finditer(text)}
    for match in _REF_RE.finditer(text):
        for target in match.group(1).split(","):
            target = target.strip()
            if target and target not in STANDARD_SECTION_LABELS and target not in own_labels:
                violations.append(
                    f"非法交叉引用 \\ref{{{target}}}：目标不在标准章节标签或本节 \\label 中"
                )

    # 数字：先剥离 cite/includegraphics/注释再扫描（避免 bibkey 年份、宽度参数误报）；
    # 紧跟数字的 % 视为百分号而非注释起始（契约的 \d+\.?\d*% 形态）
    stripped = _CITE_RE.sub(" ", text)
    stripped = _GRAPHICS_RE.sub(" ", stripped)
    stripped = re.sub(r"(?<![\d\\])%.*$", " ", stripped, flags=re.MULTILINE)
    allowed_values = _allowed_metric_values(fact_pack)
    for match in _PERCENT_NUM_RE.finditer(stripped):
        token = match.group(1)
        context = stripped[max(0, match.start() - 24) : match.start()]
        if not _number_ok(token, True, allowed_values, context):
            violations.append(f"数字 {token}% 未命中事实包 metrics（±0.01）")
    for match in _DECIMAL_RE.finditer(stripped):
        token = match.group(0)
        # 跳过已按百分数校验过的数字
        tail = stripped[match.end() : match.end() + 2]
        if tail.startswith(("%", "\\%")):
            continue
        context = stripped[max(0, match.start() - 24) : match.start()]
        if not _number_ok(token, False, allowed_values, context):
            violations.append(f"数字 {token} 未命中事实包 metrics（±0.01）")
    return violations


# ---- LLM 撰写（含重写与 reflection） ----


async def _complete_text(ctx: ActionContext, *, system: str, user: str) -> str:
    result = await ctx.llm.complete(
        "writing",
        [Message(role="system", content=system), Message(role="user", content=user)],
        user_id=ctx.run.created_by,
        project_id=ctx.run.project_id,
        voyage_id=ctx.run.id,
    )
    return result.content.strip()


# 流式镜像：攒够约 50 个字符（约一句话）推一次增量，兼顾流畅与消息量
_STREAM_FLUSH_CHARS = 50


async def _ai_stream(
    ctx: ActionContext, *, file_id: uuid.UUID, section: str, op: str, text: str = ""
) -> None:
    """把分节流式命令发布到 redis（API 进程订阅后写活跃房间）；无 bus 时静默。"""
    if ctx.bus is not None:
        await ctx.bus.publish_crdt_stream(
            {"op": op, "file_id": str(file_id), "section": section, "text": text}
        )


async def _ai_phase(
    ctx: ActionContext, *, manuscript_id: uuid.UUID, file_id: uuid.UUID, section: str, phase: str
) -> None:
    """把 AI 撰写相位（typing/revising/done/compiling）推给项目成员的通知 WS，
    前端据此画 AI 光标与状态条。"""
    await ctx.notify(
        {
            "type": "manuscript.ai_writing",
            "manuscript_id": str(manuscript_id),
            "file_id": str(file_id),
            "section": section,
            "phase": phase,
        }
    )


async def _stream_completion(ctx: ActionContext, *, system: str, user: str, sink) -> str:
    """流式补全：逐块交给 sink（async callable，用于发布 delta），返回完整文本。"""
    parts: list[str] = []
    pending: list[str] = []
    pending_len = 0
    async for chunk in ctx.llm.stream(
        "writing",
        [Message(role="system", content=system), Message(role="user", content=user)],
        user_id=ctx.run.created_by,
        project_id=ctx.run.project_id,
        voyage_id=ctx.run.id,
    ):
        if not chunk:
            continue
        parts.append(chunk)
        pending.append(chunk)
        pending_len += len(chunk)
        if pending_len >= _STREAM_FLUSH_CHARS:
            await sink("".join(pending))
            pending = []
            pending_len = 0
    if pending:
        await sink("".join(pending))
    return "".join(parts).strip()


async def _draft_streaming(
    ctx: ActionContext,
    *,
    manuscript_id: uuid.UUID,
    file_id: uuid.UUID,
    section: str,
    system: str,
    user: str,
    fact_pack: dict[str, Any],
) -> tuple[str, int, list[str]]:
    """流式撰写 + 静态校验：逐块镜像到活跃房间（编辑器实时可见 AI 打字），
    校验违规则重写（对观看者呈现为「AI 正在改写」）。返回 (文本, 重写次数, 未解决违规)。

    连续重写仍未通过时不抛错整单失败：返回最后一稿与违规清单，调用方降级处理
    （加 TODO 标注、记 needs_review、继续写后续节）。
    """
    prompt = user
    text = ""
    violations: list[str] = []
    for attempt in range(1 + MAX_SECTION_REWRITES):
        # 清空占位/上一稿，随后流式追加（重写时呈现为 revising）
        await _ai_stream(ctx, file_id=file_id, section=section, op="open")
        await _ai_phase(
            ctx,
            manuscript_id=manuscript_id,
            file_id=file_id,
            section=section,
            phase="typing" if attempt == 0 else "revising",
        )

        async def _sink(delta: str) -> None:
            await _ai_stream(ctx, file_id=file_id, section=section, op="delta", text=delta)

        raw = await _stream_completion(ctx, system=system, user=prompt, sink=_sink)
        text = _strip_code_fence(raw)
        # 用去掉代码围栏后的干净文本对齐房间（流式期可能混入 ``` 围栏）
        await _ai_stream(ctx, file_id=file_id, section=section, op="replace", text=text)
        violations = validate_section_text(text, fact_pack)
        if not violations:
            return text, attempt, []
        prompt = (
            user
            + "\n\n上一稿静态校验未通过，请修复以下问题后重写（保持其余内容）：\n- "
            + "\n- ".join(violations)
            + f"\n\n上一稿：\n{text}"
        )
    return text, MAX_SECTION_REWRITES, violations


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _degraded_text(text: str, violations: list[str]) -> str:
    """校验未通过的降级稿：顶部加 TODO 注释标出问题，供人工核对。"""
    header = ["% TODO(AI 起草)：本节自动校验未通过，请人工核对下列问题后删除本注释："]
    header += [f"% - {v}" for v in violations]
    return "\n".join(header) + "\n" + text


async def _record_needs_review(ctx: ActionContext, *, section: str, violations: list[str]) -> None:
    """降级写入后记 Activity，让「最近动态」里能看到哪节需要人工核对。"""
    async with get_sessionmaker()() as session:
        manuscript = await _get_manuscript(session, ctx)
        session.add(
            Activity(
                project_id=manuscript.project_id,
                actor="agent:writing",
                kind="manuscript.section_needs_review",
                message=f"AI 起草「{SECTION_TITLES.get(section, section)}」未通过自动校验，"
                "已写入草稿并加 TODO 标注，需人工核对",
                payload={
                    "manuscript_id": str(manuscript.id),
                    "section": section,
                    "violations": violations[:20],
                },
            )
        )
        await session.commit()


async def _reflect(
    ctx: ActionContext, *, text: str, fact_pack: dict[str, Any], section_title: str
) -> tuple[str, bool]:
    """一轮 self-reflection 精修；精修稿再过校验，不过则保留原稿。"""
    pack_json = json.dumps(fact_pack, ensure_ascii=False)[:_FACT_PACK_CHARS]
    user = (
        f"小节：{section_title}\n"
        f"事实包（引用/图表/数字约束）：{pack_json}\n"
        f"待精修文本：\n<<<SECTION\n{text}\nSECTION>>>"
    )
    refined = _strip_code_fence(await _complete_text(ctx, system=REFLECT_SYSTEM_PROMPT, user=user))
    if refined and not validate_section_text(refined, fact_pack):
        return refined, True
    return text, False


def _fact_pack_of(manuscript: Manuscript) -> dict[str, Any]:
    fact_pack = manuscript.fact_pack
    if not isinstance(fact_pack, dict):
        raise ValueError("稿件缺少 fact_pack（请先刷新事实包）")
    return fact_pack


# ---- 1. 分节撰写 ----


@register("writing.section")
@_guarded
async def writing_section(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    section = str(params.get("section") or "")
    if section not in SECTION_TITLES:
        raise ValueError(f"未知小节：{section!r}")
    async with get_sessionmaker()() as session:
        manuscript = await _get_manuscript(session, ctx)
        await _set_status(ctx, session, manuscript, "writing")
        fact_pack = _fact_pack_of(manuscript)
        file = await _section_file(session, manuscript, section)
        file_id, skeleton = file.id, file.content

    notes = _params(ctx).get("notes")
    user = (
        f"论文标题：{manuscript.title}\n"
        f"撰写小节：{SECTION_TITLES[section]}（{section}）\n"
        f"事实包 fact-pack（唯一事实来源）：\n"
        f"{json.dumps(fact_pack, ensure_ascii=False)[:_FACT_PACK_CHARS]}\n"
        f"备注：{notes or '（无）'}\n"
        f"当前文档骨架（节选）：\n{skeleton[:_SKELETON_CHARS]}"
    )
    text, rewrites, violations = await _draft_streaming(
        ctx,
        manuscript_id=manuscript.id,
        file_id=file_id,
        section=section,
        system=SECTION_SYSTEM_PROMPT
        + ctx.skill_guidance("writing.section", f"writing.section({section})"),
        user=user,
        fact_pack=fact_pack,
    )
    reflected = False
    if violations:
        text = _degraded_text(text, violations)
        # 降级稿（含 TODO 标注）镜像回房间，让观看者看到最终落稿
        await _ai_stream(ctx, file_id=file_id, section=section, op="replace", text=text)
        await _record_needs_review(ctx, section=section, violations=violations)
    else:
        refined, reflected = await _reflect(
            ctx, text=text, fact_pack=fact_pack, section_title=SECTION_TITLES[section]
        )
        if reflected:
            await _ai_phase(
                ctx,
                manuscript_id=manuscript.id,
                file_id=file_id,
                section=section,
                phase="revising",
            )
            await _ai_stream(ctx, file_id=file_id, section=section, op="replace", text=refined)
        text = refined
    await _ai_phase(
        ctx, manuscript_id=manuscript.id, file_id=file_id, section=section, phase="done"
    )

    via_room = await get_crdt_rooms().apply_ai_edit(file_id, section, text)
    return {
        "section": section,
        "chars": len(text),
        "rewrites": rewrites,
        "reflected": reflected,
        "via_room": via_room,
        "needs_review": bool(violations),
        "violations": violations[:20],
    }


# ---- 2. Related Work（延后：候选集内选引） ----


async def _s2_candidates(title: str) -> list[dict[str, Any]]:
    """S2 title 检索 top10 → 候选条目（不可达时返回空，降级为仅库内候选）。"""
    client = SemanticScholarClient()
    try:
        hits = await client.search_papers(title, limit=S2_RELATED_LIMIT)
    except Exception:  # noqa: BLE001 — 外部 API 失败降级，不阻塞写作
        return []
    finally:
        await client.aclose()
    candidates: list[dict[str, Any]] = []
    for hit in hits:
        hit_title = str(hit.get("title") or "").strip()
        if not hit_title:
            continue
        authors = [
            str(a.get("name"))
            for a in hit.get("authors") or []
            if isinstance(a, dict) and a.get("name")
        ]
        candidates.append(
            {
                "title": hit_title,
                "year": hit.get("year"),
                "authors": authors,
                "venue": hit.get("venue"),
                "url": hit.get("url"),
                "source": "s2",
            }
        )
    return candidates


def _dedup_key(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    for i in range(26):
        key = f"{base}{chr(ord('a') + i)}"
        if key not in used:
            return key
    return f"{base}{len(used)}"


def build_related_candidates(
    fact_pack: dict[str, Any], s2_hits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """候选集 = fact_pack.citations 全集 + S2 命中（生成不冲突的 bibkey，去重同名）。"""
    candidates = [dict(c) for c in fact_pack.get("citations") or []]
    used_keys = {str(c.get("bibkey")) for c in candidates}
    known_titles = {str(c.get("title") or "").strip().lower() for c in candidates}
    for hit in s2_hits:
        title_norm = str(hit["title"]).strip().lower()
        if title_norm in known_titles:
            continue
        known_titles.add(title_norm)
        base = citation_key_for(
            title=hit["title"], author_names=hit.get("authors") or [], year=hit.get("year")
        )
        key = _dedup_key(base, used_keys)
        used_keys.add(key)
        candidates.append({"bibkey": key, **hit})
    return candidates


@register("writing.related_work")
@_guarded
async def writing_related_work(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        manuscript = await _get_manuscript(session, ctx)
        await _set_status(ctx, session, manuscript, "writing")
        fact_pack = _fact_pack_of(manuscript)
        file = await _section_file(session, manuscript, "related_work")
        file_id = file.id
        manuscript_id = manuscript.id

    s2_hits = await _s2_candidates(manuscript.title)
    candidates = build_related_candidates(fact_pack, s2_hits)
    # 校验用扩展事实包：citations 换成候选集（figures/metrics 约束不变）
    fact_pack_for_validation = dict(fact_pack) | {"citations": candidates}

    brief = [
        {"bibkey": c["bibkey"], "title": c.get("title"), "year": c.get("year")} for c in candidates
    ]
    user = (
        f"论文标题：{manuscript.title}\n"
        f"撰写小节：Related Work\n"
        f"候选文献（只准从中选 \\cite）：\n{json.dumps(brief, ensure_ascii=False)}\n"
        f"研究背景（事实包节选）：\n"
        f"{json.dumps(fact_pack.get('idea'), ensure_ascii=False)}"
    )
    text, rewrites, violations = await _draft_streaming(
        ctx,
        manuscript_id=manuscript_id,
        file_id=file_id,
        section="related_work",
        system=RELATED_WORK_SYSTEM_PROMPT + ctx.skill_guidance("writing.related_work"),
        user=user,
        fact_pack=fact_pack_for_validation,
    )
    reflected = False
    if violations:
        text = _degraded_text(text, violations)
        await _ai_stream(ctx, file_id=file_id, section="related_work", op="replace", text=text)
        await _record_needs_review(ctx, section="related_work", violations=violations)
    else:
        refined, reflected = await _reflect(
            ctx, text=text, fact_pack=fact_pack_for_validation, section_title="Related Work"
        )
        if reflected:
            await _ai_phase(
                ctx,
                manuscript_id=manuscript_id,
                file_id=file_id,
                section="related_work",
                phase="revising",
            )
            await _ai_stream(
                ctx, file_id=file_id, section="related_work", op="replace", text=refined
            )
        text = refined

    # 被引用的 S2 命中追加进 fact_pack.citations（编译生成 @misc 条目）
    cited = {
        key.strip()
        for match in _CITE_RE.finditer(text)
        for key in match.group(1).split(",")
        if key.strip()
    }
    existing = {str(c.get("bibkey")) for c in fact_pack.get("citations") or []}
    added = [
        c
        for c in candidates
        if c.get("source") == "s2" and c["bibkey"] in cited and c["bibkey"] not in existing
    ]
    async with get_sessionmaker()() as session:
        manuscript = await _get_manuscript(session, ctx)
        if added:
            new_pack = dict(manuscript.fact_pack or {})
            new_pack["citations"] = list(new_pack.get("citations") or []) + added
            manuscript.fact_pack = new_pack
            await session.commit()
            # 新增 S2 引用 → 同步进 references.bib 文件，避免编译时未定义引用
            await latex_compile.sync_references_bib(session, manuscript)

    await _ai_phase(
        ctx,
        manuscript_id=manuscript_id,
        file_id=file_id,
        section="related_work",
        phase="done",
    )
    via_room = await get_crdt_rooms().apply_ai_edit(file_id, "related_work", text)
    return {
        "section": "related_work",
        "chars": len(text),
        "rewrites": rewrites,
        "reflected": reflected,
        "via_room": via_room,
        "candidates": len(candidates),
        "s2_hits": len(s2_hits),
        "s2_cited_added": len(added),
    }


# ---- 3. 编译 ----


@register("writing.compile")
@_guarded
async def writing_compile(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    phase = str(params.get("phase") or "final")
    async with get_sessionmaker()() as session:
        manuscript = await _get_manuscript(session, ctx)
        # 编译期通知前端收起 AI 光标、显示「AI 正在编译」（file/section 留空）
        await ctx.notify(
            {
                "type": "manuscript.ai_writing",
                "manuscript_id": str(manuscript.id),
                "file_id": None,
                "section": None,
                "phase": "compiling",
            }
        )
        result = await latex_compile.compile_manuscript(session, manuscript)
        if result["status"] == "ok":
            # compile_manuscript 已把 writing → compiled；补发状态事件
            await ctx.notify(
                {
                    "type": "manuscript.status",
                    "manuscript_id": str(manuscript.id),
                    "status": manuscript.status,
                }
            )
    errors = [d["message"] for d in result["diagnostics"] if d["severity"] == "error"]
    if phase == "final" and result["status"] != "ok":
        raise RuntimeError(
            f"终编译未通过（status={result['status']}）：{'；'.join(errors[:3]) or '无诊断'}"
        )
    return {
        "phase": phase,
        "status": result["status"],
        "version": result["version"],
        "errors": len(errors),
        "warnings": sum(1 for d in result["diagnostics"] if d["severity"] == "warning"),
    }
