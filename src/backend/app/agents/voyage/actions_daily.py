"""每日新论文抓取动作（Voyage kind ``daily_feed_sync`` 的固定计划执行体）。

流水线（navigator.daily_feed_plan）：
    daily.fetch → daily.upsert → daily.cleanup → daily.embed → daily.sync_libraries

设计约定：
- 抓取/去重/清理全是确定性代码，不走 LLM；只有最后一步的向量化会花配额；
- 跨步骤传值走 ``ctx.checkpoint``（抓到的条目 / 本次涉及的论文 id），断点续跑时
  引擎会把 checkpoint 原样带回来；
- ``daily.fetch`` **任一分类抓失败即报错**（不是「全都空才报」）：部分失败会让当天
  那个分类的论文永久缺失，而全实验室的文献库都靠这个池供料；
- ``daily.sync_libraries`` 收尾时触发各库同步：池子备好了才同步，时刻不用猜；
- ``daily.embed`` 保持 best-effort：向量化是可选增强，失败不该让整次同步算失败。
"""

import datetime as dt
import logging
import uuid
from typing import Any

from app.agents.voyage.actions import ActionContext, register
from app.core.db import get_sessionmaker
from app.services import daily_feed as daily_feed_service

logger = logging.getLogger(__name__)


def _is_weekend(now: dt.datetime | None = None) -> bool:
    """今天 arXiv 本来就不公告吗。

    RSS 的 skipDays 明写着周六周日不发，那时拿到周五那批是正常的，不该报「抓早了」。
    单独成函数是为了能被测试固定住——直接读 now() 的话，同一条断言会随跑测试的星期
    时对时错（这条豁免上线后第一个周六就把测试挂了）。
    """
    return (now or dt.datetime.now(dt.UTC)).weekday() >= 5


# ---- 1. 抓取订阅分类的当天新公告 ----


@register("daily.fetch")
async def fetch(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        categories, by_category, statuses = await daily_feed_service.fetch_new_by_category(
            session
        )

    fetched = sum(s["count"] for s in statuses.values())
    failed = [c for c, s in statuses.items() if s["status"] == "error"]
    # arXiv 声明的批次日期不是今天 = 抓早了，拿到的是上一批。这种失败原本完全无声：
    # 条目照样几百条，只是全都昨天入过池了，去重后一条不进，而每一步都报成功。
    stale = (
        []
        if _is_weekend()
        else sorted({s["batch_date"] for s in statuses.values() if s.get("stale")})
    )
    for category, state in statuses.items():
        if state["status"] == "error":
            await ctx.log(f"{category}：抓取失败 —— {state['detail']}", level="error")
        else:
            await ctx.log(f"{category}：抓到 {state['count']} 篇")

    # 条目原样带给下一步（daily.upsert）：重抓一次既多打一遍 arXiv，也可能拿到不同结果
    ctx.checkpoint["daily_entries"] = by_category

    result: dict[str, Any] = {
        "categories": categories,
        "fetched": fetched,
        "per_category": statuses,
        "failed_categories": failed,
        "stale_batch_dates": stale,
    }
    if stale and not failed:
        # 抓早了，拿到的是上一批。这**不算失败**：按轮询语义，「今天那批还没发布」是正常
        # 状态——检查点会继续探（探满上限就当天收工），手动触发也可能落在发布之前。
        # 以前这里置 error，于是一次手动点击就留下一条 paused_error，而它其实什么也没坏。
        #
        # 但痕迹必须留下，否则又退回最初那个问题：条目照样几百条，全是昨天入过池的，
        # 去重后一条不进，而每一步都报成功。stale_batch_dates + 这行说明就是那道痕迹。
        result["note"] = (
            f"抓到的是 {'、'.join(stale)} 的公告，今天那批还没发布——本次不会有新论文入池。"
            "arXiv 每天约 04:00 UTC（北京 12:00）放当天批次，检查点会继续探。"
        )
        await ctx.log(result["note"], level="warning")
        return result
    if failed:
        # **任一分类失败就报错**，不是「全都空才报」。部分失败下当天那个分类的论文会
        # 永久缺失（RSS 只公告一次，每日池只留一周），而全实验室的文献库都靠这个池
        # 供料——静默残缺比整体失败更危险。
        result["error"] = (
            f"{len(failed)} 个分类抓取失败：{'、'.join(failed)}；"
            "当天这些分类的新论文不会进池，请重试"
        )
    elif categories and fetched == 0:
        # 全部分类都抓成功但一篇没有：周末/节假日是正常的，仍提示一句便于对照
        result["note"] = "所有订阅分类今天都没有新公告（周末或节假日时属正常）"
    return result


# ---- 2. 去重入池 + 建/合并推送条目 ----


@register("daily.upsert")
async def upsert(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    by_category = ctx.checkpoint.get("daily_entries") or {}
    async with get_sessionmaker()() as session:
        stats = await daily_feed_service.upsert_entries(session, by_category=by_category)

    touched = [str(pid) for pid in stats["touched"]]
    ctx.checkpoint["daily_touched_papers"] = touched
    # 交给 daily.sync_libraries 判断要不要触发库同步：一篇新的都没有就不必触发
    ctx.checkpoint["daily_created"] = stats["created"]
    ctx.checkpoint.pop("daily_entries", None)  # 已入池，不必再占着 checkpoint
    await ctx.log(f"新增 {stats['created']} 篇，合并分类 {stats['merged']} 篇")
    return {"created": stats["created"], "merged": stats["merged"], "papers": len(touched)}


# ---- 3. 清理过期推送（含无人收藏论文的回收） ----


@register("daily.cleanup")
async def cleanup(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        expired = await daily_feed_service.cleanup_expired(session)
        await session.commit()
    await ctx.log(f"清理过期推送 {expired} 条")
    return {"expired": expired}


# ---- 4. 建立语义向量 ----


@register("daily.embed")
async def embed(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    raw = ctx.checkpoint.get("daily_touched_papers") or []
    paper_ids = [uuid.UUID(str(pid)) for pid in raw]
    async with get_sessionmaker()() as session:
        stats = await daily_feed_service.embed_touched_papers(
            session, paper_ids=paper_ids, user_id=ctx.run.created_by
        )
    if not stats["enabled"]:
        await ctx.log("未开启自动建向量，跳过")
    else:
        await ctx.log(f"建立向量 {stats['embedded']} 篇")
    return stats


# ---- 5. 触发文献库同步 ----


@register("daily.sync_libraries")
async def sync_libraries(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    """新论文入池后立刻触发各文献库的同步；一篇新的都没有就不触发。

    以前库同步是自己定时跑的（抓取时刻 + 90 分钟），那是在赌抓取已经跑完——抓取慢一点
    或者失败重试，同步就会在旧池子上空跑一整轮，而界面上只显示「0 篇新论文」。改成
    事件驱动：池子备好了才同步，时刻不用猜也不用配。

    池里一篇新论文都没有时（周末、节假日、arXiv 当天没发）就不触发：14 个库各跑一轮
    打分/编译，扫的是同一批昨天就打过分的论文，白花一轮钱，还在每个库下面留一条
    「0 篇新论文」的任务记录。

    判据是 **created**（真正新进池的论文数），不是抓到的条目数：抓到几百条但全是昨天
    入过池的，去重后 created=0，对文献库来说和没抓一样。``merged`` 也不算——那是已在
    池中的论文多挂了一个分类，而库同步不按分类筛，没有新信号。
    """
    from app.core.queue import get_task_queue

    created = int(ctx.checkpoint.get("daily_created") or 0)
    if created <= 0:
        await ctx.log("池里没有新论文，跳过文献库同步", level="warning")
        return {"triggered": False, "created": 0, "reason": "no_new_papers"}

    queue = await get_task_queue()
    await queue.enqueue("daily_wiki_ingest")
    await ctx.log(f"已触发各文献库同步（新增 {created} 篇）")
    return {"triggered": True, "created": created}
