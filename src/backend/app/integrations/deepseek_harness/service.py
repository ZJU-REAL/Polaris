"""Read-only projection of Polaris assistant skills for DeepSeek Harness."""

import hashlib
import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.deepseek_harness.schemas import (
    HarnessSkillCatalog,
    HarnessSkillCatalogItem,
    HarnessSkillDefinition,
    HarnessSkillFile,
)
from app.models.agent_skill import AgentSkill, AgentSkillFile
from app.services import agent_skills

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _FileRecord:
    path: str
    content: str
    size: int

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def _hash_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat()


async def _files_by_skill(
    session: AsyncSession, skill_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[_FileRecord]]:
    if not skill_ids:
        return {}
    rows = (
        await session.execute(
            select(
                AgentSkillFile.skill_id,
                AgentSkillFile.path,
                AgentSkillFile.content,
                AgentSkillFile.size,
            )
            .where(AgentSkillFile.skill_id.in_(skill_ids))
            .order_by(AgentSkillFile.path)
        )
    ).all()
    grouped: dict[uuid.UUID, list[_FileRecord]] = defaultdict(list)
    for skill_id, path, content, size in rows:
        grouped[skill_id].append(_FileRecord(path=path, content=content, size=size))
    return dict(grouped)


def _skill_revision(skill: AgentSkill, files: Sequence[_FileRecord]) -> str:
    """Hash only semantic content, so load counters do not invalidate catalogs."""

    return _hash_json(
        {
            "id": str(skill.id),
            "slug": skill.slug,
            "name": skill.name,
            "description": skill.description,
            "body": skill.body,
            "invocation": skill.invocation,
            "scope": skill.scope,
            "allowedTools": skill.allowed_tools,
            "updatedAt": _utc_iso(skill.updated_at),
            "files": [
                {"path": item.path, "size": item.size, "revision": item.revision} for item in files
            ],
        }
    )


def _safe_catalog_item(
    skill: AgentSkill, files: Sequence[_FileRecord]
) -> HarnessSkillCatalogItem | None:
    """A row that predates the current slug rule must not 500 the whole catalog.

    Creation now enforces the Harness slug rule, but a legacy row could still
    violate the response contract; skip it rather than fail discovery for every
    other skill this user has.
    """
    try:
        return _catalog_item(skill, files)
    except ValidationError:
        logger.warning("skipping skill %s: not representable in the Harness catalog", skill.slug)
        return None


def _catalog_item(skill: AgentSkill, files: Sequence[_FileRecord]) -> HarnessSkillCatalogItem:
    return HarnessSkillCatalogItem(
        id=skill.id,
        slug=skill.slug,
        name=skill.name,
        description=skill.description,
        invocation=skill.invocation,
        scope=skill.scope,
        allowedTools=skill.allowed_tools,
        files=[
            HarnessSkillFile(path=item.path, size=item.size, revision=item.revision)
            for item in files
        ],
        revision=_skill_revision(skill, files),
        updatedAt=skill.updated_at,
    )


async def skill_catalog(session: AsyncSession, *, user_id: uuid.UUID) -> HarnessSkillCatalog:
    skills = await agent_skills.visible_skills(session, user_id=user_id)
    files = await _files_by_skill(session, [skill.id for skill in skills])
    items = [
        item
        for skill in skills
        if (item := _safe_catalog_item(skill, files.get(skill.id, ()))) is not None
    ]
    items.sort(key=lambda item: item.slug)
    return HarnessSkillCatalog(
        revision=_hash_json([[item.slug, item.revision] for item in items]), skills=items
    )


async def skill_definition(
    session: AsyncSession, *, user_id: uuid.UUID, slug: str
) -> HarnessSkillDefinition | None:
    skill = await agent_skills.get_visible_skill(session, slug=slug, user_id=user_id)
    if skill is None:
        return None
    files = (await _files_by_skill(session, [skill.id])).get(skill.id, [])
    item = _safe_catalog_item(skill, files)
    if item is None:
        return None
    return HarnessSkillDefinition(**item.model_dump(), body=skill.body)


async def skill_file(
    session: AsyncSession, *, user_id: uuid.UUID, slug: str, path: str
) -> tuple[str, str] | None:
    skill = await agent_skills.get_visible_skill(session, slug=slug, user_id=user_id)
    if skill is None:
        return None
    content = await session.scalar(
        select(AgentSkillFile.content).where(
            AgentSkillFile.skill_id == skill.id,
            AgentSkillFile.path == path,
        )
    )
    if content is None:
        return None
    revision = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return content, revision
