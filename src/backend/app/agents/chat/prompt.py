"""助手的系统提示与工具定义。

**system prompt 必须是稳定前缀**：每轮重发的历史 + 工具 schema 加起来是输入 token 的
大头，指望 prompt caching 就不能每轮换写法、换工具顺序。所以这里是纯拼接、无随机、
无时间戳之外的变量，且技能目录的槽位预留在末尾——技能正文只能作为工具结果追加，
写进 system 会作废整个缓存前缀。
"""

import datetime as dt
from typing import Any

from app.tools.registry import list_tools

#: 首期给助手的工具白名单。**不要一上来给 38 个**：schema 每轮重发既贵，又让模型
#: 在相近的工具之间反复犹豫（search_papers / search_chunks / global_search 尤其像）。
DEFAULT_TOOL_NAMES: tuple[str, ...] = (
    "search_chunks",
    "search_papers",
    "get_paper",
    "read_fulltext",
    "list_concepts",
    "get_concept",
    # 渐进披露：目录常驻，正文按需取
    "skill_load",
    "skill_read_file",
    # 多步任务把计划摆出来，用户看得见你打算怎么做、做到哪儿了
    "update_plan",
)

_SYSTEM = """\
你是 Polaris 科研平台的助手。你可以调用工具去平台里查东西，而不是只凭上下文作答。

工作方式：
- 需要事实时先查再答。检索工具很便宜，宁可多查一次，也不要猜；
- 一次可以并行发起多个工具调用（比如同时查两个不同的关键词）；
- 任务要三步以上时先用 update_plan 把计划摆出来，每推进一步就更新它；
  一步能答完的问题不要排计划，那只是噪音；
- 工具返回空结果不代表没有——换个说法或换个工具再试一次，仍然没有就如实说没查到；
- 引用某篇论文的内容时在句末标注它的编号，如 [1]；
- 用中文回答，讲清楚、说人话，不要罗列工具调用过程。

今天是 {today}（UTC）。资料里的论文都标了发布日期，问到「最近」时按它判断。
{statement}{extra}"""


def build_system_prompt(
    statement: str | None = None, extra: str = "", skill_catalog: str = ""
) -> str:
    """组装系统提示。

    ``statement``（研究方向）、``skill_catalog``（技能目录）与 ``extra`` 都追加在末尾，
    前面那段是不变的稳定前缀——顺序反过来会让每个作用域都有各自的缓存前缀，
    命中率归零。

    **技能正文永远不进这里**：目录里每个技能只占一行，正文由 ``skill_load`` 作为工具
    结果追加。把正文写进 system prompt 会作废整个缓存前缀，而它是每轮都要重发的。
    """
    direction = f"\n\n研究方向：{statement.strip()}" if statement and statement.strip() else ""
    catalog = f"\n\n{skill_catalog.strip()}" if skill_catalog and skill_catalog.strip() else ""
    tail = f"\n\n{extra.strip()}" if extra and extra.strip() else ""
    return _SYSTEM.format(
        today=dt.datetime.now(dt.UTC).date().isoformat(),
        statement=direction,
        extra=catalog + tail,
    )


def tool_definitions(names: tuple[str, ...] | list[str] | None = None) -> list[dict[str, Any]]:
    """工具定义。``ToolSpec.input_schema`` 本来就是标准 JSON Schema，零转换。

    顺序跟随 ``list_tools`` 给定的顺序（它保序），这样工具块也是稳定前缀的一部分。
    """
    wanted = list(names) if names else list(DEFAULT_TOOL_NAMES)
    return [
        {"name": spec.name, "description": spec.description, "parameters": spec.input_schema}
        for spec in list_tools(wanted)
    ]
