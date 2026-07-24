"""论文作者↔发表机构解析：LLM 从全文标题页尽力给出每位作者最可能的机构归属。

标题页里作者与机构的对应常不明确（上标脚注 / 邮箱 / 排版就近），所以要"最大可能"的对应
而非严格匹配。优先把已知作者名单交给 LLM 逐一映射（保持作者顺序与规范名）。无全文 / 失败
由调用方兜底（如 OpenAlex 反查）。DOI 论文的作者-机构映射由 OpenAlex 结构化数据直接给。
"""

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.paper import Paper
from app.models.system_setting import SystemSetting

logger = logging.getLogger(__name__)

# ---- 抽取模式（管理员可设，存 SystemSetting）----
# on_add：论文入库/补全阶段就用专门的 LLM 调用解析机构（默认）；
# on_compile：跳过专门调用，把机构映射折叠进 wiki 编译那一次 LLM（省一次调用）。
AFFILIATION_MODE_KEY = "affiliation_extraction_mode"
AFFILIATION_MODES = ("on_add", "on_compile")
DEFAULT_AFFILIATION_MODE = "on_add"


class InvalidAffiliationModeError(ValueError):
    """set_affiliation_extraction_mode 收到不在 AFFILIATION_MODES 里的取值。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        super().__init__(mode)


async def get_affiliation_extraction_mode(session: AsyncSession) -> str:
    """读取机构抽取模式（system_settings 表，默认 on_add；非法存量值回落默认）。"""
    row = await session.get(SystemSetting, AFFILIATION_MODE_KEY)
    value = row.value if row is not None else None
    return value if value in AFFILIATION_MODES else DEFAULT_AFFILIATION_MODE


async def set_affiliation_extraction_mode(session: AsyncSession, mode: str) -> str:
    """写入机构抽取模式；取值非法抛 InvalidAffiliationModeError。"""
    if mode not in AFFILIATION_MODES:
        raise InvalidAffiliationModeError(mode)
    row = await session.get(SystemSetting, AFFILIATION_MODE_KEY)
    if row is None:
        session.add(SystemSetting(key=AFFILIATION_MODE_KEY, value=mode))
    else:
        row.value = mode
    await session.commit()
    return mode

# 标题页范围：作者-机构对应比纯机构列表信息量大，略放宽到 3500 字符
_HEAD_CHARS = 3500
_MAX_TOKENS = 900  # 输出逐作者映射，比纯机构数组略大
_MAX_AFFILIATIONS = 20  # 单篇去重后机构总数上限（防幻觉刷屏）
_MAX_PER_AUTHOR = 4  # 单个作者机构数上限

AUTHOR_AFFIL_SYSTEM_PROMPT = """\
你是论文元数据抽取助手（POLARIS_AUTHOR_AFFIL）。给你一篇论文开头的文本（标题页，含作者\
名单、机构、上标数字/符号脚注、邮箱），以及一份「已知作者名单」。请为每位作者给出其最\
可能所属的机构。
要求：
- 逐位作者输出，作者名用「已知作者名单」里的原名与原顺序，不要增删或改写作者；
- 用上标数字/符号脚注、邮箱域名、排版就近等线索推断作者与机构的对应；对应不明确时给出\
最可能的单个机构（宁缺毋滥，可留空数组），不要硬凑；
- 机构名取到学校/公司/研究院一级（如 "Zhejiang University"、"Google DeepMind"），不要\
院系/实验室等下级细分；保留论文中的英文原名，不要翻译；不含地址、城市、国家、邮箱；
- 只输出一个 JSON 数组，每项形如 {"name": "<作者名>", "affiliations": ["<机构名>", ...]}，\
不要输出任何其他文字；
- 完全识别不出某作者的机构时，其 affiliations 输出 []。
"""
_NO_AUTHORS_HINT = "（已知作者名单为空：请你先从标题页识别作者，再映射其机构。）"


def _read_head(paper: Paper) -> str | None:
    """取全文开头 ~3500 字符（标题页）；无全文 / 文件缺失返回 None。"""
    if not paper.full_text_path:
        return None
    path = Path(paper.full_text_path)
    if not path.exists():
        return None
    head = path.read_text(encoding="utf-8", errors="ignore")[:_HEAD_CHARS].strip()
    return head or None


def author_names(paper: Paper) -> list[str]:
    """已存的作者名（兼容 [{"name":...}] 与历史的纯字符串列表）。"""
    out: list[str] = []
    for a in paper.authors or []:
        if isinstance(a, dict) and a.get("name"):
            out.append(str(a["name"]).strip())
        elif isinstance(a, str) and a.strip():
            out.append(a.strip())
    return out


def _parse_mapping(content: str) -> list[dict[str, Any]] | None:
    """解析 LLM 输出的 [{"name","affiliations"}]；去重截断；解析失败 / 空返回 None。"""
    start = content.find("[")
    end = content.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    result: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        raw = item.get("affiliations")
        affs = (
            list(dict.fromkeys(s for x in raw if isinstance(x, str) and (s := x.strip())))[
                :_MAX_PER_AUTHOR
            ]
            if isinstance(raw, list)
            else []
        )
        result.append({"name": name, "affiliations": affs})
    return result or None


def flatten_affiliations(mapping: list[dict[str, Any]]) -> list[str]:
    """从作者→机构映射汇成去重的机构总表（供筛选 chips / 展示）。"""
    return list(
        dict.fromkeys(
            aff
            for item in mapping
            for aff in (item.get("affiliations") or [])
            if isinstance(aff, str) and aff
        )
    )[:_MAX_AFFILIATIONS]


def apply_author_affiliations(paper: Paper, mapping: list[dict[str, Any]] | None) -> bool:
    """把作者→机构映射写回 paper：每位作者带上 affiliations，并汇总 paper.affiliations。

    按作者名（小写去空白）对齐已有作者，补 affiliations、保留原有字段；已知作者为空时直接
    用映射建作者。返回是否写入了任何机构（无机构时不覆盖，交调用方兜底）。
    """
    if not mapping:
        return False
    by_name = {
        str(item["name"]).strip().lower(): (item.get("affiliations") or []) for item in mapping
    }
    existing = paper.authors or []
    if existing:
        merged: list[Any] = []
        for a in existing:
            if isinstance(a, dict) and a.get("name"):
                affs = by_name.get(str(a["name"]).strip().lower())
                merged.append({**a, "affiliations": affs} if affs else dict(a))
            elif isinstance(a, str) and a.strip():
                affs = by_name.get(a.strip().lower())
                merged.append(
                    {"name": a.strip(), "affiliations": affs} if affs else {"name": a.strip()}
                )
            else:
                merged.append(a)
        paper.authors = merged
    else:
        paper.authors = [
            {"name": item["name"], "affiliations": item.get("affiliations") or []}
            for item in mapping
        ]
    flat = flatten_affiliations(mapping)
    if flat:
        paper.affiliations = flat
    return bool(flat)


async def extract_author_affiliations_llm(
    paper: Paper,
    *,
    llm: LLMRouter,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> list[dict[str, Any]] | None:
    """LLM 从标题页解析每位作者的最可能机构，返回 [{"name","affiliations"}]。

    无全文、调用失败或解析不出都返回 None（调用方自行兜底）。
    """
    head = _read_head(paper)
    if head is None:
        return None
    known = author_names(paper)
    known_block = "\n".join(f"- {n}" for n in known) if known else _NO_AUTHORS_HINT
    try:
        result = await llm.complete(
            "librarian",
            [
                Message(role="system", content=AUTHOR_AFFIL_SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=(
                        f"论文标题：{paper.title}\n\n已知作者名单：\n{known_block}\n\n"
                        f"论文开头文本：\n{head}"
                    ),
                ),
            ],
            max_tokens=_MAX_TOKENS,
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=voyage_id,
        )
        return _parse_mapping(result.content)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — 解析尽力而为，失败由调用方兜底
        logger.warning(
            "LLM author-affiliation extraction failed for paper %s", paper.id, exc_info=True
        )
        return None


# ---- on_compile 模式：折叠进 wiki 编译的作者↔机构定界块 ----
# LLM 在正文全部结束后附上该块，供确定性解析后从正文剥离（无论解析成败都必须删干净，
# 绝不能残留进最终 wiki）。块内是 [{"name","affiliations"}] JSON。
_AFFIL_BLOCK_RE = re.compile(r"<<<POLARIS_AFFILIATIONS\b(.*?)POLARIS_AFFILIATIONS>>>", re.DOTALL)
# 兜底：模型只吐了开头标记、没闭合 → 从开头标记删到文末，绝不让裸标记漏进 wiki
_AFFIL_DANGLING_RE = re.compile(r"<<<POLARIS_AFFILIATIONS\b.*\Z", re.DOTALL)
# 块前可能被模型顺手带出的分隔线（---）一并清掉，避免正文尾巴挂个孤零零的 hr
_TRAILING_HR_RE = re.compile(r"\s*-{3,}[ \t]*$")

AFFIL_COMPILE_INSTRUCTION = """\

另外，请在上面正文**全部结束之后**，另起一段，按下面固定格式附上每位作者最可能所属的\
机构，仅供系统解析、不属于正文的一部分：
<<<POLARIS_AFFILIATIONS
[{"name": "作者名", "affiliations": ["机构名"]}]
POLARIS_AFFILIATIONS>>>
要求：作者用上文「作者：」里的原名与顺序；机构取学校/公司/研究院一级的英文原名（不含\
院系/城市/国家）；识别不出机构的作者其 affiliations 用 []；这一整块只在文末出现一次，\
不要在正文里提及它。\
"""


def parse_and_strip_affiliation_block(content: str) -> tuple[str, list[dict[str, Any]] | None]:
    """从编译输出剥离作者↔机构定界块并解析其中的映射。

    返回 (剥离后的正文, 映射 | None)。无论块内 JSON 是否可解析，定界块都会被完整删除，
    确保它绝不残留进最终 wiki；解析失败 / 无块时映射为 None。
    """
    match = _AFFIL_BLOCK_RE.search(content)
    if match is None:
        # 没闭合的裸开头标记也要删净（模型偶尔漏闭合），此时解析不出映射
        if "<<<POLARIS_AFFILIATIONS" in content:
            stripped = _AFFIL_DANGLING_RE.sub("", content).rstrip()
            stripped = _TRAILING_HR_RE.sub("", stripped).rstrip()
            return (stripped + "\n" if stripped else stripped), None
        return content, None
    stripped = _AFFIL_BLOCK_RE.sub("", content).rstrip()
    stripped = _TRAILING_HR_RE.sub("", stripped).rstrip()
    stripped = stripped + "\n" if stripped else stripped
    return stripped, _parse_mapping(match.group(1))


async def extract_affiliations_llm(
    paper: Paper,
    *,
    llm: LLMRouter,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> list[str] | None:
    """向后兼容：仅要去重机构总表的旧调用（内部走作者→机构映射再拍平）。"""
    mapping = await extract_author_affiliations_llm(
        paper,
        llm=llm,
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,
        voyage_id=voyage_id,
    )
    if mapping is None:
        return None
    return flatten_affiliations(mapping) or None
