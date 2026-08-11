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
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import active_space
from app.models.library_direction import DirectionLibrary
from app.models.paper import Paper

logger = logging.getLogger(__name__)

# 前端按此固定顺序渲染进度条；事件 data.stage 取值于此
STAGES = ["download", "extract", "embed", "score"]

# 论文级向量的文本上限（字符）
EMBED_TEXT_MAX_CHARS = 2000

# 论文级向量的平台总闸（管理员开关，**默认开**）：关掉后所有入库入口都不建论文级向量。
# 前身是 daily_feed_embed_enabled（默认关、只管每日推送那一条路径）——默认关意味着
# 推送来的论文在语义检索里根本搜不到，而只管一条路径又名不副实。旧键的存量值不迁移，
# 读不到就取新默认（开）。

_OWNER_TTL_SECONDS = 3600  # 批量任务可能较久；归属 key 与事件回放保留 1 小时

#: 批量导入时限制 PDF/向量/LLM 并发。**按事件循环各持一个**，不能做成模块级单例：
#: ``asyncio.Semaphore`` 一旦真的被争用过就绑死在那个循环上，换一个循环再用会抛
#: ``RuntimeError: ... is bound to a different event loop``。生产是单循环，碰不到；
#: 测试每个用例一个新循环，于是「先跑一个会争用的批量导入，再跑第二个」必炸——而且
#: 炸的是后一个用例，看起来像它自己的毛病。已实测复现：同一个信号量在第二个
#: ``asyncio.run`` 里争用即抛。
_ENRICH_CONCURRENCY = 3
_ENRICH_SEMAPHORES: "weakref.WeakKeyDictionary[Any, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _enrich_semaphore() -> asyncio.Semaphore:
    """当前事件循环的补全并发闸。循环消失后条目自动回收。"""
    loop = asyncio.get_running_loop()
    sem = _ENRICH_SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_ENRICH_CONCURRENCY)
        _ENRICH_SEMAPHORES[loop] = sem
    return sem


def paper_task_owner_key(task_id: str) -> str:
    return f"paper_task_owner:{task_id}"


# 已启动的后台任务引用（防止 asyncio 任务被 GC；也供测试 await 到完成）
_TASKS: dict[str, asyncio.Task] = {}

Emit = Callable[..., Awaitable[None]]


async def paper_processing_complete(session: AsyncSession, paper: Paper) -> bool:
    """论文是否已处理完整（PDF + 全文 + 当前空间下的向量都在）——完整则无需再补全。

    「有向量」按**激活空间**算：换了嵌入模型之后，旧空间的向量对检索已经不可见，
    这篇论文就该重新走一遍补全，而不是因为库里还留着旧向量就认为它已就绪。
    """
    if not (paper.pdf_path and paper.full_text_path):
        return False
    return await has_current_paper_vector(session, paper)


def paper_embedding_text(paper: Paper) -> str:
    """论文级向量的统一文本口径：标题 + 作者名 + 摘要（截断 EMBED_TEXT_MAX_CHARS 字）。

    三处生成论文级向量的地方共用（手动添加补全、ingest 上链批量、每日池批量），
    保证同一批向量在同一口径下可比。作者名进文本是为了「找某人的工作」这类检索。
    口径若要改，同时把 services/embedding.py 的 TEXT_VERSION +1。
    """
    names: list[str] = []
    for item in paper.authors or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name and str(name).strip():
            names.append(str(name).strip())
    parts = [paper.title or "", ", ".join(names), paper.abstract or ""]
    return "\n".join(parts)[:EMBED_TEXT_MAX_CHARS]


async def has_current_paper_vector(session: AsyncSession, paper: Paper) -> bool:
    """这篇论文在激活空间下已有论文级向量？（没有激活空间 = 还没建过任何向量）"""
    space = await active_space(session)
    if space is None:
        return False
    from app.services.embedding import papers_with_vector

    return bool(await papers_with_vector(session, [paper.id], space))


async def embed_paper(
    session: AsyncSession,
    paper: Paper,
    *,
    user_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    library_id: uuid.UUID | None = None,
) -> None:
    """为论文建论文级向量（文本口径见 paper_embedding_text）。调用方负责 commit。

    向量写进 paper_vectors 并带上所属空间。provider 不支持嵌入时抛
    NotImplementedError（调用方按 skipped 处理）。
    """
    from app.services.embedding import embed_documents, upsert_paper_vector

    vectors, space = await embed_documents(
        session,
        [paper_embedding_text(paper)],
        user_id=user_id,
        project_id=project_id,
        library_id=library_id,
    )
    await upsert_paper_vector(session, paper.id, vectors[0], space)


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

    # 分块（文献问答底座）：有全文按全文切，没全文也建一个「标题 + 摘要」兜底块，
    # 保证这篇论文对文献对话可检索。已有全文块则不重切（避免丢已补的块向量）。
    # 不单列可见进度阶段（STAGES 保持 download/extract/embed/score 不变）；块向量按用户开关
    # 在下面 embed 阶段之后补。best-effort：失败记日志、回滚重取，不阻断后续阶段。
    from app.services.chunks import ensure_paper_chunks

    try:
        if await ensure_paper_chunks(session, paper):
            await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.warning("enrich chunk indexing failed for paper %s", paper_id, exc_info=True)
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
    if await has_current_paper_vector(session, paper):
        await emit("embed", "skipped", "already embedded")
    else:
        try:
            await embed_paper(
                session,
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

    # 摘要兜底块的向量 = 论文级向量的拷贝（零 token），故排在 embed 之后、不受开关控制。
    # best-effort：拷不上就留空，下次补建时一起补。
    try:
        from app.services.chunks import sync_abstract_chunk_vectors

        if await sync_abstract_chunk_vectors(session, paper_ids=[paper_id]):
            await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.warning("abstract chunk vector sync failed for %s", paper_id, exc_info=True)
        paper = await _rollback_and_reload()

    # 块向量：抓到全文就一定建。best-effort，不影响 embed 阶段判定；不发独立进度事件。
    from app.core.llm.router import get_llm_router
    from app.services.chunks import embed_pending_chunks_for_papers

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
        logger.warning("enrich chunk embed failed for paper %s", paper_id, exc_info=True)
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


async def _run_enrichment_unbounded(
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


async def _run_enrichment(
    *,
    task_id: str,
    paper_id: uuid.UUID,
    library_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    project_id: uuid.UUID | None,
    redis: Redis,
) -> None:
    """限制单进程内补全并发，避免一次批量导入同时压满上游服务。"""
    async with _enrich_semaphore():
        await _run_enrichment_unbounded(
            task_id=task_id,
            paper_id=paper_id,
            library_id=library_id,
            user_id=user_id,
            project_id=project_id,
            redis=redis,
        )


def _batch_input_summary(item: dict[str, Any]) -> tuple[str, str]:
    for source in ("arxiv_id", "doi", "bibtex"):
        value = item.get(source)
        if value:
            compact = " ".join(str(value).split())
            return source, compact[:180]
    return "unknown", ""


async def _run_batch_import(
    *,
    task_id: str,
    items: list[dict[str, Any]],
    library_id: uuid.UUID,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None,
    redis: Redis,
) -> None:
    """逐项导入、独立提交，再受控并发补全；一项失败不回滚其它项。"""
    from app.core.db import get_sessionmaker
    from app.core.events import EventBus, publish_paper_task_event
    from app.services import paper_import as paper_import_service

    bus = EventBus(redis)
    totals = {"created": 0, "existing": 0, "invalid": 0, "failed": 0}
    enrichment_tasks: list[tuple[int, str]] = []

    try:
        for index, item in enumerate(items):
            source, input_value = _batch_input_summary(item)
            event: dict[str, Any] = {
                "index": index,
                "source": source,
                "input": input_value,
                "status": "failed",
            }
            try:
                async with get_sessionmaker()() as session:
                    library = await session.get(DirectionLibrary, library_id)
                    if library is None:
                        await publish_paper_task_event(
                            bus, task_id, "error", {"message": "library not found"}
                        )
                        return
                    try:
                        result = await paper_import_service.add_manual_paper_to_library(
                            session,
                            library=library,
                            arxiv_id=item.get("arxiv_id"),
                            doi=item.get("doi"),
                            bibtex=item.get("bibtex"),
                            project_id=project_id,
                        )
                    except paper_import_service.DuplicatePaperError as e:
                        paper = await session.get(Paper, e.paper_id)
                        event.update(
                            status="existing",
                            paper_id=str(e.paper_id),
                            title=paper.title if paper is not None else "",
                            processing=False,
                        )
                    except paper_import_service.ParseFailedError as e:
                        event.update(status="invalid", error=str(e), processing=False)
                    else:
                        paper = result.paper
                        processing = result.created or not await paper_processing_complete(
                            session, paper
                        )
                        child_task_id: str | None = None
                        if processing:
                            child_task_id = await launch_paper_enrichment(
                                redis=redis,
                                paper_id=paper.id,
                                user_id=user_id,
                                library_id=library_id,
                                project_id=project_id,
                            )
                        if child_task_id:
                            enrichment_tasks.append((index, child_task_id))
                        event.update(
                            status="created",
                            paper_id=str(paper.id),
                            title=paper.title,
                            processing=bool(child_task_id),
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 单项隔离，继续处理后续输入
                logger.exception("batch paper import item %d failed", index)
                event.update(status="failed", error=f"{type(e).__name__}: {e}", processing=False)

            status_name = str(event["status"])
            totals[status_name] += 1
            await publish_paper_task_event(bus, task_id, "batch_item", event)
            await publish_paper_task_event(
                bus,
                task_id,
                "batch_progress",
                {"completed": index + 1, "total": len(items), **totals},
            )

        async def wait_for_enrichment(index: int, child_task_id: str) -> int:
            await await_task(child_task_id)
            return index

        waits = [wait_for_enrichment(index, child_id) for index, child_id in enrichment_tasks]
        for completed in asyncio.as_completed(waits):
            index = await completed
            await publish_paper_task_event(
                bus, task_id, "batch_enriched", {"index": index}
            )

        await publish_paper_task_event(
            bus,
            task_id,
            "done",
            {"total": len(items), **totals},
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("batch paper import task failed: %s", task_id)
        try:
            await publish_paper_task_event(
                bus, task_id, "error", {"message": f"{type(e).__name__}: {e}"}
            )
        except Exception:  # noqa: BLE001
            logger.warning("failed to publish batch paper task error event", exc_info=True)


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


async def launch_paper_batch_import(
    *,
    redis: Redis,
    items: list[dict[str, Any]],
    library_id: uuid.UUID,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
) -> str | None:
    """登记并启动批量导入父任务；逐项结果与补全状态走同一 SSE 通道。"""
    task_id = uuid.uuid4().hex
    try:
        await redis.setex(paper_task_owner_key(task_id), _OWNER_TTL_SECONDS, str(user_id))
    except Exception:  # noqa: BLE001
        logger.warning("batch paper task owner registration failed; import not launched")
        return None

    task = asyncio.create_task(
        _run_batch_import(
            task_id=task_id,
            items=items,
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
    """等待某后台任务跑完。

    批量导入用它等各篇的补全子任务收尾，好在全部完成后发一条 ``done``——所以这不再
    只是测试用的助手，docstring 曾经这么写，改坏它会让批量导入的完成事件失准。

    只看得见**本进程**起的任务：``_TASKS`` 是进程内字典。任务已经跑完并被回调摘掉，
    或者压根是别的进程起的，这里都直接返回——对调用方而言「不知道」和「已完成」
    同样处理，因为真正的完成信号走 SSE，不靠这个函数。
    """
    task = _TASKS.get(task_id)
    if task is not None:
        await task


async def owner_of(redis: Redis, task_id: str) -> str | None:
    """取任务归属用户 id（字符串），无则 None。"""
    return await redis.get(paper_task_owner_key(task_id))
