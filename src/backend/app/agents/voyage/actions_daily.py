"""每日新论文抓取动作（Voyage kind ``daily_feed_sync`` 的固定计划执行体）。

流水线（navigator.daily_feed_plan）：
    daily.fetch → daily.upsert → daily.cleanup → daily.embed

设计约定：
- 抓取/去重/清理全是确定性代码，不走 LLM；只有最后一步的向量化会花配额；
- 跨步骤传值走 ``ctx.checkpoint``（抓到的条目 / 本次涉及的论文 id），断点续跑时
  引擎会把 checkpoint 原样带回来；
- ``daily.fetch`` 全分类颗粒无收即报错：arXiv 客户端把网络/解析失败兜底成 []
  （见 services/literature/arxiv.py），以前这类失败被整个吞掉，纳入任务系统后
  让这一步失败 → 任务进 paused_error，列表看得见、日志查得到、可以重试；
- ``daily.embed`` 保持 best-effort：向量化是可选增强，失败不该让整次同步算失败。
"""

import logging
import uuid
from typing import Any

from app.agents.voyage.actions import ActionContext, register
from app.core.db import get_sessionmaker
from app.services import daily_feed as daily_feed_service

logger = logging.getLogger(__name__)


# ---- 1. 抓取订阅分类的当天新公告 ----


@register("daily.fetch")
async def fetch(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        categories, by_category, statuses = await daily_feed_service.fetch_new_by_category(
            session
        )

    fetched = sum(s["count"] for s in statuses.values())
    failed = [c for c, s in statuses.items() if s["status"] == "error"]
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
    }
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


# ---- 4. 建立语义向量（管理员开关，默认关） ----


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
