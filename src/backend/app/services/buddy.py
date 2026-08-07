"""PolarisBuddy 的陪伴层：问候语与页面上下文。

两条纪律：

1. **问候语不过模型**。它每次开面板都要出现，过一次 LLM 就是每次开面板都花钱、
   还得等两秒；更要命的是模型会把数字说错——"你这周读了 12 篇"必须真的是 12 篇。
   所以数字全部来自 SQL，句子从数字里挑，一个字都不生成。
2. **数字取不到就不说这句话**，不要兜 0 冒充。"你这周还没读论文"和"统计失败"
   在界面上长得一样，但对用户是两回事。
"""

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.buddy_memory import BuddyMemory
from app.models.daily_feed import DailyFeedEntry
from app.models.experiment import Experiment
from app.models.idea import Idea
from app.models.library import UserLibraryEntry
from app.models.manuscript import Manuscript
from app.models.paper import PaperUserMeta
from app.models.project import ProjectMember

#: 「最近」的口径。七天是一周工作节奏，比"今天"稳（周一看不到上周五的成果会很挫）。
RECENT_DAYS = 7

#: 页面上下文注入的长度上限。它是前端声明的，不能无限长地进提示词。
MAX_CONTEXT_CHARS = 200


@dataclass(slots=True)
class BuddyStats:
    """全部来自 SQL 的真实计数。"""

    saved_recent: int  # 最近 7 天收藏进个人库的论文（saved=false 是浏览记录，不算）
    saved_total: int
    ideas_recent: int  # 最近 7 天在自己参与的课题里新增的想法
    experiments_running: int
    daily_today: int  # 今天的新论文（全平台）
    reading_now: int  # 标了「在读」还没读完的论文——搁在半路的事最值得被问一句
    manuscripts_active: int  # 自己课题下在写的稿子
    topics: int  # 参与的课题数；0 = 还没开张，问候语要走冷启动那条

    def as_dict(self) -> dict[str, int]:
        return {
            "saved_recent": self.saved_recent,
            "saved_total": self.saved_total,
            "ideas_recent": self.ideas_recent,
            "experiments_running": self.experiments_running,
            "daily_today": self.daily_today,
            "reading_now": self.reading_now,
            "manuscripts_active": self.manuscripts_active,
            "topics": self.topics,
        }


async def collect_stats(session: AsyncSession, *, user_id: uuid.UUID) -> BuddyStats:
    """用户自己的近况。只统计他真的参与的课题——别人课题的想法数与他无关。"""
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=RECENT_DAYS)
    my_projects = select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)

    async def count(stmt: Any) -> int:
        return int((await session.execute(stmt)).scalar_one() or 0)

    saved_recent = await count(
        select(func.count())
        .select_from(UserLibraryEntry)
        .where(
            UserLibraryEntry.user_id == user_id,
            UserLibraryEntry.saved.is_(True),
            UserLibraryEntry.trashed_at.is_(None),
            UserLibraryEntry.created_at >= since,
        )
    )
    saved_total = await count(
        select(func.count())
        .select_from(UserLibraryEntry)
        .where(
            UserLibraryEntry.user_id == user_id,
            UserLibraryEntry.saved.is_(True),
            UserLibraryEntry.trashed_at.is_(None),
        )
    )
    ideas_recent = await count(
        select(func.count())
        .select_from(Idea)
        .where(
            Idea.project_id.in_(my_projects),
            Idea.created_at >= since,
            Idea.trashed_at.is_(None),
        )
    )
    experiments_running = await count(
        select(func.count())
        .select_from(Experiment)
        .where(
            Experiment.project_id.in_(my_projects),
            Experiment.status == "running",
            Experiment.trashed_at.is_(None),
        )
    )
    daily_today = await count(
        select(func.count())
        .select_from(DailyFeedEntry)
        .where(DailyFeedEntry.feed_date == dt.datetime.now(dt.UTC).date())
    )
    reading_now = await count(
        select(func.count())
        .select_from(PaperUserMeta)
        .where(PaperUserMeta.user_id == user_id, PaperUserMeta.reading_status == "reading")
    )
    manuscripts_active = await count(
        select(func.count())
        .select_from(Manuscript)
        .where(Manuscript.project_id.in_(my_projects), Manuscript.trashed_at.is_(None))
    )
    topics = await count(
        select(func.count()).select_from(ProjectMember).where(ProjectMember.user_id == user_id)
    )
    return BuddyStats(
        saved_recent=saved_recent,
        saved_total=saved_total,
        ideas_recent=ideas_recent,
        experiments_running=experiments_running,
        daily_today=daily_today,
        reading_now=reading_now,
        manuscripts_active=manuscripts_active,
        topics=topics,
    )


def compose_greeting(stats: BuddyStats, *, name: str | None = None) -> str:
    """从计数里挑一句话说。挑，不是生成——数字必须对得上。

    优先级是「他现在最可能关心的事」：跑着的实验 > 本周的积累 > 今天的新论文 >
    什么都没有时的开场白。一次只说一件事，问候语不是仪表盘。
    """
    who = f"{name}，" if name else ""
    if stats.experiments_running:
        return (
            f"{who}你有 {stats.experiments_running} 个实验正在跑。"
            "要我帮你看看进展，或者一起理下一步吗？"
        )
    if stats.ideas_recent:
        return f"{who}这周你已经攒了 {stats.ideas_recent} 个新想法。想挑一个往下推推吗？"
    if stats.saved_recent:
        return (
            f"{who}这周你收了 {stats.saved_recent} 篇论文进库。"
            "要我帮你把它们串一串，看看有没有共同的线索？"
        )
    if stats.daily_today:
        # 这里曾经报「今天新到了 N 篇论文，要我先替你筛一遍吗」。两个毛病：N 每天在变
        # 却不帮人做任何决定；而「替你筛一遍」是张兑现不了的支票——筛论文是每日池
        # 打分收录那条流水线的活，一轮对话干不了，答应了只能敷衍。
        # 有新论文的日子就正常打个招呼，把话头让给用户。
        return f"{who}今天有新论文进来了。想从哪儿看起？"
    if stats.saved_total:
        return f"{who}你的库里已经有 {stats.saved_total} 篇论文了。今天想从哪儿看起？"
    return f"{who}我是 PolarisBuddy。你在平台上做的事我都能帮上手——先从一个问题开始吧。"


#: 开场：一句主动的问话 + 三张卡片。
#:
#: 卡片是两段式的：卡面只放**摘要**（三五个字，一眼扫完），点下去进输入框的是
#: **完整的问题**——有头有尾、带上下文、能直接问出口的那种。这两件事分开写，是因为
#: 它们的读者不同：卡面给眼睛扫，问题给模型读。挤成一句的结果是卡片上一行长句折成
#: 三行，或者进了输入框的话干巴巴没有上下文。
#:
#: **仍然不过模型**（见文件头第 1 条纪律），而且现在多了一条硬理由：只读的演示账号
#: 根本调不动模型，开场白要是走 LLM，公开演示上会直接空掉。
#:
#: 「智能」来自信号的覆盖与排序，不是来自生成：先看他此刻正在看什么（页面上下文是
#: 最强的信号——他人就在那儿），看不出名堂再退回他自己的近况。一次只问一件事。
Card = dict[str, str]


def _card(summary: str, prompt: str) -> Card:
    return {"summary": summary, "prompt": prompt}


def compose_opening(stats: BuddyStats, *, page_kind: str | None = None) -> tuple[str, list[Card]]:
    """返回 (问句, 三张卡片)。卡片的 prompt 是**用户说的话**，点了就当他这么问。"""
    # —— 他正在看的东西优先：此刻在屏幕上的那件事，比七天前的统计切题得多 ——
    by_page: dict[str, tuple[str, list[Card]]] = {
        "paper": (
            "在读这篇？我可以帮你拆开看看。",
            [
                _card(
                    "讲清这篇",
                    "我正在读这篇论文，帮我讲清楚它要解决的是什么问题、为什么这个问题值得做。",
                ),
                _card(
                    "和已有工作比",
                    "把这篇的方法和已有工作对比一下：它到底改了什么，这个改动为什么有用。",
                ),
                _card(
                    "证据够不够",
                    "这篇的实验证据够不够强？有哪些该做没做的对照，结论有没有被过度延伸。",
                ),
            ],
        ),
        "idea": (
            "这个想法要往下推吗？",
            [
                _card(
                    "有人做过吗",
                    "帮我查一下这个想法有没有人做过：找出最接近的几篇工作，说说它们各自做到哪一步。",
                ),
                _card(
                    "最弱的一环", "这个想法最站不住的地方在哪？把最可能被审稿人问倒的那一点指出来。"
                ),
                _card(
                    "第一步做什么",
                    "把这个想法拆成可以动手的步骤，告诉我第一步该做什么、需要什么条件。",
                ),
            ],
        ),
        "experiment": (
            "这个实验现在怎么样？",
            [
                _card("跑到哪了", "这个实验现在跑到哪一步了？把已有的结果讲一下，它们说明了什么。"),
                _card("结果可信吗", "这个实验的结果可信吗？有没有哪些设置或指标会让结论站不住。"),
                _card(
                    "下一轮改什么", "如果要再跑一轮，该改哪个变量、为什么改它，预期会看到什么变化。"
                ),
            ],
        ),
        "manuscript": (
            "在写这篇？我可以搭把手。",
            [
                _card("这节通不通", "帮我看看我正在写的这一节：论证链条通不通，哪里跳步了。"),
                _card("还缺哪些引用", "对照这篇稿子的主题，相关工作里还缺哪些该引而未引的文献。"),
                _card("改紧凑些", "把这段改得更紧凑一些：留住意思，去掉绕弯子的地方。"),
            ],
        ),
        "library": (
            "这个库里想找什么？",
            [
                _card(
                    "最近进了什么", "这个文献库最近进了哪些论文？挑出值得优先读的几篇并说明理由。"
                ),
                _card(
                    "按主题归类", "把这个库里的论文按主题归归类，让我知道它大致覆盖了哪几个方向。"
                ),
                _card("共同的思路", "这个库里的工作有哪些反复出现的思路或者共用的技术路线？"),
            ],
        ),
        "project": (
            "这个课题接下来做什么？",
            [
                _card("现在到哪了", "帮我理一下这个课题目前的进展：想法、实验、稿子各到什么阶段。"),
                _card("还有什么空档", "这个课题所在的方向上还有哪些没被做过的空档，值得我去占。"),
                _card("下一步做什么", "综合现在的进展，下一步最该动手的是什么？说明为什么是它。"),
            ],
        ),
        "daily": (
            "今天的新论文，要我帮你挑吗？",
            [
                _card(
                    "挑相关的", "从今天的新论文里挑出和我的研究方向相关的几篇，并说明为什么相关。"
                ),
                _card("今天值得读的", "今天这批新论文里有哪些值得一读？给我理由，别只给标题。"),
                _card("按主题归类", "把今天的新论文按主题归归类，让我快速知道今天大致发生了什么。"),
            ],
        ),
    }
    if page_kind and page_kind in by_page:
        return by_page[page_kind]

    # —— 看不出他在看什么，就退回他自己的近况：搁在半路的事排在前面 ——
    if stats.experiments_running:
        return (
            "有实验在跑，要看看进展吗？",
            [
                _card("跑到哪了", "我现在有实验在跑，帮我看看各自跑到哪一步了、结果说明什么。"),
                _card(
                    "有没有问题", "检查一下在跑的实验有没有异常：指标不动、报错、或者设置上的坑。"
                ),
                _card("下一步做什么", "根据现在的结果，下一步该做什么？要不要调整设置再跑一轮。"),
            ],
        )
    if stats.manuscripts_active:
        return (
            "稿子写到哪了？",
            [
                _card("现在的问题", "帮我看看在写的这篇稿子现在最大的问题是什么。"),
                _card("还缺哪些引用", "对照稿子的主题，相关工作里还缺哪些该引而未引的文献。"),
                _card("接下来补哪节", "接下来最该补的是哪一节？说明为什么是它。"),
            ],
        )
    if stats.reading_now:
        return (
            "有几篇还在读，要接着看吗？",
            [
                _card("串一串", "把我在读的这几篇串一串：它们各自在回答什么问题，彼此什么关系。"),
                _card("共同点", "我在读的这几篇有什么共同的思路或者共用的方法？"),
                _card("挑一篇细讲", "从我在读的这几篇里挑一篇最值得细读的，讲清楚它做了什么。"),
            ],
        )
    if stats.ideas_recent:
        return (
            "最近攒了些想法，挑一个推推？",
            [
                _card("哪个值得做", "我最近攒了一些想法，帮我看看哪个最值得先做，理由是什么。"),
                _card("有人做过吗", "帮我查查这些想法里有没有已经被人做过的，找出最接近的工作。"),
                _card("拆成第一步", "挑一个想法，把它拆成可以动手的步骤，告诉我第一步做什么。"),
            ],
        )
    if stats.saved_recent:
        return (
            "这周收了些论文，要串一串吗？",
            [
                _card(
                    "共同的线索", "把我这周收的论文串一串，看看有没有共同的线索或者反复出现的问题。"
                ),
                _card("按主题归类", "把我这周收的论文按主题归归类，让我知道自己在关注什么。"),
                _card("哪篇最该细读", "我这周收的论文里，哪一篇最值得花时间细读？给我理由。"),
            ],
        )
    if stats.daily_today:
        return (
            "今天有新论文进来了，想看看吗？",
            [
                _card(
                    "挑相关的", "从今天的新论文里挑出和我的研究方向相关的几篇，并说明为什么相关。"
                ),
                _card("今天值得读的", "今天这批新论文里有哪些值得一读？给我理由，别只给标题。"),
                _card("按主题归类", "把今天的新论文按主题归归类，让我快速知道今天大致发生了什么。"),
            ],
        )
    if stats.saved_total:
        return (
            "今天想从哪儿看起？",
            [
                _card("库里值得重读的", "我的文献库里有哪些值得重读的论文？挑几篇并说明理由。"),
                _card(
                    "找某个方向",
                    "我想找某个方向的论文——先问我是哪个方向，再铺开看看有哪些代表工作。",
                ),
                _card("这个方向的新进展", "我关注的方向最近有什么新进展？把值得注意的几篇讲一讲。"),
            ],
        )
    # —— 全新账号：别问他"你的实验怎么样"，他什么都还没有 ——
    return (
        "想从哪儿开始？",
        [
            _card(
                "找某个方向", "我想找某个方向的论文——先问我是哪个方向，再铺开看看有哪些代表工作。"
            ),
            _card("最近有什么新的", "最近有哪些值得读的新论文？挑几篇讲讲它们在做什么。"),
            _card("你能做什么", "介绍一下你能帮我做什么，按我在平台上会用到的场景说。"),
        ],
    )


def compose_nudge(stats: BuddyStats) -> str | None:
    """今天值得**主动**说的一句话；没有真事就返回 None。

    与 :func:`compose_greeting` 的区别是门槛：问候语是用户自己点开面板时说的，
    什么都没有也该有句开场白；主动提示是敲一下肩膀，**没有新东西就不该敲**。
    为了让悬浮球看起来「活着」而常亮的红点，是在教用户忽略它。

    句子和问候语一样从计数里挑——措辞留在这一处，前端只负责「今天提过没」。
    """
    if stats.experiments_running:
        return f"你有 {stats.experiments_running} 个实验在跑，要看看进展吗？"
    if stats.daily_today:
        # 主动提示不能是「打个招呼」——敲肩膀是为了报事，不是问好。所以这里保留数字
        # （它就是那件事本身），只去掉同样兑现不了的「替你筛一遍」。
        return f"今天到了 {stats.daily_today} 篇新论文。"
    return None


#: 页面类型 → 说给模型听的一句话。键与前端 `buddyContext.ts` 的 kind 一一对应。
_CONTEXT_LABELS = {
    "paper": "用户正在读一篇论文",
    "idea": "用户正在看一个研究想法",
    "experiment": "用户正在看一个实验",
    "library": "用户正在浏览一个文献库",
    "project": "用户正在课题工作台",
    "manuscript": "用户正在写论文稿",
    "daily": "用户正在看每日新论文列表",
}


def render_page_context(kind: str | None, obj_id: str | None) -> str:
    """把前端声明的页面上下文渲染成一行注入文本；认不出就返回空串。

    上下文是**线索不是授权**：这里只说"用户在看什么"，Buddy 要真去读内容还得调
    工具，那条路上的权限校验一点没少。所以前端伪造 id 最多让 Buddy 查一篇它本来
    就有权查的东西。
    """
    label = _CONTEXT_LABELS.get((kind or "").strip())
    if not label:
        return ""
    line = label if not obj_id else f"{label}（id: {obj_id}）"
    return f"[当前页面] {line}。回答时优先考虑这个上下文；用户说「这篇」「这个」多半指它。"[
        :MAX_CONTEXT_CHARS
    ]


#: 记忆注入提示词的总长上限。它每轮都要重发——放任下去就是每轮都在为一堆旧便签付钱。
MAX_MEMORY_CHARS = 1200

#: 单条记忆的长度上限。一条写成一篇文章的记忆，模型多半只会记住开头。
MAX_MEMORY_ITEM_CHARS = 300


async def list_memories(session: AsyncSession, *, user_id: uuid.UUID) -> list[BuddyMemory]:
    """用户的长期记忆，新的在前。"""
    rows = await session.execute(
        select(BuddyMemory)
        .where(BuddyMemory.user_id == user_id)
        .order_by(BuddyMemory.created_at.desc())
    )
    return list(rows.scalars().all())


async def add_memory(
    session: AsyncSession, *, user_id: uuid.UUID, text: str, kind: str = "fact"
) -> BuddyMemory:
    row = BuddyMemory(
        user_id=user_id, text=text.strip()[:MAX_MEMORY_ITEM_CHARS], kind=kind or "fact"
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def delete_memory(session: AsyncSession, *, user_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
    row = await session.get(BuddyMemory, memory_id)
    if row is None or row.user_id != user_id:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def search_memories(
    session: AsyncSession, *, user_id: uuid.UUID, query: str, limit: int
) -> list[BuddyMemory]:
    """在记忆里找。空查询返回最近的几条——「我上次说了什么」本来就没有关键词。

    用 ilike 而不是向量：记忆是几十条量级的短句，为它建一套向量管线的收益还不如
    诚实的字面匹配；等它真的多到检索不动了再说。
    """
    stmt = select(BuddyMemory).where(BuddyMemory.user_id == user_id)
    if query.strip():
        stmt = stmt.where(BuddyMemory.text.ilike(f"%{query.strip()}%"))
    rows = await session.execute(stmt.order_by(BuddyMemory.created_at.desc()).limit(limit))
    return list(rows.scalars().all())


async def render_memories(session: AsyncSession, *, user_id: uuid.UUID) -> str:
    """记忆 → 追加进系统提示的一段；没有记忆返回空串。

    **总长封顶**并且按新到旧填：每轮都要重发，放任下去就是每轮都在为一堆旧便签付钱。
    填不下的直接不进——与其截断成半句话让模型猜，不如少给一条。
    """
    # 只有 fact 进提示词：note 是带时间戳的片段，检索到才回上下文，
    # 每轮都带上就是每轮为陈年琐事付钱
    rows = [r for r in await list_memories(session, user_id=user_id) if r.kind != "note"]
    if not rows:
        return ""
    lines: list[str] = []
    used = 0
    for row in rows:
        line = f"- {row.text.strip()}"
        if used + len(line) > MAX_MEMORY_CHARS:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return "关于这位用户，你需要一直记得：\n" + "\n".join(lines)


#: 记忆开关存在用户设置里的键。不值得为一个布尔值开一张表。
MEMORY_ENABLED_KEY = "buddy_memory_enabled"


def memory_enabled(user: Any) -> bool:
    """这个人开了记忆吗。**默认关**：一个会自己记东西的助手，得由用户先说「可以」。"""
    settings = getattr(user, "settings", None) or {}
    return bool(settings.get(MEMORY_ENABLED_KEY, False))


async def set_memory_enabled(session: AsyncSession, *, user: Any, enabled: bool) -> bool:
    settings = dict(getattr(user, "settings", None) or {})
    settings[MEMORY_ENABLED_KEY] = bool(enabled)
    user.settings = settings
    await session.commit()
    return bool(enabled)
