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

    def as_dict(self) -> dict[str, int]:
        return {
            "saved_recent": self.saved_recent,
            "saved_total": self.saved_total,
            "ideas_recent": self.ideas_recent,
            "experiments_running": self.experiments_running,
            "daily_today": self.daily_today,
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
    return BuddyStats(
        saved_recent=saved_recent,
        saved_total=saved_total,
        ideas_recent=ideas_recent,
        experiments_running=experiments_running,
        daily_today=daily_today,
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
        return f"{who}今天新到了 {stats.daily_today} 篇论文。要我先替你筛一遍吗？"
    if stats.saved_total:
        return f"{who}你的库里已经有 {stats.saved_total} 篇论文了。今天想从哪儿看起？"
    return f"{who}我是 PolarisBuddy。你在平台上做的事我都能帮上手——先从一个问题开始吧。"


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
        return f"今天到了 {stats.daily_today} 篇新论文，要我先替你筛一遍吗？"
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


async def add_memory(session: AsyncSession, *, user_id: uuid.UUID, text: str) -> BuddyMemory:
    row = BuddyMemory(user_id=user_id, text=text.strip()[:MAX_MEMORY_ITEM_CHARS])
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


async def render_memories(session: AsyncSession, *, user_id: uuid.UUID) -> str:
    """记忆 → 追加进系统提示的一段；没有记忆返回空串。

    **总长封顶**并且按新到旧填：每轮都要重发，放任下去就是每轮都在为一堆旧便签付钱。
    填不下的直接不进——与其截断成半句话让模型猜，不如少给一条。
    """
    rows = await list_memories(session, user_id=user_id)
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
