"""手动添加文献后的分阶段后台补全（下载→抽取→向量化→打分）。

同步请求只建元数据行（paper_import.create_pool_paper_stub）；重活在这里以后台
asyncio 任务跑，自开新 AsyncSession，按阶段向 redis 频道发进度事件供前端订阅。

阶段固定集合（前端按此渲染）：download → extract → embed → score。
（解析元数据在同步请求阶段已完成，不作为进度阶段单列。）
每阶段 best-effort：失败发 status="error" 但继续后续步骤；已就绪则 status="skipped"。
"""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import DirectionLibrary
from app.models.paper import Paper

logger = logging.getLogger(__name__)

# 前端按此固定顺序渲染进度条；事件 data.stage 取值于此
STAGES = ["download", "extract", "embed", "score"]

# 论文级向量的文本上限（字符）
EMBED_TEXT_MAX_CHARS = 2000

_OWNER_TTL_SECONDS = 600  # paper_task_owner 归属 key 存活时间


def paper_task_owner_key(task_id: str) -> str:
    return f"paper_task_owner:{task_id}"


# 已启动的后台任务引用（防止 asyncio 任务被 GC；也供测试 await 到完成）
_TASKS: dict[str, asyncio.Task] = {}

Emit = Callable[..., Awaitable[None]]


def paper_processing_complete(paper: Paper) -> bool:
    """论文是否已处理完整（PDF + 全文 + 向量都在）——完整则无需再启动补全任务。"""
    return bool(paper.pdf_path and paper.full_text_path and paper.embedding is not None)


def paper_embedding_text(paper: Paper) -> str:
    """论文级向量的统一文本口径：标题 + 作者名 + 摘要（截断 EMBED_TEXT_MAX_CHARS 字）。

    三处生成 Paper.embedding 的地方共用（手动添加补全、ingest 上链批量、每日池批量），
    保证同一批向量在同一口径下可比。作者名进文本是为了「找某人的工作」这类检索。
    """
    names: list[str] = []
    for item in paper.authors or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name and str(name).strip():
            names.append(str(name).strip())
    parts = [paper.title or "", ", ".join(names), paper.abstract or ""]
    return "\n".join(parts)[:EMBED_TEXT_MAX_CHARS]


async def embed_paper(
    paper: Paper,
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
) -> None:
    """为论文生成 Paper.embedding（文本口径见 paper_embedding_text）。

    provider 不支持嵌入时抛 NotImplementedError（调用方按 skipped 处理）。
    """
    from app.core.llm.router import get_llm_router

    vectors = await get_llm_router().embed(
        [paper_embedding_text(paper)],
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,
    )
    paper.embedding = vectors[0]


async def enrich_paper(
    session: AsyncSession,
    paper: Paper,
    *,
    target: DirectionLibrary | None = None,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    emit: Emit,
) -> None:
    """分阶段补全一篇论文（下载→抽取→(补机构)→向量化→打分）。

    - target 提供时按其 definition 打分（课题/库工作台），个人书架 import 无 target 跳过。
    - 每步 best-effort：抛错发 error 事件但继续；已就绪发 skipped。
    """
    from app.services.literature import get_arxiv_client
    from app.services.literature.pdf_extract import extract_full_text, save_pdf

    # 先固定 id：rollback 会让 ORM 对象过期，之后再同步读其属性会触发意外 IO
    paper_id = paper.id
    target_id = target.id if target is not None else None

    async def _rollback_and_reload() -> Paper:
        """回滚失败事务并重新取回附着的 paper（rollback 会过期原实例）。"""
        await session.rollback()
        return await session.get(Paper, paper_id)

    # 解析元数据（resolve）已在同步请求阶段完成，进度从「下载」起，不单列该阶段。

    # ---- download ----
    await emit("download", "running")
    if paper.pdf_path:
        await emit("download", "skipped", "already downloaded")
    elif not paper.arxiv_id:
        await emit("download", "skipped", "no arxiv id")
    else:
        try:
            content = await get_arxiv_client().download_pdf(paper.arxiv_id)
            paper.pdf_path = str(save_pdf(str(paper_id), content))
            await session.commit()
            await emit("download", "ok")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("enrich download failed for paper %s", paper_id, exc_info=True)
            paper = await _rollback_and_reload()
            await emit("download", "error", f"{type(e).__name__}: {e}")

    # ---- extract ----
    await emit("extract", "running")
    if paper.full_text_path:
        await emit("extract", "skipped", "already extracted")
    elif not paper.pdf_path:
        await emit("extract", "skipped", "no pdf")
    else:
        try:
            txt_path = await extract_full_text(str(paper_id), Path(paper.pdf_path))
            paper.full_text_path = str(txt_path)
            await session.commit()
            await emit("extract", "ok")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("enrich extract failed for paper %s", paper_id, exc_info=True)
            paper = await _rollback_and_reload()
            await emit("extract", "error", f"{type(e).__name__}: {e}")

    # 全文分块（文献问答底座）：抽到全文后就地建块，无块才切（避免重切丢已补的块向量）。
    # 不单列可见进度阶段（STAGES 保持 download/extract/embed/score 不变）；块向量按用户开关
    # 在下面 embed 阶段之后补。best-effort：失败记日志、回滚重取，不阻断后续阶段。
    if paper.full_text_path:
        from app.services.chunks import index_paper_fulltext, paper_has_chunks

        try:
            if not await paper_has_chunks(session, paper_id):
                await index_paper_fulltext(session, paper)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning(
                "enrich chunk indexing failed for paper %s", paper_id, exc_info=True
            )
            paper = await _rollback_and_reload()

    # 作者↔机构：on_add 模式下全文到手且尚无机构时 LLM 补（非独立阶段，best-effort，不发
    # 事件）；on_compile 模式跳过，改由 wiki 编译折叠抽取
    if not paper.affiliations and paper.full_text_path:
        try:
            from app.core.llm.router import get_llm_router
            from app.services.affiliations import (
                apply_author_affiliations,
                extract_author_affiliations_llm,
                get_affiliation_extraction_mode,
            )

            if await get_affiliation_extraction_mode(session) == "on_add":
                mapping = await extract_author_affiliations_llm(
                    paper, llm=get_llm_router(), user_id=user_id, project_id=project_id
                )
                if mapping and apply_author_affiliations(paper, mapping):
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("enrich affiliations failed for paper %s", paper_id, exc_info=True)
            paper = await _rollback_and_reload()

    # ---- embed ----
    await emit("embed", "running")
    if paper.embedding is not None:
        await emit("embed", "skipped", "already embedded")
    else:
        try:
            await embed_paper(
                paper,
                user_id=user_id,
                project_id=project_id,
                library_id=target_id,
            )
            await session.commit()
            await emit("embed", "ok")
        except asyncio.CancelledError:
            raise
        except NotImplementedError:
            paper = await _rollback_and_reload()
            await emit("embed", "skipped", "provider does not support embeddings")
        except Exception as e:  # noqa: BLE001
            logger.warning("enrich embed failed for paper %s", paper_id, exc_info=True)
            paper = await _rollback_and_reload()
            await emit("embed", "error", f"{type(e).__name__}: {e}")

    # 块向量：仅当用户开启全文索引开关时补（开关关只留块行，等重建/开开关再补）。
    # best-effort，不影响 embed 阶段判定；不发独立进度事件。
    from app.services.chunks import embed_pending_chunks_for_papers, user_wants_fulltext_index

    if await user_wants_fulltext_index(session, user_id):
        from app.core.llm.router import get_llm_router

        try:
            await embed_pending_chunks_for_papers(
                session,
                paper_ids=[paper_id],
                llm=get_llm_router(),
                user_id=user_id,
                project_id=project_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning(
                "enrich chunk embed failed for paper %s", paper_id, exc_info=True
            )
            paper = await _rollback_and_reload()

    # ---- score ----
    await emit("score", "running")
    if target_id is None:
        await emit("score", "skipped", "no target library")
    else:
        from app.services.libraries import get_membership
        from app.services.relevance import score_added_paper_best_effort

        membership = await get_membership(session, library_id=target_id, paper_id=paper_id)
        if membership is None:
            await emit("score", "skipped", "no membership")
        else:
            try:
                # best-effort helper 内部吞异常并自 commit/rollback，故用打分是否落地判定 ok/error
                await score_added_paper_best_effort(
                    session, paper, membership, project_id=project_id, user_id=user_id
                )
                await session.refresh(membership)
                if membership.relevance_score is not None:
                    await emit("score", "ok")
                else:
                    await emit("score", "error", "scoring produced no score")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("enrich score failed for paper %s", paper.id, exc_info=True)
                await emit("score", "error", f"{type(e).__name__}: {e}")


async def _run_enrichment(
    *,
    task_id: str,
    paper_id: uuid.UUID,
    library_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    redis: Redis,
) -> None:
    """后台任务体：自开新 session 跑 enrich_paper，收尾发 done / 出错发 error。"""
    from app.core.db import get_sessionmaker
    from app.core.events import EventBus, publish_paper_task_event

    bus = EventBus(redis)

    async def emit(stage: str, status: str, detail: str | None = None) -> None:
        await publish_paper_task_event(
            bus, task_id, "stage", {"stage": stage, "status": status, "detail": detail}
        )

    try:
        async with get_sessionmaker()() as session:
            paper = await session.get(Paper, paper_id)
            if paper is None:
                await publish_paper_task_event(
                    bus, task_id, "error", {"message": "paper not found"}
                )
                return
            target = (
                await session.get(DirectionLibrary, library_id) if library_id else None
            )
            await enrich_paper(
                session,
                paper,
                target=target,
                user_id=user_id,
                project_id=project_id,
                emit=emit,
            )
        await publish_paper_task_event(bus, task_id, "done", {})
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("paper enrichment task failed: %s", task_id)
        try:
            await publish_paper_task_event(
                bus, task_id, "error", {"message": f"{type(e).__name__}: {e}"}
            )
        except Exception:  # noqa: BLE001
            logger.warning("failed to publish paper task error event", exc_info=True)


async def launch_paper_enrichment(
    *,
    redis: Redis,
    paper_id: uuid.UUID,
    user_id: uuid.UUID,
    library_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
) -> str | None:
    """登记归属 + 起后台任务，返回 task_id；redis 不可用时降级返回 None（不阻塞添加）。"""
    task_id = uuid.uuid4().hex
    try:
        await redis.setex(paper_task_owner_key(task_id), _OWNER_TTL_SECONDS, str(user_id))
    except Exception:  # noqa: BLE001 — redis 不可达时进度追踪不可用，但添加本身已成功
        logger.warning("paper task owner registration failed; enrichment not launched")
        return None

    task = asyncio.create_task(
        _run_enrichment(
            task_id=task_id,
            paper_id=paper_id,
            library_id=library_id,
            user_id=user_id,
            project_id=project_id,
            redis=redis,
        )
    )
    _TASKS[task_id] = task
    task.add_done_callback(lambda t: _TASKS.pop(task_id, None))
    return task_id


async def await_task(task_id: str) -> None:
    """等待某后台任务跑完（测试用；生产不需要）。"""
    task = _TASKS.get(task_id)
    if task is not None:
        await task


async def owner_of(redis: Redis, task_id: str) -> str | None:
    """取任务归属用户 id（字符串），无则 None。"""
    return await redis.get(paper_task_owner_key(task_id))
