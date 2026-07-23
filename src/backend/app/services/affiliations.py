"""论文发表机构解析：LLM 从全文开头（标题页）提取机构名（不 import fastapi）。

全文抓取成功后优先走这里；无全文 / LLM 失败时由调用方用 OpenAlex 反查兜底。
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.models.paper import Paper

logger = logging.getLogger(__name__)

# 标题页范围：作者名单/机构/邮箱脚注都在论文开头，取前 3000 字符足够且省 token
_HEAD_CHARS = 3000
_MAX_TOKENS = 512  # 输出只是一个机构名数组，轻量调用
_MAX_AFFILIATIONS = 20  # 防御 LLM 幻觉刷屏

AFFILIATIONS_SYSTEM_PROMPT = """\
你是论文元数据抽取助手（POLARIS_AFFILIATIONS）。给你一篇论文开头的文本\
（标题页，含作者名单、机构、邮箱脚注），请提取作者所属机构列表。
要求：
- 机构名取到学校/公司/研究院一级（如 "Zhejiang University"、"Google DeepMind"），\
不要院系/实验室等下级细分；
- 保留论文中的英文原名，不要翻译；
- 去重、按出现顺序排列；不要包含地址、城市、国家、邮箱；
- 只输出一个 JSON 字符串数组，不要输出任何其他文字，例如：\
["Zhejiang University", "Google DeepMind"]
- 文本中识别不出机构时输出 []。
"""


def _read_head(paper: Paper) -> str | None:
    """取全文开头 ~3000 字符（标题页）；无全文/文件缺失返回 None。"""
    if not paper.full_text_path:
        return None
    path = Path(paper.full_text_path)
    if not path.exists():
        return None
    head = path.read_text(encoding="utf-8", errors="ignore")[:_HEAD_CHARS].strip()
    return head or None


def _parse_affiliations(content: str) -> list[str] | None:
    """从 LLM 输出中解析 JSON 字符串数组，去重保序；解析失败/空返回 None。"""
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
    names = list(
        dict.fromkeys(name for item in data if isinstance(item, str) and (name := item.strip()))
    )
    return names[:_MAX_AFFILIATIONS] or None


async def extract_affiliations_llm(
    paper: Paper,
    *,
    llm: LLMRouter,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
    voyage_id: uuid.UUID | None = None,
) -> list[str] | None:
    """LLM 从论文全文标题页解析机构名列表。

    无全文、调用失败或解析不出结果都返回 None（调用方自行兜底，如 OpenAlex 反查）。
    """
    head = _read_head(paper)
    if head is None:
        return None
    try:
        result = await llm.complete(
            "librarian",
            [
                Message(role="system", content=AFFILIATIONS_SYSTEM_PROMPT),
                Message(role="user", content=f"论文标题：{paper.title}\n\n论文开头文本：\n{head}"),
            ],
            max_tokens=_MAX_TOKENS,
            user_id=user_id,
            project_id=project_id,
            library_id=library_id,
            voyage_id=voyage_id,
        )
        return _parse_affiliations(result.content)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — 机构解析尽力而为，失败由调用方兜底
        logger.warning("LLM affiliation extraction failed for paper %s", paper.id, exc_info=True)
        return None
