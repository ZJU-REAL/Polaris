"""健康检查。"""

from fastapi import APIRouter

from app import __version__
from app.core.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    # instance_id：桌面外壳启动引擎时给的随机标识（POLARIS_INSTANCE_ID）。外壳拿它确认
    # 端口上应答的是自己这次拉起的引擎（#850）；从源码跑时为空串。它不是口令，公开无妨。
    return {"status": "ok", "version": __version__, "instance_id": get_settings().instance_id}
