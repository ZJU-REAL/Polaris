"""技能 schema（docs/skill-system.md §1.2/§4）：manifest 严格校验。"""

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

SKILL_KINDS = ("guidance", "rubric", "persona", "workflow")
SKILL_SCOPES = ("builtin", "user", "project")

# 注入点白名单（docs/skill-system.md §3.1）。workflow 技能用虚拟注入点 navigator.free_plan
SKILL_TARGETS = frozenset(
    {
        "wiki.score_relevance",
        "wiki.compile",
        "forge.gap_analysis",
        "forge.generate",
        "forge.score",
        "review.debate",
        "review.referees",
        "review.meta_review",
        "experiment.plan",
        "experiment.setup",
        "experiment.iterate",
        "experiment.report",
        "writing.section",
        "writing.related_work",
        "present.outline",
        "present.slides",
        "navigator.free_plan",
    }
)
# writing.section 支持分节子注入点：writing.section(abstract) 等
_SECTION_TARGET_RE = re.compile(r"^writing\.section\([a-z_]{1,32}\)$")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

BODY_MAX_CHARS = 8192  # 防单个技能吃掉 prompt 预算


def is_valid_target(target: str) -> bool:
    return target in SKILL_TARGETS or bool(_SECTION_TARGET_RE.match(target))


class SkillPersona(BaseModel):
    """persona 技能的单个人设。"""

    name: str = Field(min_length=1, max_length=64)
    stance: str = Field(min_length=1, max_length=255)
    style: str | None = Field(default=None, max_length=255)


class SkillManifest(BaseModel):
    """SkillVersion.manifest 的结构化校验（多余字段拒绝）。"""

    model_config = ConfigDict(extra="forbid")

    targets: list[str] = Field(default_factory=list, max_length=8)
    # 用户可调旋钮定义（JSON Schema 子集，S1 只存不深校验，编辑器/启用界面消费）
    config_schema: dict[str, Any] | None = None
    # body 中允许出现的模板变量名（白名单，渲染时缺失原样保留）
    variables: list[str] = Field(default_factory=list, max_length=16)
    personas: list[SkillPersona] = Field(default_factory=list, max_length=8)
    # workflow 技能的步骤模板（Navigator 步骤 schema，service 层对照动作白名单校验）
    steps: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    # 产出约束：format=json 时 Sextant 先做确定性 schema 校验（S2 接入）
    output_contract: dict[str, Any] | None = None
    # 仅作 UI 提示，不影响模型路由
    model_hint: str | None = Field(default=None, max_length=255)

    @field_validator("targets")
    @classmethod
    def _check_targets(cls, v: list[str]) -> list[str]:
        for t in v:
            if not is_valid_target(t):
                raise ValueError(f"unknown target: {t}")
        return v


class SkillCreate(BaseModel):
    slug: str
    kind: str
    name: str = Field(min_length=1, max_length=255)
    name_en: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    manifest: SkillManifest = Field(default_factory=SkillManifest)
    body: str = Field(min_length=1, max_length=BODY_MAX_CHARS)

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError("slug must match ^[a-z0-9][a-z0-9-]{1,62}$")
        return v

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        if v not in SKILL_KINDS:
            raise ValueError(f"kind must be one of {SKILL_KINDS}")
        return v


class SkillVersionCreate(BaseModel):
    manifest: SkillManifest
    body: str = Field(min_length=1, max_length=BODY_MAX_CHARS)
    changelog: str | None = Field(default=None, max_length=2000)


class SkillVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    skill_id: uuid.UUID
    version: int
    manifest: dict[str, Any]
    body: str
    changelog: str | None
    created_by: uuid.UUID | None
    created_at: datetime


class SkillRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    kind: str
    name: str
    name_en: str | None
    description: str | None
    scope: str
    owner_id: uuid.UUID | None
    project_id: uuid.UUID | None
    is_archived: bool
    created_at: datetime
    updated_at: datetime


class SkillDetail(SkillRead):
    """技能详情 = 元信息 + 当前（最新）版本内容。"""

    current_version: SkillVersionRead | None = None


class ProjectSkillCreate(BaseModel):
    skill_id: uuid.UUID
    target: str
    version_id: uuid.UUID | None = None  # None = 跟随最新
    config: dict[str, Any] | None = None
    sort_order: int = 0

    @field_validator("target")
    @classmethod
    def _check_target(cls, v: str) -> str:
        if not is_valid_target(v):
            raise ValueError(f"unknown target: {v}")
        return v


class ProjectSkillUpdate(BaseModel):
    enabled: bool | None = None
    config: dict[str, Any] | None = None
    sort_order: int | None = None
    version_id: uuid.UUID | None = None
    # version_id=None 有歧义（不改 or 改为跟随最新），显式开关区分
    unpin_version: bool = False


class SkillTestRequest(BaseModel):
    """试运行：预览技能注入后的效果（guidance/rubric 会真实调用一次 LLM）。"""

    target: str | None = None  # 缺省用技能声明的第一个 target
    goal: str = Field(
        default="请基于以上补充标准，给出一段示例输出，展示这些标准如何影响你的判断。",
        min_length=1,
        max_length=2000,
    )


class SkillTestResult(BaseModel):
    rendered: str  # 注入到 system prompt 的最终文本（persona/workflow 为结构预览）
    output: str | None  # LLM 预览输出（persona/workflow 技能为 None）
    model: str | None


class SkillRunRequest(BaseModel):
    """workflow 技能「运行此流程」：以技能 steps 为计划直接创建 AI 任务。"""

    project_id: uuid.UUID
    goal: str = Field(min_length=1, max_length=2000)
    vars: dict[str, str] | None = None  # 注入步骤 prompt 的模板变量
    budget: dict[str, Any] | None = None  # {max_tokens?}


SKILL_EXPORT_FORMAT = "polaris-skill@1"


class SkillExport(BaseModel):
    """跨部署分享的技能包（导出/导入均为此结构，导入走全量校验）。"""

    format: str = SKILL_EXPORT_FORMAT
    slug: str
    kind: str
    name: str
    name_en: str | None = None
    description: str | None = None
    version: int | None = None  # 导出时的版本号，仅供参考
    manifest: SkillManifest
    body: str = Field(min_length=1, max_length=BODY_MAX_CHARS)

    @field_validator("format")
    @classmethod
    def _check_format(cls, v: str) -> str:
        if v != SKILL_EXPORT_FORMAT:
            raise ValueError(f"unsupported format: {v}")
        return v


class SkillPublishRequest(BaseModel):
    summary: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, v: list[str]) -> list[str]:
        cleaned = [t.strip() for t in v if t.strip()]
        if any(len(t) > 32 for t in cleaned):
            raise ValueError("tag too long (max 32 chars)")
        return cleaned


class SkillListingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    skill_id: uuid.UUID
    skill_version_id: uuid.UUID
    summary: str | None
    tags: list[str] | None
    status: str  # pending | approved | rejected | delisted
    install_count: int
    published_by: uuid.UUID | None
    comment: str | None
    created_at: datetime
    # 联表补充（service 填充）
    skill: SkillRead | None = None
    version: int | None = None
    rating_avg: float | None = None
    rating_count: int = 0


class SkillListingDetail(SkillListingRead):
    """详情：附发布版本全文（安装前强制可预览）。"""

    manifest: dict[str, Any] | None = None
    body: str | None = None


class SkillRatingCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


class SkillRatingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    listing_id: uuid.UUID
    user_id: uuid.UUID
    rating: int
    comment: str | None
    created_at: datetime


class ListingDecision(BaseModel):
    comment: str | None = Field(default=None, max_length=2000)


class ProjectSkillRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    skill_id: uuid.UUID
    version_id: uuid.UUID | None
    target: str
    config: dict[str, Any] | None
    sort_order: int
    enabled: bool
    created_at: datetime
    # 联表补充（service 填充）
    skill: SkillRead | None = None
    pinned_version: int | None = None
