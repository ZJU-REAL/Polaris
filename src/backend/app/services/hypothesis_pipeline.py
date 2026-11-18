"""文献 → 假设四段管线（#648，设计报告 §12，P2 D3；不 import fastapi）。

生成（generate）→ 文献接地（ground）→ 查新（novelty）→ 可行性（feasibility），
供 discovery 引擎（actions_discovery.hypothesis_expand）逐候选调用。全程延续
「确定性 vs 判断性分离」：检索、去重、越界校验、资源信号统计全是确定性代码，
LLM 只负责五件判断性的事——组合灵感成假设、拆子命题、从检索结果里选证据表
立场、判已知/新颖、写风险论证段。

1. **generate**：三路灵感确定性取材——语义近邻（direction 直接检索）、引文图
   远端（命中论文沿引文边走 2 跳、只取非直接邻居：图距离远的实体更可能带来
   跨域组合，MOOSE-Chem「假设 ≈ 背景 + 灵感组合」）、多样窗（未被覆盖论文的
   等距片段窗口，替代随机采样以保证可重放）；#670（P2.5 F6）起再喂三路**预计算
   燃料**——方法类比（同目的异机制的做法，#666）、未连接概念对（Swanson ABC
   候选，#667）、开放缺口（文献自己指出的未解问题，#668）。燃料同样是确定性
   取材，且**任何一路失败都静默降级为空**：燃料是加分项，供不上不该拖垮生成
   （库刚建、还没有抽取产物时三路燃料天然为空，行为与纯检索路完全一致）。
   → ``hyp_generate`` 一次调用组合出 n 个候选 → SequenceMatcher 相似度 >0.85
   折叠去重（Stanford 100+ 研究者盲评：LLM 假设多样性坍缩严重，大量采样后
   去重是必要步骤，§12）。
2. **ground**：``hyp_ground`` 拆子命题（≤6）→ 逐子命题确定性检索 top-12 →
   同环节第二次调用只允许从检索结果里**选择**片段 + 立场（support/refute）
   或判 speculation；后处理校验所选片段 ∈ 该子命题的检索集，越界一律降为
   speculation——引用只能指向真实检索结果，结构上杜绝编造引文（各模型引文
   伪造率 14–95%）。
3. **novelty**：每条子命题换措辞再检索一轮（同一措辞查两遍只会复读接地
   结果），逐条判 known / novel / uncertain。没有文献依据的子命题也要判：
   一个好假设恰恰是「有依据的已知部分 + 文献没覆盖的新部分」，只判有依据的
   那几条，新颖比例就永远接近零——先摘出假设中已知的部分再逐项对照文献，
   co-scientist Reflection 路线。
   接地与查新的检索结果合成一条假设的**证据池**（按片段去重、编号），两项判定
   在**同一次**模型调用里做完，各看各的候选片段（见 ``EvidencePool`` 与
   ``JUDGE_SYSTEM_PROMPT``）；``hyp_novelty`` 环节只剩单独查新时用。
4. **feasibility**：venue/年份分布、库内相关片段密度等可得性信号**确定性**
   统计——资源匹配是查表不是发挥。风险论证段在一轮扩展里由接地判定那次调用顺带
   写（它手里正好有全部证据）；``hyp_feasibility`` 只剩单独算可行性时用。

score = novel 比例 × support 覆盖率。选这个最笨的公式是有意的：两个因子各自
可解释、各自单调（新颖子命题越多越值得做，有文献支持的子命题越多越扎实），
乘积天然惩罚偏科——全新颖但无接地是空中楼阁，全接地但不新颖是重复劳动。
加权、校准、两两对比这些精细化留给 D4 锦标赛，先让分数「能解释给人听」。

一轮扩展的若干候选由 ``evaluate_candidates`` 并发走完上面 2–4 步（限流）；同轮假设
共用的证据片段做成一段缓存前缀，先由一条判定写进缓存再并发其余的（``_judge_round``）。

模型调用一律走 ``LLMRouter.complete_json``：每个环节一个 Pydantic schema，服务端按
schema 约束输出；失败是带 ``transient`` 的类型化错误（core/llm/errors.py），不再有
「回了一段不是 JSON 的文字就原样重问三遍」。检索是严格的语义检索——向量用不了时
抛 ``RetrievalUnavailableError``，不退回关键词（见 ``_retrieve``）。
"""

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from difflib import SequenceMatcher
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import budgets
from app.core.llm.base import EffortLevel, Message, structured_stream_started
from app.core.llm.errors import LLMError, LLMRefusalError, LLMSchemaError, LLMTruncatedError
from app.core.llm.router import LLMRouter
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper, PaperChunk
from app.models.paper_citation import PaperCitation
from app.services import chunks as chunks_service
from app.services import concept_fuels, gap_ledger, library_rag, method_index
from app.services.papers import PAPER_STATUS_GROUPS

logger = logging.getLogger(__name__)

# 四个 LLM 环节（router.py STAGES / 前端 LLM_STAGES 同步登记）：
# generate/ground 是较长的结构化生成走中档，novelty/feasibility 是短 JSON 判定走短档
GENERATE_STAGE = "hyp_generate"
GROUND_STAGE = "hyp_ground"
NOVELTY_STAGE = "hyp_novelty"
FEASIBILITY_STAGE = "hyp_feasibility"

DEFAULT_CANDIDATES = 6  # generate 默认一次要多少候选（去重前）
# 「给模型看多少」的上限统一登记在 core/llm/budgets.py，这里只取名字
# 子命题上限：多了检索与判定成本线性涨，而假设该是聚焦的
MAX_SUBCLAIMS = budgets.HYPOTHESIS_MAX_SUBCLAIMS
GROUND_TOP_K = budgets.HYPOTHESIS_EVIDENCE_PER_SUBCLAIM  # 每条子命题送给 LLM 挑选的检索结果数
DEDUP_THRESHOLD = 0.85  # 陈述相似度超过它视为同一假设（去重折叠）
INSPIRATION_PER_ROUTE = budgets.HYPOTHESIS_INSPIRATIONS_PER_ROUTE  # 引文远端 / 多样窗各取几条
FUEL_PER_ROUTE = budgets.HYPOTHESIS_FUELS_PER_ROUTE  # 三路燃料各取几条（#670）
SNIPPET_CHARS = budgets.HYPOTHESIS_SNIPPET_CHARS  # 灵感与证据片段的截断长度
DENSITY_LIMIT = 50  # 相关片段密度统计的计数上限（信号要的是量级，不是全量）
# expand 后低于此分自动剪枝（actions_discovery 消费）。
#
# score = 新颖子命题占比 × 有文献支持的子命题占比。有支持的子命题多半会被判成
# 「已知」或「不确定」，新颖的那一两条恰恰落在没有依据的部分，所以真实模型下一个
# 像样的假设也就 0.1–0.25：六条子命题里四条有支持、一条新颖 = 0.111。以前的 0.15
# 是在每个假设最多五条子命题、替身回答的分数上定的，换成真实模型后一轮扩展出的
# 子假设大半被剪（实测 12 篇的库：0.25 / 0.22 / 0.16 留下，0.14 / 0.11 / 0.11 / 0.08
# 被剪，其中 0.14 那条六条子命题有五条带文献支持）。0.10 这条线的含义是：
# 至少有一条新颖的子命题，并且多数子命题有文献支持；一条新颖都没有（0 分）、
# 或者只有一半以下有支持的，照旧剪掉。
MIN_VIABLE_SCORE = 0.10

# 「模型给不出这段判断」的三种失败：结构两次都不对、写不完、拒答。查新与风险论证
# 遇到它们按既有约定降级（见各自的函数）；网络、限流、密钥这类失败不在其中——
# 那不是判断不出来，是这一步没跑成，要原样抛出去让整步重试或交给人处理。
_NO_JUDGEMENT = (LLMSchemaError, LLMTruncatedError, LLMRefusalError)

# ---- 各环节的思考深度（有意逐个定，不跟着超时档位走）----
#
# 生成：一轮只调一次，产出决定后面所有调用值不值——给高档。
GENERATE_EFFORT: EffortLevel = "high"
# 拆子命题：把一句话拆成几条断言，输入输出都很短——低档。
SPLIT_EFFORT: EffortLevel = "low"
# 接地 / 查新判定：对着十几段原文逐条归类，是阅读理解不是开放推理；Anthropic 对
# 「研究与知识工作」的实测是中档与高档准确率持平、低档掉几个点。输出只有几百 token，
# 中档和低档的钱差不出来，所以取中档保判定质量（以前接地是中档、查新是低档）。
JUDGE_EFFORT: EffortLevel = "medium"
# 风险论证：基于已经算好的信号写一小段话——低档。
FEASIBILITY_EFFORT: EffortLevel = "low"

#: 一轮扩展里同时在途的模型调用上限。网关在并发高时会回 503；6 足够让一轮 4 条候选
#: 的每个阶段一波跑完，又不至于把限流打穿。
EVALUATE_CONCURRENCY = budgets.HYPOTHESIS_EVALUATE_CONCURRENCY
#: 可重试失败（限流 / 过载 / 连接中断）的退避秒数；用完仍失败就上抛。
TRANSIENT_RETRY_DELAYS: tuple[float, ...] = (3.0, 10.0, 25.0)


# ---- 各环节的输出 schema ----

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class CandidateOut(BaseModel):
    statement: NonEmptyStr
    rationale: str


class GenerateOut(BaseModel):
    candidates: list[CandidateOut] = Field(min_length=1)


class SubclaimsOut(BaseModel):
    subclaims: list[NonEmptyStr] = Field(min_length=1)


class JudgementItemOut(BaseModel):
    """一条子命题的两项判定：文献接地（stance）与查新（verdict），各带所依据的片段编号。"""

    index: int
    stance: Literal["support", "refute", "speculation"]
    stance_evidence_ids: list[str]
    verdict: Literal["known", "novel", "uncertain"]
    verdict_evidence_ids: list[str]


class JudgementOut(BaseModel):
    items: list[JudgementItemOut]


class GroundJudgementOut(JudgementOut):
    """接地那一次判定：除了逐条判定，再写一段风险论证（以前是每条假设单独一次调用）。"""

    risk_note: NonEmptyStr


class RiskNoteOut(BaseModel):
    risk_note: NonEmptyStr


GENERATE_SYSTEM_PROMPT = """\
POLARIS_HYP_GENERATE
你是研究假设生成器。输入 JSON 里是研究方向和三路文献灵感（semantic=语义近邻、
citation_far=引文图远端、diverse=多样窗口片段）。输入可能还带 fuels 节——从库里
预计算的三类燃料，每条都带来源（paper_id / 概念名）：
- method_analogies：同目的异机制的做法（purpose 相近、mechanism 不同的方法卡）；
- unconnected_pairs：尚未被共同研究的概念组合（经桥概念间接相连的 A、C）；
- open_gaps：文献指出的未解问题（gap/矛盾/负结果的原文归纳）。
请把方向与灵感/燃料**组合**成 n 个彼此不同、可检验的研究假设（鼓励跨片段、
跨燃料组合，而不是逐条改写；有燃料时优先消费燃料——它们是库里最值得下注的线索）。
输入若带 existing 节，那是这次探索里已经提出过的假设（包括正在细化的父假设）：
每个新候选都要和它们、也和彼此有实质区别——换一个机制、对象、条件或检验方式，
不要改写、缩写或拼接已有的陈述。尽量给满 n 个；确实想不出实质不同的再少给。
输出 JSON：{"candidates": [{"statement": "假设陈述（一句话，可检验）", \
"rationale": "一句话依据（引用了哪路灵感或燃料）"}]}"""

GROUND_SPLIT_SYSTEM_PROMPT = f"""\
POLARIS_HYP_GROUND
你是假设分解器。把输入 JSON 里的假设陈述拆成最多 {MAX_SUBCLAIMS} 条可独立检索验证的子命题
（每条一句话，合起来覆盖假设的关键断言）。
输出 JSON：{{"subclaims": ["...", "..."]}}"""

# 接地与查新在一次调用里判完（各看各的候选片段，互不替代）。实测过另一种做法——两次
# 调用共享一个缓存的证据池：查新那次 75% 的输入读自缓存，但证据池只被读一次，写缓存的
# 1.25 倍加价吃掉了大半折扣（合计 1.35 份证据池的钱）；合成一次调用是 1.0 份。
# 风险论证也在这一次里写：以前它是判定之后单独一次调用，一轮扩展要多等二三十秒，
# 写的又只是「哪里证据薄」——判定这次手里正好拿着全部证据。
JUDGE_SYSTEM_PROMPT = """\
POLARIS_HYP_GROUND
你对照文献片段判定研究假设的子命题。输入开头是一段或两段 <evidence>：每段文献片段以
[编号] 和所属论文标题开头，后面是原文。有两段时，第一段是这一轮几条假设共用的片段
（其中有些只属于别的假设，与这条无关），第二段是只属于这条假设的。其后是一个 JSON：
statement（假设陈述）和 items——每条是一个子命题（index、subclaim）以及两组候选片段
编号：ground_candidates（按子命题本身检索到的）和 novelty_candidates（换成「已有研究」
的问法再检索到的）。对 items 里的每一条分别做两项**互相独立**的判定，index 原样回传：

一、文献接地（stance）——只看 ground_candidates 指向的片段：
- support：有片段支持这条子命题，stance_evidence_ids 填这些片段；
- refute：有片段反驳它，stance_evidence_ids 填这些片段；
- speculation：这些片段都不相关或不足以支持/反驳，stance_evidence_ids 留空。

二、查新（verdict）——只看 novelty_candidates 指向的片段，判断这条子命题是否已被覆盖：
- known：已有片段明确提出或验证过这条子命题，verdict_evidence_ids 填这些片段；
- novel：这些片段都没有提出或验证过它，是新的（片段全都与它无关、或只是同一领域的
  相关讨论，都判 novel），verdict_evidence_ids 留空；
- uncertain：有片段与它直接沾边但不足以判定，verdict_evidence_ids 填这些片段。
  novelty_candidates 为空的子命题判 uncertain。

两项判定不要互相推导：有文献支持不等于已知（支持的可能只是它的前提），没有支持也不
等于新颖。编号只能从该条对应的那一组候选里选，**禁止使用别的编号，禁止编造**。

输出要求里有 risk_note 时，再写一段风险论证：用假设陈述的语言，三四句话说清验证这条
假设前最薄弱的环节（哪些关键部分在文献里找不到支持或被反驳、依据是否偏少偏旧）和动手
前要先补什么。这段话直接给研究者看：不要出现片段编号、字段名（stance、verdict、
support、speculation 之类）或「输入」「片段池」这类说法。

输出 JSON：{"items": [{"index": 0, "stance": "...", "stance_evidence_ids": ["c3"], \
"verdict": "...", "verdict_evidence_ids": []}], "risk_note": "..."}"""

FEASIBILITY_SYSTEM_PROMPT = """\
POLARIS_HYP_FEASIBILITY
你是可行性风险论证员。输入 JSON 里是假设陈述和已经确定性统计好的资源信号
（接地文献数、venue/年份分布、库内相关片段密度）。**不要重新估计这些数字**，
只基于它们写一段简短的风险论证（哪里依据偏弱、验证该假设前要补什么）。
这段话直接给研究者看：用假设陈述的语言写，把数字说成自然的话（如「只有 2 篇文献
支持」），不要出现输入里的字段名（signals、grounded_paper_count、related_chunk_hits、
venues 之类），也不要写「信号显示」「signals 显示」这类说法。
输出 JSON：{"risk_note": "..."}"""


# ---- 通用小件 ----


def _merge_usage(total: dict[str, int], usage: dict[str, Any] | None) -> dict[str, int]:
    total["prompt_tokens"] += int((usage or {}).get("prompt_tokens", 0) or 0)
    total["completion_tokens"] += int((usage or {}).get("completion_tokens", 0) or 0)
    return total


async def retry_transient[R](stage: str, call: Callable[[], Awaitable[R]]) -> R:
    """限流、过载、连接中断这类过一会儿可能就好的失败，退避后重试这一次调用。

    并发的十几次调用里有一次撞上网关的 503 或并发上限（429，实测 4 路并发就遇到过：
    上限是按用户算的，别的任务也在占）就让整步重来，等于把已经付过钱的调用全部重付
    一遍，还要多等一轮自动重试。SDK 自己的重试用完之后才到这里；这里也用完就原样抛。
    """
    attempt = 0
    while True:
        try:
            return await call()
        except LLMError as e:
            if not e.transient or attempt >= len(TRANSIENT_RETRY_DELAYS):
                raise
            logger.warning("%s: transient failure (%s), retrying", stage, e.code)
            await asyncio.sleep(TRANSIENT_RETRY_DELAYS[attempt])
            attempt += 1


async def _complete_json[T: BaseModel](
    llm: LLMRouter,
    stage: str,
    *,
    system: str,
    schema: type[T],
    usage: dict[str, int],
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    library_id: uuid.UUID,
    voyage_id: uuid.UUID | None,
    payload: dict[str, Any] | None = None,
    user: str | None = None,
    effort: EffortLevel | None = None,
    cache_prefix: str | None = None,
) -> T:
    """一次结构化调用：system 是环节提示，payload 序列化成 user 消息（或直接给 user）。

    ``effort`` 不给时用环节的默认档。token 并入 ``usage``（修复重问的那一次也在
    ``result.usage`` 里）。``cache_prefix`` 是几次调用共用、要读缓存的那段材料，排在
    user 消息之前（见 ``LLMRouter.complete_json``）。

    可重试的失败在这里退避重试（见 :func:`retry_transient`）；其余失败抛
    ``LLMError``，不在这里吞。
    """
    text = user if user is not None else json.dumps(payload, ensure_ascii=False)
    result = await retry_transient(
        stage,
        lambda: llm.complete_json(
            stage,
            [Message(role="system", content=system), Message(role="user", content=text)],
            schema=schema,
            effort=effort,
            cache_prefix=cache_prefix,
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=voyage_id,
        ),
    )
    _merge_usage(usage, result.usage)
    return result.data


def _ordered_paper_ids(chunks: list[PaperChunk]) -> list[str]:
    """片段列表 → 去重保序的 paper_id 字符串列表（检索留痕用，D6 披露 #655）。"""
    out: list[str] = []
    for chunk in chunks:
        pid = str(chunk.paper_id)
        if pid not in out:
            out.append(pid)
    return out


async def _paper_titles(session: AsyncSession, paper_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not paper_ids:
        return {}
    rows = (
        await session.execute(select(Paper.id, Paper.title).where(Paper.id.in_(paper_ids)))
    ).all()
    return {pid: (title or "") for pid, title in rows}


# ---- 片段：从哪里起、到哪里止 ----
#
# 全文的第一段常常是标题、作者、单位和邮箱，摘要排在后面。语义检索照样会把它捞上来
# （标题里的词与查询很像），给模型看的却全是人名和机构——实测一次探索 1,824 个片段位
# 里有 38 个是这种段落。片段按句子收尾：以前硬截 1,500 字，证据卡上常常停在半个词上。

#: 摘要的起始标记（行首的 Abstract / 摘要）。
_ABSTRACT_RE = re.compile(r"(?im)^[ \t]*(?:abstract|摘[ \t]*要)\b[ \t]*[:：.—-]?[ \t]*")
#: 「这是论文头部」的迹象：邮箱、arXiv 编号、机构名。
_HEADER_RE = re.compile(
    r"@|arXiv:\d|Universit|Institut|Laborator|Department|School of|Research,|\bInc\.|"
    r"大学|研究院|学院|研究所"
)
#: 句末：中文句号类，或英文句号 / 问号 / 叹号后跟空白。
_SENTENCE_END_RE = re.compile(r"[。！？；]|[.!?;](?=\s)")
#: 前置首段剩下的正文少于这么多字时，当它只有头部。
_MIN_BODY_CHARS = 200
#: 片段开头若落在半句话里，往后找句首最多找这么远（找不到就从头给）。
_LEAD_IN_CHARS = 200


def _front_matter_end(chunk: PaperChunk) -> int | None:
    """全文首段的头部（标题、作者、单位）在哪里结束。

    返回正文起点；不是这种段落时返回 0；整段只有头部（没有摘要可留）时返回 None。
    只看 seq 0：后面的段落不会有作者单位。没有头部迹象的首段（只有标题 + 摘要的论文
    就是这样建的段）原样保留。
    """
    if chunk.seq != 0 or not _HEADER_RE.search(chunk.text[:600]):
        return 0
    match = _ABSTRACT_RE.search(chunk.text)
    if match is None or len(chunk.text) - match.end() < _MIN_BODY_CHARS:
        return None
    return match.end()


def snippet_span(chunk: PaperChunk, limit: int = SNIPPET_CHARS) -> tuple[int, int]:
    """给模型和证据卡看的那一段在 ``chunk.text`` 里的 [起, 止)。

    起点跳过首段的头部；后续分段落在半句话里（小写字母开头）时挪到下一句句首。止点在上限内
    最后一个句末之后；上限内一个句末都没有（或在前一半里）才退到最后一个空白、再
    不行硬截。
    """
    text = chunk.text
    start = _front_matter_end(chunk) or 0
    if start == 0 and chunk.seq > 0 and text[:1].islower():
        lead = _SENTENCE_END_RE.search(text, 0, _LEAD_IN_CHARS)
        if lead is not None:
            start = lead.end()
    while start < len(text) and text[start].isspace():
        start += 1
    end = len(text)
    if end - start > limit:
        hard = start + limit
        ends = [m.end() for m in _SENTENCE_END_RE.finditer(text, start, hard)]
        if ends and ends[-1] - start >= limit // 2:
            end = ends[-1]
        else:
            space = text.rfind(" ", start + limit // 2, hard)
            end = space if space != -1 else hard
    return start, end


def _drop_front_matter(chunks: list[PaperChunk], limit: int) -> list[PaperChunk]:
    """去掉只有论文头部的首段（有别的结果可用时），截到 ``limit`` 条。"""
    useful = [c for c in chunks if _front_matter_end(c) is not None]
    return (useful or chunks)[:limit]


def _chunk_entry(chunk: PaperChunk, titles: dict[uuid.UUID, str]) -> dict[str, Any]:
    start, end = snippet_span(chunk)
    return {
        "paper_id": str(chunk.paper_id),
        "title": titles.get(chunk.paper_id, ""),
        "snippet": chunk.text[start:end],
    }


async def _member_paper_ids(session: AsyncSession, library_id: uuid.UUID) -> set[uuid.UUID]:
    """库内成员论文集合（回收站/候选不算）：所有灵感与证据都不许越过库边界。"""
    return set(
        (
            await session.execute(
                select(LibraryPaper.paper_id).where(
                    LibraryPaper.library_id == library_id,
                    LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
                )
            )
        )
        .scalars()
        .all()
    )


async def _retrieve(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    query: str,
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    limit: int,
) -> list[PaperChunk]:
    """确定性检索原语：复用 RAG 的单轮语义检索，截前 limit 条。

    刻意不走 library_rag.answer 的完整流水线——那里面有查询扩展和重排两次 LLM
    判断，接地/查新要的是「同样输入永远同样证据集」的确定性检索。

    ``strict=True``：向量用不了时抛 ``RetrievalUnavailableError``，不退回关键词检索。
    关键词按字面匹配，中文方向对不上英文论文，降级之后每条子命题都检索不到东西、
    整棵树全是「推测」，任务却照样显示完成。宁可这一步明确失败。
    """
    rows = await library_rag.retrieve_round(
        session,
        library_id=library_id,
        query=query,
        user_id=user_id,
        project_id=project_id,
        strict=True,
    )
    return [chunk for chunk, _score in rows[:limit]]


# ---- 1) 生成：三路灵感 → 一次组合 → 去重 ----


async def _citation_far_chunks(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    seed_paper_ids: list[uuid.UUID],
) -> list[PaperChunk]:
    """引文图远端灵感：从命中论文沿引文边走 2 跳，只取**非直接邻居**的库内论文。

    直接邻居（引用/被引）与种子的相关性语义检索多半也够得着；隔一层的论文才是
    「结构上有关联、语义上够得远」的跨域灵感来源。逐跳 BFS 而非递归 CTE：假设树
    同款理由——两跳的小查询，可读性优先。
    """
    if not seed_paper_ids:
        return []
    member_ids = await _member_paper_ids(session, library_id)

    async def neighbors(frontier: list[uuid.UUID]) -> list[uuid.UUID]:
        edges = (
            (
                await session.execute(
                    select(PaperCitation)
                    .where(
                        PaperCitation.citing_paper_id.in_(frontier)
                        | PaperCitation.cited_paper_id.in_(frontier)
                    )
                    .order_by(PaperCitation.citing_paper_id, PaperCitation.ref_index)
                )
            )
            .scalars()
            .all()
        )
        out: list[uuid.UUID] = []
        for edge in edges:
            for pid in (edge.cited_paper_id, edge.citing_paper_id):
                if pid is not None and pid not in frontier and pid not in out:
                    out.append(pid)
        return out

    seeds = list(seed_paper_ids)
    hop1 = [pid for pid in await neighbors(seeds) if pid not in seeds]
    if not hop1:
        return []
    hop2 = [
        pid
        for pid in await neighbors(hop1)
        if pid not in seeds and pid not in hop1 and pid in member_ids
    ][:INSPIRATION_PER_ROUTE]
    if not hop2:
        return []
    # 每篇远端论文取开头片段：远端灵感靠「它讲什么」而非「哪句最像方向」入选
    chunks = (
        (
            await session.execute(
                select(PaperChunk)
                .where(PaperChunk.paper_id.in_(hop2))
                .order_by(PaperChunk.paper_id, PaperChunk.seq)
            )
        )
        .scalars()
        .all()
    )
    first_by_paper: dict[uuid.UUID, PaperChunk] = {}
    for chunk in chunks:
        first_by_paper.setdefault(chunk.paper_id, chunk)
    return [first_by_paper[pid] for pid in hop2 if pid in first_by_paper]


async def _diverse_window_chunks(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    exclude_paper_ids: set[uuid.UUID],
) -> list[PaperChunk]:
    """多样窗灵感：未被前两路覆盖的库内片段，按等距窗口取样。

    「随机多样窗」的随机在这里被有意替换成等距取样：discovery run 的每一步都要
    可重放（断点恢复 / golden 比对），随机源会让同一 run 两次重放长出不同的树。
    等距窗口保留了「打散位置、覆盖不同论文段落」的多样性收益，又是纯确定性的。
    """
    rows = (
        (
            await session.execute(
                select(PaperChunk)
                .join(LibraryPaper, LibraryPaper.paper_id == PaperChunk.paper_id)
                .where(
                    LibraryPaper.library_id == library_id,
                    LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
                )
                .order_by(PaperChunk.paper_id, PaperChunk.seq)
            )
        )
        .scalars()
        .all()
    )
    pool = [c for c in rows if c.paper_id not in exclude_paper_ids] or list(rows)
    if not pool:
        return []
    stride = max(1, len(pool) // INSPIRATION_PER_ROUTE)
    return pool[::stride][:INSPIRATION_PER_ROUTE]


async def _gather_fuels(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    direction: str,
    user_id: uuid.UUID | None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """三路燃料确定性取材（#670）：返回 (prompt 的 fuels 节, trace 的 fuels 节)。

    prompt 节只收**非空**的路——空路进 prompt 只是噪音，且「无任何燃料时 payload
    与纯检索路完全一致」是行为判据（golden 场景靠它保证等价）。trace 节三个键
    恒在（哪怕全空）：披露报告要能如实说「这次没消费任何燃料」。

    每一路独立 try/except **静默降级为空**：燃料服务依赖抽取产物/概念/向量等
    可选设施，任何一路坏了都不该把整个 generate 拖下水——没有燃料的生成是
    降级，不是失败。
    """
    sections: dict[str, list[dict[str, Any]]] = {}
    trace: dict[str, Any] = {"methods": [], "concept_pairs": [], "gaps": []}

    # 燃料 1：方法类比——同目的异机制（#666）。带 purpose/mechanism 原文与出处，
    # LLM 才能据此做「换个机制达成同一目的」的类比组合
    try:
        cards, _mode = await method_index.search_methods(
            session,
            library_id,
            direction,
            mode="different_mechanism",
            limit=FUEL_PER_ROUTE,
            user_id=user_id,
        )
    except Exception:  # noqa: BLE001 — 燃料失败静默降级（见 docstring）
        logger.warning("method analogy fuel unavailable", exc_info=True)
        cards = []
    if cards:
        sections["method_analogies"] = [
            {
                "paper_id": str(c["paper_id"]),
                "title": c["title"],
                "purpose": c["purpose"],
                "mechanism": c["mechanism"],
            }
            for c in cards
        ]
        trace["methods"] = list(dict.fromkeys(str(c["paper_id"]) for c in cards))

    # 燃料 2：未连接概念对（#667）。A、C 与桥概念一起给：桥是「为什么值得把
    # 它们放在一起想」的可解释依据
    try:
        pairs = await concept_fuels.mine_unconnected_pairs(
            session, library_id=library_id, top_n=FUEL_PER_ROUTE
        )
    except Exception:  # noqa: BLE001
        logger.warning("unconnected pair fuel unavailable", exc_info=True)
        pairs = []
    if pairs:
        sections["unconnected_pairs"] = [
            {
                "concept_a": p["concept_a"]["name"],
                "concept_c": p["concept_c"]["name"],
                "bridges": [b["name"] for b in p["bridges"]],
            }
            for p in pairs
        ]
        trace["concept_pairs"] = [
            {"concept_a": p["concept_a"]["name"], "concept_c": p["concept_c"]["name"]}
            for p in pairs
        ]

    # 燃料 3：开放缺口（#668）。kind 不限：gap/contradiction 优先已由台账的
    # 排序权重保证，这里只管取 top
    try:
        gaps = await gap_ledger.library_gaps(session, library_id, top=FUEL_PER_ROUTE)
    except Exception:  # noqa: BLE001
        logger.warning("open gap fuel unavailable", exc_info=True)
        gaps = []
    if gaps:
        sections["open_gaps"] = [
            {"paper_id": str(g.paper_id), "kind": g.kind, "statement": g.statement} for g in gaps
        ]
        trace["gaps"] = list(dict.fromkeys(str(g.paper_id) for g in gaps))

    return sections, trace


def _norm_statement(text: str) -> str:
    return " ".join(str(text).lower().split())


def dedup_candidates(
    candidates: list[dict[str, Any]], *, threshold: float = DEDUP_THRESHOLD
) -> list[dict[str, Any]]:
    """按陈述相似度折叠近重复候选（保留先出现的——LLM 输出顺序即其置信排序）。

    SequenceMatcher 而非嵌入相似度：去重要拦的是「同一句话换个说法」这种表层
    重复（多样性坍缩的典型形态），字符级相似度便宜、确定、够用。
    """
    kept: list[dict[str, Any]] = []
    for cand in candidates:
        norm = _norm_statement(cand.get("statement") or "")
        if not norm:
            continue
        duplicate = any(
            SequenceMatcher(None, norm, _norm_statement(k["statement"])).ratio() > threshold
            for k in kept
        )
        if not duplicate:
            kept.append(cand)
    return kept


async def generate(
    session: AsyncSession,
    llm: LLMRouter,
    *,
    library_id: uuid.UUID,
    direction: str,
    n_candidates: int = DEFAULT_CANDIDATES,
    existing: list[str] | None = None,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """三路灵感 → 一次 LLM 组合出 n 个候选 → 去重。

    ``existing``：这次探索里已经提出过的假设陈述。给了就随输入交给模型，要求新候选
    与它们有实质区别——不给的时候，细化一个父假设常常只拿回它的几种改写，去重折叠
    之后一轮只剩一个子假设（真实模型实测）。

    返回 {"candidates": [{statement, rationale}], "usage": {...}, "trace": {...}}；
    candidates 已去重，数量 ≤ n_candidates（可能因折叠更少）。trace 是确定性
    检索留痕（查询 + 捞回的 paper_ids），供 expand 记进轮次账本、D6 披露消费。
    """
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    semantic_chunks = await _retrieve(
        session,
        library_id=library_id,
        query=direction,
        user_id=user_id,
        project_id=project_id,
        limit=library_rag.TOP_K,
    )
    covered = {c.paper_id for c in semantic_chunks}
    seed_papers: list[uuid.UUID] = []
    for chunk in semantic_chunks:
        if chunk.paper_id not in seed_papers:
            seed_papers.append(chunk.paper_id)
    far_chunks = await _citation_far_chunks(
        session, library_id=library_id, seed_paper_ids=seed_papers[:INSPIRATION_PER_ROUTE]
    )
    covered |= {c.paper_id for c in far_chunks}
    diverse_chunks = await _diverse_window_chunks(
        session, library_id=library_id, exclude_paper_ids=covered
    )
    all_ids = list({c.paper_id for c in semantic_chunks + far_chunks + diverse_chunks})
    titles = await _paper_titles(session, all_ids)
    # 三路燃料（#670）：方法类比 / 未连接概念对 / 开放缺口，失败静默降级为空
    fuel_sections, fuel_trace = await _gather_fuels(
        session, library_id=library_id, direction=direction, user_id=user_id
    )
    payload = {
        "direction": direction,
        "n": n_candidates,
        "inspirations": {
            "semantic": [_chunk_entry(c, titles) for c in semantic_chunks],
            "citation_far": [_chunk_entry(c, titles) for c in far_chunks],
            "diverse": [_chunk_entry(c, titles) for c in diverse_chunks],
        },
    }
    if fuel_sections:
        # 全空时不加键：无燃料的 payload 与纯检索路逐字节一致（行为等价判据）
        payload["fuels"] = fuel_sections
    known = [str(x).strip() for x in (existing or []) if str(x).strip()]
    if known:
        payload["existing"] = known
    out = await _complete_json(
        llm,
        GENERATE_STAGE,
        system=GENERATE_SYSTEM_PROMPT,
        payload=payload,
        schema=GenerateOut,
        usage=usage,
        effort=GENERATE_EFFORT,
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,
        voyage_id=voyage_id,
    )
    raw = [{"statement": c.statement, "rationale": c.rationale.strip()} for c in out.candidates]
    return {
        "candidates": dedup_candidates(raw[:n_candidates]),
        "usage": usage,
        # 检索留痕（确定性数据，D6 披露 #655）：发出过什么查询、捞回了哪些论文。
        # 三路灵感只有语义路有查询文本；引文远端/多样窗按路记论文集合。
        "trace": {
            "queries": [{"query": direction, "paper_ids": _ordered_paper_ids(semantic_chunks)}],
            "inspiration_paper_ids": {
                "semantic": _ordered_paper_ids(semantic_chunks),
                "citation_far": _ordered_paper_ids(far_chunks),
                "diverse": _ordered_paper_ids(diverse_chunks),
            },
            # 燃料留痕（#670）：消费了哪些方法卡/概念对/缺口条目，三键恒在
            # （全空 = 这次没有燃料可用），供轮次账本与 D6 披露归档
            "fuels": fuel_trace,
        },
    }


# ---- 2) 证据池：一轮扩展检索到的全部片段，去重后编号 ----


class EvidencePool:
    """证据池：接地与查新两轮检索捞到的片段，按片段去重、按首次出现编号。

    为什么要有它（实测，26 篇的库、4 轮探索）：一条假设 6 条子命题、每条 12 段、接地
    与查新各检索一轮，就是 144 个片段位；同一个库里它们大量重复，而以前每个位置都把
    1,500 字原文重发一遍——``hyp_ground`` 与 ``hyp_novelty`` 一次探索吃掉 140 万输入
    token，占假设阶段费用的四分之三。现在每段原文只出现一次，子命题只列编号。

    一轮扩展的几条假设共用一个池（编号在这一轮里唯一），各自记下自己用到哪些条目，
    见 :class:`HypothesisEvidence`。

    渲染结果（:meth:`render`）是**确定的**：同样的检索结果永远同样的字节。
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._by_chunk: dict[uuid.UUID, str] = {}
        self._by_id: dict[str, dict[str, Any]] = {}

    def add(self, chunk: PaperChunk, titles: dict[uuid.UUID, str]) -> dict[str, Any]:
        """登记一个片段，返回它的条目（已登记过就返回原条目，编号不变）。

        条目除了给模型看的标题与原文，还记着片段出自哪个分段、在分段原文里的起止
        位置（``chunk_id`` / ``match_start`` / ``match_end``），证据卡据此定位原文。
        """
        known = self._by_chunk.get(chunk.id)
        if known is not None:
            return self._by_id[known]
        start, end = snippet_span(chunk)
        entry = {
            "id": f"c{len(self.entries) + 1}",
            "paper_id": str(chunk.paper_id),
            "title": titles.get(chunk.paper_id, ""),
            "snippet": chunk.text[start:end],
            "chunk_id": str(chunk.id),
            "match_start": start,
            "match_end": end,
        }
        self.entries.append(entry)
        self._by_chunk[chunk.id] = entry["id"]
        self._by_id[entry["id"]] = entry
        return entry

    def __len__(self) -> int:
        return len(self.entries)

    @staticmethod
    def render(entries: list[dict[str, Any]]) -> str:
        """给模型看的证据正文。只放编号、论文标题和原文——paper_id 是给代码用的，
        模型引用的是片段编号，由代码换回论文。"""
        parts = ["<evidence>"]
        for entry in entries:
            parts.append(f"[{entry['id']}] {entry['title']}\n{entry['snippet']}")
        parts.append("</evidence>")
        return "\n\n".join(parts)


class HypothesisEvidence:
    """一条假设的检索结果：每条子命题在接地 / 查新两轮里各捞到哪些片段。

    ``pool`` 是这一轮共用的证据池；``shared_ids`` 是这条假设与同轮别的假设**都**用到
    的片段——它们单独成一段、放在提示最前面并打缓存断点（见 :func:`retrieve_evidence`
    的 why），其余的片段只给这条假设。
    """

    def __init__(self, subclaims: list[str], pool: EvidencePool | None = None) -> None:
        self.subclaims = subclaims
        self.pool = pool if pool is not None else EvidencePool()
        #: 与 subclaims 逐位对应；每条是该子命题检索到的池内条目
        self.ground: list[list[dict[str, Any]]] = []
        self.novelty: list[list[dict[str, Any]]] = []
        self.ground_trace: list[dict[str, Any]] = []
        self.novelty_trace: list[dict[str, Any]] = []
        self.shared_ids: frozenset[str] = frozenset()
        #: 判定调用一并给出的查新结论（index → (verdict, paper_ids)）；None = 还没判过
        self.verdicts: dict[int, tuple[str, list[str]]] | None = None
        #: 判定调用一并写的风险论证；None = 还没写过
        self.risk_note: str | None = None

    def entry_ids(self) -> set[str]:
        return {e["id"] for rows in (*self.ground, *self.novelty) for e in rows}

    def entries(self) -> list[dict[str, Any]]:
        """这条假设用到的全部条目，按池内编号顺序。"""
        mine = self.entry_ids()
        return [e for e in self.pool.entries if e["id"] in mine]

    def shared_block(self) -> str | None:
        """同轮共用的那一段（没有共用片段时为 None）。同一轮里逐字节相同。"""
        if not self.shared_ids:
            return None
        return EvidencePool.render([e for e in self.pool.entries if e["id"] in self.shared_ids])

    def own_block(self) -> str:
        """只属于这条假设的片段（可能为空串）。"""
        own = [e for e in self.entries() if e["id"] not in self.shared_ids]
        return EvidencePool.render(own) if own else ""


# 换措辞的确定性前缀：目的不是「更好的查询」，而是**换一个角度**再捞一轮——
# 同一措辞查两遍只会复读接地结果，查新就成了自证
_NOVELTY_QUERY_PREFIX = "已有研究 prior work on"
#: 多取几条，给去掉「只有论文头部」的首段留余量（见 _drop_front_matter）
_RETRIEVE_SLACK = 2


def _novelty_query(subclaim: str) -> str:
    return f"{_NOVELTY_QUERY_PREFIX} {subclaim}"


async def retrieve_evidence(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    subclaim_sets: list[list[str]],
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    include_ground: bool = True,
) -> list[HypothesisEvidence]:
    """为一轮里若干条假设的子命题做完全部检索（接地一轮 + 查新换措辞一轮）。

    所有查询**一次**向量化：一轮扩展 4 条假设 × 6 条子命题 × 2 轮 = 48 条查询，逐条
    请求向量模型就是 48 次往返。检索本身是确定性的，结果与逐条查完全一样。

    几条假设共用一个池，并标出**同轮至少两条假设都用到**的片段（``shared_ids``）。
    实测（25 篇的库、一次探索 13 条假设）：同一轮兄弟假设围绕同一个父假设展开，一轮
    四条假设各自约 70 段，合起来去重后约 190 段——三成的原文被重复发送。把共用的那
    部分做成同一段前缀并打缓存断点，第一条判定写缓存、其余三条按一成的价钱读，判定
    输入的费用降到原来的约八成；每条假设独有的片段仍然只发一次。
    """
    pool = EvidencePool()
    bundles = [HypothesisEvidence(list(subclaims), pool) for subclaims in subclaim_sets]
    queries: list[str] = []
    for bundle in bundles:
        if include_ground:
            queries.extend(bundle.subclaims)
        queries.extend(_novelty_query(s) for s in bundle.subclaims)
    hits = await library_rag.retrieve_many(
        session,
        library_id=library_id,
        queries=queries,
        user_id=user_id,
        project_id=project_id,
        limit=GROUND_TOP_K + _RETRIEVE_SLACK,
    )
    hits = [_drop_front_matter(rows, GROUND_TOP_K) for rows in hits]
    all_chunks = [chunk for rows in hits for chunk in rows]
    titles = await _paper_titles(session, list({c.paper_id for c in all_chunks}))
    cursor = 0
    for bundle in bundles:
        rounds = ([("ground", bundle.subclaims)] if include_ground else []) + [
            ("novelty", [_novelty_query(s) for s in bundle.subclaims])
        ]
        for phase, phase_queries in rounds:
            for query in phase_queries:
                chunks = hits[cursor]
                cursor += 1
                entries = [pool.add(c, titles) for c in chunks]
                # 检索留痕（D6 披露 #655）：接地的查询是子命题本身，查新是换措辞后的
                trace = {"query": query, "paper_ids": _ordered_paper_ids(chunks)}
                if phase == "ground":
                    bundle.ground.append(entries)
                    bundle.ground_trace.append(trace)
                else:
                    bundle.novelty.append(entries)
                    bundle.novelty_trace.append(trace)
    mark_shared(bundles)
    return bundles


def mark_shared(bundles: list[HypothesisEvidence]) -> None:
    """标出同轮至少两条假设都用到的片段（各条假设的 ``shared_ids``）。

    一段共用片段都没用到的假设不带这段前缀：读缓存也要花钱。只有一条假设时没有
    「共用」可言。
    """
    if len(bundles) < 2:
        return
    seen: dict[str, int] = {}
    for bundle in bundles:
        for entry_id in bundle.entry_ids():
            seen[entry_id] = seen.get(entry_id, 0) + 1
    shared = frozenset(entry_id for entry_id, n in seen.items() if n > 1)
    for bundle in bundles:
        if bundle.entry_ids() & shared:
            bundle.shared_ids = shared


# ---- 3) 文献接地：拆子命题 → 确定性检索 → 只许从检索集内选 ----


def sanitize_grounding(
    subclaims: list[str],
    retrieved: list[list[dict[str, Any]]],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """接地后处理（纯函数，越界校验的落点）：

    - 立场只认 support/refute，其余（含缺失/乱写）一律 speculation；
    - evidence_ids 逐条与**该子命题自己的**检索集求交，越界编号直接丢弃；
    - 立场是 support/refute 但没剩下任何合法编号 → 降为 speculation——
      「有立场没证据」和编造引文一样不可信；
    - snippets 回填**被选中的那几段**原文，证据卡（D5）直接可用；
      snippet_paper_ids 与 snippets 等长、逐位对应，说明每段出自哪篇论文
      （一篇论文可能有多段入选）；paper_ids 是它们去重保序的论文集合；
      snippet_chunk_ids / snippet_spans 同样逐位对应：片段出自哪个分段、在分段原文
      里的 [起, 止)（没有入选片段、或检索条目没带这些信息时不写这两个键）。
    """
    by_index: dict[int, dict[str, Any]] = {}
    for item in items:
        idx = item.get("index")
        if isinstance(idx, int) and idx not in by_index:
            by_index[idx] = item
    out: list[dict[str, Any]] = []
    for i, subclaim in enumerate(subclaims):
        pool = retrieved[i] if i < len(retrieved) else []
        raw = by_index.get(i) or {}
        stance = str(raw.get("stance") or "")
        chosen = {str(e) for e in (raw.get("evidence_ids") or [])}
        picked = [entry for entry in pool if entry["id"] in chosen]
        if stance not in ("support", "refute") or not picked:
            stance, picked = "speculation", []
        row: dict[str, Any] = {
            "subclaim": subclaim,
            "stance": stance,
            "paper_ids": list(dict.fromkeys(entry["paper_id"] for entry in picked)),
            "snippets": [entry["snippet"] for entry in picked],
            "snippet_paper_ids": [entry["paper_id"] for entry in picked],
        }
        if picked and all("chunk_id" in entry for entry in picked):
            row["snippet_chunk_ids"] = [entry["chunk_id"] for entry in picked]
            row["snippet_spans"] = [[entry["match_start"], entry["match_end"]] for entry in picked]
        out.append(row)
    return out


def _judge_message(statement: str, evidence: HypothesisEvidence, *, ground: bool) -> str:
    """判定调用的 user 消息：这条假设独有的证据在前，随后是假设与每条子命题的候选
    片段编号。同轮共用的那段证据不在这里，它作为缓存前缀排在这条消息之前
    （:meth:`HypothesisEvidence.shared_block`）。

    ``ground=False``：只做查新（接地已经判过），不给 ground_candidates。
    """
    items = []
    for i, subclaim in enumerate(evidence.subclaims):
        item: dict[str, Any] = {"index": i, "subclaim": subclaim}
        item["ground_candidates"] = [e["id"] for e in evidence.ground[i]] if ground else []
        item["novelty_candidates"] = [e["id"] for e in evidence.novelty[i]]
        items.append(item)
    payload = {"statement": statement, "items": items}
    body = json.dumps(payload, ensure_ascii=False)
    own = evidence.own_block()
    return f"{own}\n\n{body}" if own else body


async def _judge(
    llm: LLMRouter,
    stage: str,
    *,
    statement: str,
    evidence: HypothesisEvidence,
    ground: bool,
    usage: dict[str, int],
    identity: dict[str, Any],
) -> JudgementOut:
    """一次判定调用。``ground=True`` 时一并写风险论证（schema 多一个 risk_note）。"""
    return await _complete_json(
        llm,
        stage,
        system=JUDGE_SYSTEM_PROMPT,
        user=_judge_message(statement, evidence, ground=ground),
        cache_prefix=evidence.shared_block(),
        schema=GroundJudgementOut if ground else JudgementOut,
        usage=usage,
        effort=JUDGE_EFFORT,
        **identity,
    )


def _collect_verdicts(
    evidence: HypothesisEvidence, items: list[JudgementItemOut]
) -> dict[int, tuple[str, list[str]]]:
    """查新结论的越界校验：只认有候选片段的子命题，论文只取自它自己的候选。"""
    verdicts: dict[int, tuple[str, list[str]]] = {}
    for item in items:
        if not 0 <= item.index < len(evidence.novelty) or item.index in verdicts:
            continue
        candidates = evidence.novelty[item.index]
        if not candidates:
            continue  # 复检什么都没捞到：没有文献可对照，不算判过
        chosen = set(item.verdict_evidence_ids)
        ids = [e["paper_id"] for e in candidates if e["id"] in chosen]
        verdicts[item.index] = (item.verdict, list(dict.fromkeys(ids)))
    return verdicts


async def split_subclaims(
    llm: LLMRouter,
    *,
    statement: str,
    usage: dict[str, int],
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    library_id: uuid.UUID,
    voyage_id: uuid.UUID | None,
) -> list[str]:
    """假设 → 最多 :data:`MAX_SUBCLAIMS` 条可独立检索验证的子命题（一次模型调用）。"""
    split = await _complete_json(
        llm,
        GROUND_STAGE,
        system=GROUND_SPLIT_SYSTEM_PROMPT,
        payload={"statement": statement},
        schema=SubclaimsOut,
        usage=usage,
        effort=SPLIT_EFFORT,
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,
        voyage_id=voyage_id,
    )
    return list(split.subclaims)[:MAX_SUBCLAIMS]


async def judge_grounding(
    llm: LLMRouter,
    *,
    statement: str,
    evidence: HypothesisEvidence,
    usage: dict[str, int],
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    library_id: uuid.UUID,
    voyage_id: uuid.UUID | None,
) -> list[dict[str, Any]]:
    """接地 + 查新判定 + 风险论证（一次模型调用）→ grounding JSON；查新结论与风险论证
    记在 ``evidence`` 上，:func:`judge_novelty` 与可行性直接取用。

    一段都没检索到（库里没有可检索的片段）时不调模型：没有可选的证据，全部
    speculation 就是唯一诚实的答案（风险论证也就没有可写的依据）。
    """
    subclaims = evidence.subclaims
    if not evidence.entries():
        evidence.verdicts = {}
        evidence.risk_note = RISK_NOTE_NO_EVIDENCE
        return sanitize_grounding(subclaims, evidence.ground, [])
    identity: dict[str, Any] = dict(
        user_id=user_id, project_id=project_id, library_id=library_id, voyage_id=voyage_id
    )
    judged = await _judge(
        llm,
        GROUND_STAGE,
        statement=statement,
        evidence=evidence,
        ground=True,
        usage=usage,
        identity=identity,
    )
    evidence.verdicts = _collect_verdicts(evidence, judged.items)
    if isinstance(judged, GroundJudgementOut):
        evidence.risk_note = judged.risk_note
    items = [
        {"index": item.index, "stance": item.stance, "evidence_ids": item.stance_evidence_ids}
        for item in judged.items
    ]
    return sanitize_grounding(subclaims, evidence.ground, items)


async def ground(
    session: AsyncSession,
    llm: LLMRouter,
    *,
    library_id: uuid.UUID,
    statement: str,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """假设 → grounding JSON（与 models/hypothesis.py 的 D1 注释一致）：

    [{"subclaim", "stance": "support|refute|speculation", "paper_ids", "snippets"}]

    返回值里的 ``evidence`` 是这条假设的检索结果与查新结论（查新那一轮的检索和判定
    与接地在同一次调用里做完），原样传给 :func:`novelty` 就不用再检索、再调模型。
    """
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    identity: dict[str, Any] = dict(
        user_id=user_id, project_id=project_id, library_id=library_id, voyage_id=voyage_id
    )
    subclaims = await split_subclaims(llm, statement=statement, usage=usage, **identity)
    (evidence,) = await retrieve_evidence(
        session,
        library_id=library_id,
        subclaim_sets=[subclaims],
        user_id=user_id,
        project_id=project_id,
    )
    grounding = await judge_grounding(
        llm, statement=statement, evidence=evidence, usage=usage, **identity
    )
    return {
        "grounding": grounding,
        "usage": usage,
        "trace": {"queries": evidence.ground_trace},
        "evidence": evidence,
    }


# ---- 4) 查新：换措辞再检索 → 逐子命题 judge ----


def novelty_ratio(report: dict[str, Any]) -> float:
    """novel 子命题占比（score 公式的一半；空报告为 0）。"""
    subclaims = report.get("subclaims") or []
    if not subclaims:
        return 0.0
    novel = sum(1 for e in subclaims if e.get("verdict") == "novel")
    return novel / len(subclaims)


async def judge_novelty(
    llm: LLMRouter,
    *,
    statement: str,
    grounding: list[dict[str, Any]],
    evidence: HypothesisEvidence,
    usage: dict[str, int],
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    library_id: uuid.UUID,
    voyage_id: uuid.UUID | None,
) -> dict[str, Any]:
    """查新结论 → novelty_report。

    接地判定那一次调用已经一并判过（``evidence.verdicts``）时直接成文，不再调模型；
    单独调用（只有 grounding、没判过）时发一次只做查新的判定。
    """
    verdicts = evidence.verdicts
    if verdicts is None:
        verdicts = {}
        if any(evidence.novelty):
            try:
                judged = await _judge(
                    llm,
                    NOVELTY_STAGE,
                    statement=statement,
                    evidence=evidence,
                    ground=False,
                    usage=usage,
                    identity=dict(
                        user_id=user_id,
                        project_id=project_id,
                        library_id=library_id,
                        voyage_id=voyage_id,
                    ),
                )
                verdicts = _collect_verdicts(evidence, judged.items)
            except _NO_JUDGEMENT:
                logger.warning(
                    "novelty judgement unavailable; marking all uncertain", exc_info=True
                )
    subclaim_reports = []
    for i, entry in enumerate(grounding):
        verdict, ids = verdicts.get(i, ("uncertain", []))
        subclaim_reports.append(
            {
                "subclaim": entry["subclaim"],
                "verdict": verdict,
                "paper_ids": ids,
                "judged": i in verdicts,
            }
        )
    return {"subclaims": subclaim_reports}


async def novelty(
    session: AsyncSession,
    llm: LLMRouter,
    *,
    library_id: uuid.UUID,
    statement: str,
    grounding: list[dict[str, Any]],
    evidence: HypothesisEvidence | None = None,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """逐子命题查新 → novelty_report：

    {"subclaims": [{"subclaim", "verdict": "known|novel|uncertain",
                    "paper_ids", "judged"}]}

    每条子命题都换措辞复检一轮再送 judge，接地时判成 speculation 的也一样。
    score 是「新颖比例 × 有支持的比例」：有文献支持的子命题多半会被判 known，
    新颖的部分恰恰落在没有文献依据的那几条上。只判有依据的子命题时，真实模型
    下新颖比例接近零，一轮扩展出来的子假设全部低于剪枝线（实测 4 个全剪）。

    复检什么都没捞到的子命题（库里没有可检索的片段）不送 judge：没有任何文献
    可对照，「是否已被覆盖」无从判起，如实记 uncertain / judged=False。模型给
    不出判定（结构不对、写不完、拒答）时也整体降级为 uncertain——查新判不动
    不等于假设不成立，不该炸掉整轮扩展。网络、限流、密钥这类失败照常上抛。

    ``evidence`` 是 :func:`ground` 返回的检索结果与查新结论；不给时自己检索查新那
    一轮并单独判一次。
    """
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    if evidence is None:
        (evidence,) = await retrieve_evidence(
            session,
            library_id=library_id,
            subclaim_sets=[[e["subclaim"] for e in grounding]],
            user_id=user_id,
            project_id=project_id,
            include_ground=False,
        )
    report = await judge_novelty(
        llm,
        statement=statement,
        grounding=grounding,
        evidence=evidence,
        usage=usage,
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,
        voyage_id=voyage_id,
    )
    return {"report": report, "usage": usage, "trace": {"queries": evidence.novelty_trace}}


# ---- 5) 可行性：确定性资源信号 + LLM 只写风险论证 ----

RISK_NOTE_UNAVAILABLE = "（风险论证生成失败，仅供确定性信号参考。）"
RISK_NOTE_NO_EVIDENCE = "（库里检索不到与这条假设相关的片段，没有可以论证风险的依据。）"


async def feasibility_signals(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    statement: str,
    grounding: list[dict[str, Any]],
) -> dict[str, Any]:
    """确定性资源信号（§12：资源匹配是确定性查表，不调模型）：

    - grounded_paper_count / venues / year_range：接地文献的规模与来源分布，
      「有多少扎实文献托底、来自哪里、新不新」；
    - related_chunk_hits：库内与该假设相关的片段密度（关键词口径），密度低
      意味着验证材料在库内很薄，得先扩充语料。
    """
    grounded_ids: list[uuid.UUID] = []
    for entry in grounding:
        if entry.get("stance") == "speculation":
            continue
        for raw in entry.get("paper_ids") or []:
            pid = uuid.UUID(str(raw))
            if pid not in grounded_ids:
                grounded_ids.append(pid)
    papers = []
    if grounded_ids:
        papers = (
            (await session.execute(select(Paper).where(Paper.id.in_(grounded_ids)))).scalars().all()
        )
    venues: dict[str, int] = {}
    years: list[int] = []
    for paper in papers:
        if paper.venue:
            venues[paper.venue] = venues.get(paper.venue, 0) + 1
        if paper.year:
            years.append(int(paper.year))
    try:
        related_hits = len(
            await chunks_service.keyword_search_chunks(
                session, library_ids=[library_id], q=statement, limit=DENSITY_LIMIT
            )
        )
    except Exception:  # noqa: BLE001 — 密度只是参考信号，统计失败按 0 记不上抛
        related_hits = 0
    return {
        "grounded_paper_count": len(papers),
        "venues": dict(sorted(venues.items())),
        "year_range": [min(years), max(years)] if years else None,
        "related_chunk_hits": related_hits,
    }


async def risk_note(
    llm: LLMRouter,
    *,
    statement: str,
    signals: dict[str, Any],
    usage: dict[str, int],
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    library_id: uuid.UUID,
    voyage_id: uuid.UUID | None,
) -> str:
    """风险论证段（一次模型调用）。写不出来不影响信号本身的价值：note 如实说不可用。"""
    try:
        out = await _complete_json(
            llm,
            FEASIBILITY_STAGE,
            system=FEASIBILITY_SYSTEM_PROMPT,
            payload={"statement": statement, "signals": signals},
            schema=RiskNoteOut,
            usage=usage,
            effort=FEASIBILITY_EFFORT,
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=voyage_id,
        )
    except _NO_JUDGEMENT:
        logger.warning("feasibility risk note unavailable", exc_info=True)
        return RISK_NOTE_UNAVAILABLE
    return out.risk_note


async def feasibility(
    session: AsyncSession,
    llm: LLMRouter,
    *,
    library_id: uuid.UUID,
    statement: str,
    grounding: list[dict[str, Any]],
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """feasibility JSON = {"signals": {确定性信号}, "risk_note": LLM 风险论证}。"""
    signals = await feasibility_signals(
        session, library_id=library_id, statement=statement, grounding=grounding
    )
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    note = await risk_note(
        llm,
        statement=statement,
        signals=signals,
        usage=usage,
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,
        voyage_id=voyage_id,
    )
    return {"feasibility": {"signals": signals, "risk_note": note}, "usage": usage}


# ---- 6) 一轮扩展：若干候选并发走完 接地 + 查新 + 风险论证 ----


async def _judge_round(
    llm: LLMRouter,
    *,
    statements: list[str],
    bundles: list[HypothesisEvidence],
    usages: list[dict[str, int]],
    identity: dict[str, Any],
    limited: Callable[[Any], Awaitable[Any]],
) -> list[list[dict[str, Any]]]:
    """一轮的判定调用：**先暖缓存、再扇出**。

    同轮共用的证据段（:meth:`HypothesisEvidence.shared_block`）要等第一次调用开始回传
    之后才读得到缓存；几次同时发出去就都按原价读。所以先发一条带共用段的，等它开始
    回传（provider 支持时）或干脆跑完，再发其余的。等的只是服务端读完提示那几秒。
    """

    def judge_one(i: int) -> Awaitable[list[dict[str, Any]]]:
        return judge_grounding(
            llm, statement=statements[i], evidence=bundles[i], usage=usages[i], **identity
        )

    order = list(range(len(statements)))
    results: list[Any] = [None] * len(statements)
    lead = next((i for i in order if bundles[i].shared_ids), None)
    lead_task: asyncio.Future[Any] | None = None
    if lead is not None and len(order) > 1:
        started = asyncio.Event()
        lead_index: int = lead

        async def lead_call() -> list[dict[str, Any]]:
            structured_stream_started.set(started)  # 只在这个任务的上下文里生效
            return await judge_one(lead_index)

        lead_task = asyncio.ensure_future(limited(lead_call()))
        waiter = asyncio.ensure_future(started.wait())
        try:
            await asyncio.wait({lead_task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        order.remove(lead)
    rest = await asyncio.gather(*(limited(judge_one(i)) for i in order), return_exceptions=True)
    for i, outcome in zip(order, rest, strict=True):
        results[i] = outcome
    if lead_task is not None and lead is not None:
        (results[lead],) = await asyncio.gather(lead_task, return_exceptions=True)
    for outcome in results:
        if isinstance(outcome, BaseException):
            raise outcome
    return results


async def evaluate_candidates(
    session: AsyncSession,
    llm: LLMRouter,
    *,
    library_id: uuid.UUID,
    statements: list[str],
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
    concurrency: int = EVALUATE_CONCURRENCY,
) -> list[dict[str, Any]]:
    """一轮扩展的全部候选：拆子命题 → 检索 → 接地 + 查新 + 风险论证 → 打分。

    以前是逐候选、逐调用串行（一轮 4 条 × 4 次调用排队，实测每轮 3–4 分钟）。模型调用
    之间没有依赖的部分现在并发，用一个信号量限流（网关会 503，并发不能无界）：

    1. 各候选**并发**拆子命题；
    2. 全部检索一次做完（数据库会话不能并发用，检索本身也不慢）；
    3. 各候选做判定——接地、查新和风险论证在同一次调用里；同轮共用的证据段先由一条
       调用写进缓存，其余的再并发（见 :func:`_judge_round`）；
    4. 打分，并顺序算确定性的可行性信号（查库）。

    返回与 ``statements`` 逐位对应的 {statement, grounding, novelty_report,
    feasibility, score, usage, queries}。任何一次调用的不可重试失败都会让整轮失败——
    先算后写，树还没动，整步重跑是干净的。
    """
    identity: dict[str, Any] = dict(
        user_id=user_id, project_id=project_id, library_id=library_id, voyage_id=voyage_id
    )
    gate = asyncio.Semaphore(max(1, concurrency))
    usages = [{"prompt_tokens": 0, "completion_tokens": 0} for _ in statements]

    async def limited(coro: Any) -> Any:
        async with gate:
            return await coro

    async def run_all(coros: list[Any]) -> list[Any]:
        """并发跑完；有失败时等其余的收尾再抛第一个（不留没人等的任务）。"""
        results = await asyncio.gather(*(limited(c) for c in coros), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return list(results)

    subclaim_sets = await run_all(
        [
            split_subclaims(llm, statement=statement, usage=usages[i], **identity)
            for i, statement in enumerate(statements)
        ]
    )
    bundles = await retrieve_evidence(
        session,
        library_id=library_id,
        subclaim_sets=subclaim_sets,
        user_id=user_id,
        project_id=project_id,
    )
    groundings = await _judge_round(
        llm,
        statements=statements,
        bundles=bundles,
        usages=usages,
        identity=identity,
        limited=limited,
    )
    reports = [
        await judge_novelty(
            llm,
            statement=statement,
            grounding=groundings[i],
            evidence=bundles[i],
            usage=usages[i],
            **identity,
        )
        for i, statement in enumerate(statements)
    ]
    scores = [score_hypothesis(groundings[i], reports[i]) for i in range(len(statements))]
    signals = [
        await feasibility_signals(
            session, library_id=library_id, statement=statement, grounding=groundings[i]
        )
        for i, statement in enumerate(statements)
    ]
    return [
        {
            "statement": statement,
            "grounding": groundings[i],
            "novelty_report": reports[i],
            "feasibility": {
                "signals": signals[i],
                "risk_note": bundles[i].risk_note or RISK_NOTE_UNAVAILABLE,
            },
            "score": scores[i],
            "usage": usages[i],
            "queries": [{"phase": "ground", **q} for q in bundles[i].ground_trace]
            + [{"phase": "novelty", **q} for q in bundles[i].novelty_trace],
        }
        for i, statement in enumerate(statements)
    ]


# ---- 评分 ----


def score_hypothesis(grounding: list[dict[str, Any]], novelty_report: dict[str, Any]) -> float:
    """score = novel 比例 × support 覆盖率（见模块 docstring 的 why）。"""
    total = len(grounding)
    if total == 0:
        return 0.0
    support = sum(1 for e in grounding if e.get("stance") == "support")
    return round(novelty_ratio(novelty_report) * (support / total), 4)


def support_ratio(grounding: list[dict[str, Any]]) -> float:
    """有文献支持的子命题占比（score 公式的另一半；空接地为 0）。"""
    rows = [e for e in grounding if isinstance(e, dict)]
    if not rows:
        return 0.0
    return sum(1 for e in rows if e.get("stance") == "support") / len(rows)


def prune_reason(
    grounding: list[dict[str, Any]],
    novelty_report: dict[str, Any],
    score: float,
    threshold: float = MIN_VIABLE_SCORE,
) -> dict[str, Any]:
    """自动剪枝的结构化原因：分数、剪枝线、两个因子，以及卡在哪一个因子上。

    cause：一条有文献支持的子命题都没有 → ``no_support``；有支持但一条新颖的都没有
    → ``not_novel``；两样都有、只是乘起来不够 → ``low_score``。界面据此用自己的话
    说原因，不必解析中文句子。
    """
    supported = round(support_ratio(grounding), 4)
    novel = round(novelty_ratio(novelty_report), 4)
    if supported == 0:
        cause = "no_support"
    elif novel == 0:
        cause = "not_novel"
    else:
        cause = "low_score"
    return {
        "score": score,
        "threshold": threshold,
        "support_ratio": supported,
        "novel_ratio": novel,
        "cause": cause,
    }
