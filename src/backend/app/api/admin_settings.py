"""管理端全局设置路由（仅 role=admin）：机构抽取模式、论文级向量总闸等。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.core.db import get_session
from app.schemas.admin_settings import (
    AffiliationModeRead,
    AffiliationModeUpdate,
    DailyEmbedBackfillResult,
    EmbeddingSpaceAdoptResult,
    EmbeddingSpaceItem,
    EmbeddingSpaceStatus,
    ExperimentEnvSettings,
    LabLeaderboardSettingRead,
    LabLeaderboardSettingUpdate,
)
from app.schemas.tts import TTSAdminSettings, TTSTestResult
from app.services import affiliations as affiliations_service
from app.services import daily_feed as daily_service
from app.services import embedding as embedding_service
from app.services import experiment_settings as experiment_settings_service
from app.services import lab as lab_service
from app.services import tts as tts_service

router = APIRouter(
    prefix="/admin/settings", tags=["admin-settings"], dependencies=[Depends(require_admin)]
)


@router.get("/affiliation-mode", response_model=AffiliationModeRead)
async def get_affiliation_mode(session: AsyncSession = Depends(get_session)) -> AffiliationModeRead:
    return AffiliationModeRead(
        mode=await affiliations_service.get_affiliation_extraction_mode(session)
    )


@router.put("/affiliation-mode", response_model=AffiliationModeRead)
async def set_affiliation_mode(
    payload: AffiliationModeUpdate,
    session: AsyncSession = Depends(get_session),
) -> AffiliationModeRead:
    try:
        mode = await affiliations_service.set_affiliation_extraction_mode(session, payload.mode)
    except affiliations_service.InvalidAffiliationModeError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"INVALID_AFFILIATION_MODE:{exc.mode}"
        ) from exc
    return AffiliationModeRead(mode=mode)


@router.post("/daily-embed/backfill", response_model=DailyEmbedBackfillResult)
async def backfill_daily_embed(
    session: AsyncSession = Depends(get_session),
) -> DailyEmbedBackfillResult:
    """给当前窗口内还没有向量的每日论文一次性补建（开开关后补齐历史用）。费用记系统账。"""
    stats = await daily_service.backfill_embeddings(session)
    return DailyEmbedBackfillResult(**stats)


@router.get("/embedding-space", response_model=EmbeddingSpaceStatus)
async def get_embedding_space(
    session: AsyncSession = Depends(get_session),
) -> EmbeddingSpaceStatus:
    """当前向量模型 + 库里各向量空间的规模。

    ``mismatched=true`` 表示路由表里的嵌入模型已经不是建库时那个了：此时新向量一律
    拒绝写入（不能让两个模型的向量混进同一个池子），需要 adopt 确认换用新模型。
    """
    inventory = await embedding_service.space_inventory(session)
    spaces = [EmbeddingSpaceItem(**item) for item in inventory]
    active = next((s for s in spaces if s.active), None)
    model = await embedding_service.routed_model()
    return EmbeddingSpaceStatus(
        active=active,
        routed_model=model,
        mismatched=bool(active and model and active.model != model),
        spaces=spaces,
    )


@router.post("/embedding-space/adopt", response_model=EmbeddingSpaceAdoptResult)
async def adopt_embedding_space(
    session: AsyncSession = Depends(get_session),
) -> EmbeddingSpaceAdoptResult:
    """确认换用路由表里当前的嵌入模型（换模型后必须调一次）。

    现场探一次模型拿到它的实际维度，据此定义新的激活空间。旧向量留在库里不动，
    检索覆盖率会掉下来，用各处的「重新建立索引」把新空间补齐即可；adopt 回旧模型
    就是回滚。
    """
    try:
        space, previous = await embedding_service.adopt_routed_model(session)
    except Exception as exc:  # noqa: BLE001 — 探测要真打一次模型，上游不可达也在此收口
        # 探不到维度就不能定义空间，硬切过去只会让之后每次嵌入都失败
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"EMBEDDING_NOT_AVAILABLE:{type(exc).__name__}: {exc}",
        ) from exc
    items = await embedding_service.space_inventory(session)
    current = next(
        (item for item in items if item["key"] == space.key),
        {
            "key": space.key,
            "model": space.model,
            "dim": space.dim,
            "papers": 0,
            "chunks": 0,
            "ideas": 0,
            "active": True,
        },
    )
    return EmbeddingSpaceAdoptResult(
        active=EmbeddingSpaceItem(**current), previous=previous
    )


@router.get("/lab-leaderboard", response_model=LabLeaderboardSettingRead)
async def get_lab_leaderboard(
    session: AsyncSession = Depends(get_session),
) -> LabLeaderboardSettingRead:
    """实验室概况页的用量排行榜是否对普通成员可见（默认开）。"""
    return LabLeaderboardSettingRead(enabled=await lab_service.get_leaderboard_enabled(session))


@router.put("/lab-leaderboard", response_model=LabLeaderboardSettingRead)
async def set_lab_leaderboard(
    payload: LabLeaderboardSettingUpdate,
    session: AsyncSession = Depends(get_session),
) -> LabLeaderboardSettingRead:
    return LabLeaderboardSettingRead(
        enabled=await lab_service.set_leaderboard_enabled(session, payload.enabled)
    )


@router.get("/experiment-env", response_model=ExperimentEnvSettings)
async def get_experiment_env(
    session: AsyncSession = Depends(get_session),
) -> ExperimentEnvSettings:
    """实验的全局环境设置（模型/数据集位置、pip 镜像、HF 端点、代理）。"""
    return ExperimentEnvSettings(**await experiment_settings_service.get_settings(session))


@router.put("/experiment-env", response_model=ExperimentEnvSettings)
async def set_experiment_env(
    payload: ExperimentEnvSettings,
    session: AsyncSession = Depends(get_session),
) -> ExperimentEnvSettings:
    """整份覆盖写入。字段非法（会拼进远端 shell 的路径/URL）→ 422，带上是哪个字段。"""
    try:
        saved = await experiment_settings_service.set_settings(session, payload.model_dump())
    except experiment_settings_service.InvalidExperimentSettingError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"INVALID_EXPERIMENT_SETTING:{exc.field}",
        ) from exc
    return ExperimentEnvSettings(**saved)


@router.get("/tts", response_model=TTSAdminSettings)
async def get_tts_settings(
    session: AsyncSession = Depends(get_session),
) -> TTSAdminSettings:
    """Platform speech provider and default model."""
    return TTSAdminSettings(**await tts_service.get_admin_settings(session))


@router.put("/tts", response_model=TTSAdminSettings)
async def set_tts_settings(
    payload: TTSAdminSettings,
    session: AsyncSession = Depends(get_session),
) -> TTSAdminSettings:
    try:
        saved = await tts_service.set_admin_settings(session, payload.model_dump())
    except tts_service.InvalidTTSSettingError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"INVALID_TTS_SETTING:{exc.field}"
        ) from exc
    return TTSAdminSettings(**saved)


@router.post("/tts/test", response_model=TTSTestResult)
async def test_tts_settings(payload: TTSAdminSettings) -> TTSTestResult:
    """Synthesize a short sample with the unsaved draft configuration."""
    try:
        audio_bytes = await tts_service.test_admin_settings(payload.model_dump())
    except tts_service.InvalidTTSSettingError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"INVALID_TTS_SETTING:{exc.field}"
        ) from exc
    except tts_service.TTSNotAvailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return TTSTestResult(ok=True, model=payload.model, audio_bytes=audio_bytes)
