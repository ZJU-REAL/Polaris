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
#: 助手默认拿不到的工具。**默认给全部只读工具**，这里只列例外。
#:
#: 反过来（白名单）曾经是默认：只给 6 个检索工具，理由是「38 个 schema 每轮重发既贵
#: 又让模型选择困难」。实测下来那个判断错在两处：一是代价被高估了——全部只读工具的
#: schema 加起来约 12.6k 字符（≈3.2k tokens），一轮问答的历史往往比这更大；二是代价
#: 算错了地方——模型拿不到取图工具时，不会「省下一次调用」，而是**假装看过图**或者
#: 干脆说做不到，用户为此付的代价远超几千 token。
#:
#: 例外只有一个：get_fact_pack 一次返回整包事实，动辄几万字符，模型几乎总能用更
#: 便宜的工具问到同样的东西。需要它的场景由技能显式声明。
#: submit_plan 只在计划模式给（见 tools_for_mode）：别的模式下它没有意义——
#: 模型会拿它当"我说完了"的花式说法，把一轮好好的回答变成一份待审的计划。
_TOOL_DENYLIST = frozenset({"get_fact_pack", "submit_plan"})


def default_tool_names(*, memory_enabled: bool = False) -> tuple[str, ...]:
    """这一轮默认给哪些工具：注册表里全部只读的，减去 denylist。

    动态取而不是写死一份名单：新工具加进注册表就自动可用。写死名单的问题不是麻烦，
    而是**没人会记得回来加**——助手会悄悄少一项能力，而界面上看不出任何异常。
    """
    from app.tools.memory import MEMORY_TOOL_NAMES
    from app.tools.registry import list_tools

    names = []
    for spec in list_tools():
        if spec.name in _TOOL_DENYLIST:
            continue
        if spec.name in MEMORY_TOOL_NAMES:
            # 记忆没打开时这两个工具根本不进工具面：模型不知道有这回事，
            # 也就不会假装记住——「我记下了」而其实没存，比不能记更糟。
            if memory_enabled:
                names.append(spec.name)
            continue
        if spec.read_only:
            names.append(spec.name)
    return tuple(names)


#: 兼容旧引用（测试与 API 都在用这个名字）。求值时机在 import 之后，工具已注册完毕。
def __getattr__(name: str) -> object:
    if name == "DEFAULT_TOOL_NAMES":
        return default_tool_names()
    raise AttributeError(name)


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
    wanted = list(names) if names else list(default_tool_names())
    return [
        {"name": spec.name, "description": spec.description, "parameters": spec.input_schema}
        for spec in list_tools(wanted)
    ]


def tools_for_mode(names: tuple[str, ...], mode: str) -> tuple[str, ...]:
    """按模式调整工具面。

    计划模式多一个 ``submit_plan``——它是这个模式的**出口**：调研完把方案交出去，
    这一轮就结束，等人点头。没有它，"只调研不动手"就只是一句叮嘱，模型说完
    "以上是我的计划，可以吗"就没有下文了，界面也无从画出批准按钮。
    """
    if mode == "plan" and "submit_plan" not in names:
        return (*names, "submit_plan")
    return names


#: 三种模式。默认那种（``chat``）行为不变。
CHAT_MODES = ("chat", "plan", "goal")

#: 计划模式的附加指令。照搬 Claude Code 的 plan mode：**只调研、不动手**，先把方案
#: 摆出来等人点头。它的价值不在「多一个开关」，而在于把「我以为你要的是 A」这个
#: 误会提前到花钱之前——研究任务里改错方向的代价是几个小时，不是几秒。
_PLAN_MODE = """\
【计划模式】这一轮**只做调研和规划，不动手执行**。

1. 先用工具把事实查清楚——不了解现状就排出来的计划是猜的；
2. 想清楚打算怎么做之后，调 **submit_plan** 把计划交出去，这一轮就结束；
3. 计划要具体到「每一步做什么、产出什么」，而不是「分析问题、给出结论」这种废话；
4. rationale 里写明为什么这么做、哪里可能不成立。

用户点头之后，你会在下一轮拿到这份计划，**那时才动手**。所以这一轮不要写最终答案，
也不要在交出计划之后接着干活。"""

#: 目标模式的附加指令。目标随会话存着，每轮都带上——用户不必每次重述，模型也不会
#: 在第三轮忘了最初要干什么。
_GOAL_MODE = """\
【目标模式】这场对话有一个持续目标：{goal}

每一轮都要：先说这一轮把目标推进了多少，再说下一步做什么。已经达成的部分不要重复
汇报；卡住了就直说卡在哪、需要什么才能继续——假装有进展比停下来更浪费时间。

**自己往前走**：能用工具查清楚的就去查，别把「你希望我先看哪个」这种问题抛回来。
只有在真的需要用户做决定时才停下来问——方向选择、要不要花钱、以及任何改动数据的事。
目标达成了就明说达成了，不要为了显得还在干活而硬找下一步。"""


def mode_instructions(mode: str, *, goal: str = "") -> str:
    """模式附加指令；``chat`` 返回空串（默认行为一个字都不变）。"""
    if mode == "plan":
        return _PLAN_MODE
    if mode == "goal" and goal.strip():
        return _GOAL_MODE.format(goal=goal.strip())
    return ""
