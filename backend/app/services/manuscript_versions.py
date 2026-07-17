"""稿件文件版本快照（自动打点 + 人工回滚，docs/api-m5-b.md §2 扩展）。

打点时机：AI 分节写入前（pre_ai）、编译当刻（compile）、恢复前备份（pre_restore）。
同文件连续内容相同不重复存；每文件最多保留 MAX_VERSIONS_PER_FILE 份（删最旧）。
仅依赖 models + db，供 crdt_rooms / latex_compile / api 复用（避免 import 环）。
"""

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.manuscript import ManuscriptFile, ManuscriptFileVersion

MAX_VERSIONS_PER_FILE = 50


async def snapshot_file(
    session: AsyncSession,
    file: ManuscriptFile,
    *,
    origin: str,
    label: str | None = None,
    created_by: uuid.UUID | None = None,
    content: str | None = None,
) -> ManuscriptFileVersion | None:
    """存一份快照（content 缺省取 file.content）；与最新一份内容相同则跳过。

    调用方负责 commit（与触发点的业务写入同事务）。
    """
    text = file.content if content is None else content
    latest = (
        await session.execute(
            select(ManuscriptFileVersion)
            .where(ManuscriptFileVersion.file_id == file.id)
            .order_by(ManuscriptFileVersion.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None and latest.content == text:
        return None
    version = ManuscriptFileVersion(
        file_id=file.id,
        seq=(latest.seq + 1) if latest is not None else 1,
        origin=origin,
        label=label,
        content=text,
        created_by=created_by,
    )
    session.add(version)
    await _prune(session, file.id)
    return version


async def _prune(session: AsyncSession, file_id: uuid.UUID) -> None:
    count = (
        await session.execute(
            select(func.count())
            .select_from(ManuscriptFileVersion)
            .where(ManuscriptFileVersion.file_id == file_id)
        )
    ).scalar_one()
    overflow = count + 1 - MAX_VERSIONS_PER_FILE  # +1：本次待 add 的一份
    if overflow <= 0:
        return
    oldest_ids = (
        (
            await session.execute(
                select(ManuscriptFileVersion.id)
                .where(ManuscriptFileVersion.file_id == file_id)
                .order_by(ManuscriptFileVersion.seq.asc())
                .limit(overflow)
            )
        )
        .scalars()
        .all()
    )
    if oldest_ids:
        await session.execute(
            delete(ManuscriptFileVersion).where(ManuscriptFileVersion.id.in_(oldest_ids))
        )


async def list_versions(session: AsyncSession, file_id: uuid.UUID) -> list[ManuscriptFileVersion]:
    """版本列表（新在前）。content 也在 ORM 对象上，API 层只序列化元数据。"""
    rows = (
        await session.execute(
            select(ManuscriptFileVersion)
            .where(ManuscriptFileVersion.file_id == file_id)
            .order_by(ManuscriptFileVersion.seq.desc())
        )
    ).scalars()
    return list(rows)


async def get_version(
    session: AsyncSession, file_id: uuid.UUID, version_id: uuid.UUID
) -> ManuscriptFileVersion | None:
    version = await session.get(ManuscriptFileVersion, version_id)
    if version is None or version.file_id != file_id:
        return None
    return version
