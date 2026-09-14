"""可选的执行后端与流程包清单（#674）。

两个注册表早就装好了东西——``python-ml`` 之外还有 ngspice、openfoam、fmu，流程包还
能从 ``<data_dir>/packs/`` 读用户自己写的——但创建实验时没有任何地方能选到它们：
``params.backend`` / ``params.process_pack`` 两个字段在类型里注了「本期无选择 UI」，
而它们等的那个条件早就满足了。于是每个实验都跑 python-ml，其余三个后端只有改仓库
才用得上。

前端写死名单是不行的：流程包是磁盘上的数据，用户丢一个 YAML 进去就该能选到。

只读、普通登录即可：知道装了哪些后端不涉及权限，而挑后端的人就是建实验的人。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import current_active_user
from app.models.user import User

router = APIRouter(prefix="/experiment-backends", tags=["experiments"])


class RunnerBackendRead(BaseModel):
    """一个执行后端在选择器里的样子。

    带上 manifest 里几项**选之前必须知道**的事实，而不只是一个 id：
    要不要 SSH 凭据、要不要 License 席位、会不会动真设备。选完才发现跑不起来，
    对一次动辄几十分钟的实验来说代价太大。
    """

    backend: str
    #: batch=一次跑完 / session=会话交互 / streaming=持续流式
    interaction: str
    #: none / filesystem / network / physical（physical=动真设备）
    side_effects: str
    #: 接受的凭据类型；含 "ssh" 表示要先配 SSH 凭据才跑得起来
    credential_kinds: list[str]
    #: 需要的 License feature 名（如 HFSS）；空 = 无 license 要求
    licenses: list[str]
    #: 缺省后端（不选时就是它，等于今天所有存量实验的行为）
    is_default: bool


class ProcessPackRead(BaseModel):
    """一个流程包在选择器里的样子。``name`` 是写进 params.process_pack 的值。"""

    name: str
    #: 包里的阶段 id，按顺序——选包等于选一条流程，阶段名是唯一看得见的依据
    phases: list[str]
    #: 父包名（单层继承）；None = 自成一包
    extends: str | None = None


@router.get("", response_model=list[RunnerBackendRead])
async def list_backends(
    _user: User = Depends(current_active_user),
) -> list[RunnerBackendRead]:
    """已注册的执行后端。

    manifest 由插件工厂的无参探针实例给出（见 registry.manifest_for），无副作用。
    """
    from app.services.runners import registry

    out: list[RunnerBackendRead] = []
    for backend in registry.known_backends():
        manifest = registry.manifest_for(backend)
        out.append(
            RunnerBackendRead(
                backend=backend,
                interaction=str(manifest.interaction),
                side_effects=str(manifest.side_effects),
                credential_kinds=list(manifest.credential_kinds),
                licenses=[lic.feature for lic in manifest.licenses],
                is_default=backend == registry.DEFAULT_BACKEND,
            )
        )
    return out


@router.get("/process-packs", response_model=list[ProcessPackRead])
async def list_process_packs(
    _user: User = Depends(current_active_user),
) -> list[ProcessPackRead]:
    """可选的流程包（内置 + ``<data_dir>/packs/`` 里用户自己写的）。

    坏包在加载时已被逐个跳过，不会让这个接口 500——手写的包写坏了是常态，
    不该让所有人都选不了流程。
    """
    from app.services import process_packs

    out: list[ProcessPackRead] = []
    for name in sorted(process_packs.known_pack_names()):
        try:
            pack = process_packs.load_pack(name)
        except process_packs.ProcessPackError:
            # 名字在清单里但装载失败（父包缺失等）：跳过而不是整份报错
            continue
        out.append(
            ProcessPackRead(
                name=name,
                phases=[phase.id for phase in pack.phases],
                extends=pack.extends,
            )
        )
    return out
