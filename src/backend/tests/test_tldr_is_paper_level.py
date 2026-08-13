"""TL;DR 属于论文本身，不属于任何一个文献库。

生产上这条被打破过：papers.tldr 里存的是相关性打分写下的判词——
「论文关注延迟反馈下策略评估的诊断方法，不涉及长期运行智能体……相关性较低。」
那是对某一个库的方向说的，却挂在全平台共享的论文上，于是每个库、每日流和搜索
结果看到的都是别人库的评价。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.paper import Paper
from app.services.paper_wiki import upsert_wiki
from tests.conftest import add_paper, make_project_with_library, register_and_login

WIKI = """## TL;DR
该论文提出一种两阶段检索方法，在三个基准上把召回率提升了 8 个百分点。

## 方法
省略。
"""


async def test_compiled_tldr_replaces_the_scoring_placeholder(client):
    """编译一跑，正式 TL;DR 就该接管打分阶段留下的占位。"""
    token = await register_and_login(client, email="tldr@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, _library_id = await make_project_with_library(
        client, headers, name="tldr-proj", definition={"statement": "长期运行智能体"}
    )

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Two-stage retrieval",
            # 打分阶段留下的、带方向色彩的占位
            tldr="该研究不涉及长期运行智能体的持续执行，相关性较低。",
        )
        await session.commit()
        paper_id = paper.id

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, paper_id)
        await upsert_wiki(session, paper=paper, content=WIKI, model="fake")
        await session.commit()

    async with get_sessionmaker()() as session:
        tldr = (await session.execute(select(Paper.tldr).where(Paper.id == paper_id))).scalar_one()
    assert "相关性较低" not in tldr, "编译之后不该还挂着对某个库的判词"
    assert "两阶段检索方法" in tldr
