"""可选的学科包清单（#775）。

学科包是磁盘上的数据：内置几个，用户往 ``<data_dir>/disciplines/`` 里丢一个 YAML
就多一个。所以「有哪些学科可选」只能问文件系统，不能在前端写死一份名单——写死的
后果是用户照着文档写了自己的包、扔进目录、然后在界面上找不到它。

只读、不需要 owner：知道装了哪些学科不涉及任何权限，而挑学科的人就是普通用户。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import current_active_user
from app.models.user import User

router = APIRouter(prefix="/disciplines", tags=["disciplines"])


class DisciplineRead(BaseModel):
    """一个学科包在选择器里的样子。``name`` 是写进库里的那个值。"""

    name: str
    title: str
    description: str
    #: 这个包带来几条抽取 schema。0 表示方法卡沿用内置字段（如计算机科学）
    schema_count: int
    #: 新建文献库默认勾选的来源；包没声明时是平台的通用默认（#821）
    sources: list[str]
    #: 选了 arXiv 时的分类快捷项；空 = 这个学科在 arXiv 上没有对应分类
    arxiv_categories: list[str]


class DisciplineDefaults(BaseModel):
    """不选学科（「通用」）时的文献口径。与包里的默认值同一处来源，前端不另抄一份。"""

    sources: list[str]


@router.get("/defaults", response_model=DisciplineDefaults)
async def discipline_defaults(
    _user: User = Depends(current_active_user),
) -> DisciplineDefaults:
    from app.services.discipline_packs import DEFAULT_SOURCES

    return DisciplineDefaults(sources=list(DEFAULT_SOURCES))


@router.get("", response_model=list[DisciplineRead])
async def list_disciplines(
    _user: User = Depends(current_active_user),
) -> list[DisciplineRead]:
    """每次现扫而不是读缓存：用户刚丢进去的包该立刻出现在选择器里。

    目录很小，这点开销换掉「改了包还得重启」一整类困惑（同 known_disciplines）。
    坏包在 discover_packs 里已被逐个跳过，不会让这个接口 500。
    """
    from app.services.discipline_packs import DEFAULT_SOURCES, discover_packs

    return sorted(
        (
            DisciplineRead(
                name=pack.name,
                title=pack.title,
                description=pack.description,
                schema_count=len(pack.schemas),
                sources=list(pack.literature.sources if pack.literature else DEFAULT_SOURCES),
                arxiv_categories=list(pack.literature.arxiv_categories if pack.literature else ()),
            )
            for pack in discover_packs()
        ),
        key=lambda item: item.title,
    )
