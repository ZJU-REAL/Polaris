"""可选的文献来源清单。

建库表单此前第一个问的是「arXiv 分类」，因为入库路径写死了 arXiv。路径改成按库
声明的来源检索之后，表单要先问「从哪里找文献」——而可选项只能问注册表：装一个源
就该立刻可选，撤一个就该立刻消失，前端写死一份名单会让这两件事都要改代码才生效。

只读、普通登录即可：知道平台支持哪些文献源不涉及权限，而建库的人就是普通用户。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import current_active_user
from app.models.user import User

router = APIRouter(prefix="/literature-sources", tags=["literature"])

#: 给人看的名字与它擅长的领域。注册表只有 id，而 "europepmc" 对一个做结构的人
#: 不构成任何提示——选择器里要让人能判断「这个源和我有没有关系」。
_SOURCE_META: dict[str, tuple[str, str]] = {
    "arxiv": ("arXiv", "预印本：物理、数学、计算机、定量生物"),
    "openalex": ("OpenAlex", "跨学科总库，覆盖面最广"),
    "semantic": ("Semantic Scholar", "跨学科，引文关系丰富"),
    "pubmed": ("PubMed", "生物医学与生命科学"),
    "europepmc": ("Europe PMC", "生物医学，含预印本与全文"),
    "crossref": ("Crossref", "期刊论文总库：化学、工程、社科"),
    "hal": ("HAL", "法国科研机构开放存档"),
    "core": ("CORE", "开放获取论文聚合"),
    "base": ("BASE", "学术资源聚合，覆盖多语种"),
    "sciverse": ("ScienceDirect", "Elsevier 期刊"),
}


class LiteratureSourceRead(BaseModel):
    """一个可选来源。``id`` 是写进库配置 keywords.sources 的值。"""

    id: str
    title: str
    description: str
    #: 这个源支不支持按分类检索。只有 arXiv 有分类体系——表单据此决定
    #: 要不要展示「arXiv 分类」那一项，而不是对所有人都摆出来
    supports_categories: bool


@router.get("", response_model=list[LiteratureSourceRead])
async def list_literature_sources(
    _user: User = Depends(current_active_user),
) -> list[LiteratureSourceRead]:
    """能用来检索的文献源，按注册顺序（= 检索优先级）。"""
    from app.services.literature import sources as literature_sources

    out: list[LiteratureSourceRead] = []
    for source_id, _adapter in literature_sources.sources_with_capability("search"):
        title, description = _SOURCE_META.get(source_id, (source_id, ""))
        out.append(
            LiteratureSourceRead(
                id=source_id,
                title=title,
                description=description,
                supports_categories=source_id == "arxiv",
            )
        )
    return out
