"""不收窄课题时，助手够得着自己全部课题的资产。

这条测试的由来：Buddy 默认不选课题，于是 ``ctx.project_id`` 是 None，而工具里写的是
``Experiment.project_id == ctx.project_id``——SQL 里成了 ``= NULL``，一条都匹配不上。
用户对着一个明明存在的实验问，助手回「项目内不存在该实验」。想法、稿件、任务全都
是同一个毛病，只是它们失败得更安静：返回空清单，助手就说"你还没有实验"。
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.experiment import Experiment
from app.models.idea import Idea
from app.models.user import User
from app.tools.context import ToolContext
from app.tools.project_state import get_experiment, list_experiments
from app.tools.scope import project_ids_for
from tests.conftest import register_and_login


async def _seed(client) -> tuple[uuid.UUID, str, str]:
    """建一个课题 + 想法 + 实验，返回 (user_id, project_id, experiment_id)。

    实验直接落库而不走 API：走 API 要先把想法推到 promoted、再配一套 SSH 凭据，
    那套铺垫与「范围怎么算」这件事无关，只会让测试坏在别处。
    """
    token = await register_and_login(client, email="scope@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project = (
        await client.post("/api/projects", json={"name": "Scope", "statement": "s"}, headers=headers)
    ).json()
    async with get_sessionmaker()() as session:
        user_id = (
            await session.execute(select(User.id).where(User.email == "scope@example.com"))
        ).scalar_one()
        idea = Idea(project_id=uuid.UUID(project["id"]), title="An idea", content="c")
        session.add(idea)
        await session.flush()
        exp = Experiment(project_id=uuid.UUID(project["id"]), idea_id=idea.id, status="running")
        session.add(exp)
        await session.commit()
        exp_id = str(exp.id)
    return user_id, project["id"], exp_id


async def test_no_topic_selected_still_reaches_my_experiments(client):
    """默认状态（没选课题）下，实验必须查得到——这正是用户撞上的那个报错。"""
    user_id, _project_id, exp_id = await _seed(client)
    ctx = ToolContext(project_id=None, llm=LLMRouter(), user_id=user_id)

    detail = await get_experiment(ctx, {"experiment_id": exp_id})
    assert detail["id"] == exp_id

    listed = await list_experiments(ctx, {})
    assert [e["experiment_id"] for e in listed["experiments"]] == [exp_id]


async def test_picking_a_topic_narrows_to_it(client):
    """选了课题就只看这个课题——放开范围不等于取消范围。"""
    user_id, project_id, exp_id = await _seed(client)
    other = uuid.uuid4()

    mine = ToolContext(project_id=uuid.UUID(project_id), llm=LLMRouter(), user_id=user_id)
    assert (await get_experiment(mine, {"experiment_id": exp_id}))["id"] == exp_id

    elsewhere = ToolContext(project_id=other, llm=LLMRouter(), user_id=user_id)
    with pytest.raises(ValueError, match="不存在该实验"):
        await get_experiment(elsewhere, {"experiment_id": exp_id})


async def test_scope_resolution(client):
    """project_ids_for 的三种情形。"""
    user_id, project_id, _ = await _seed(client)
    async with get_sessionmaker()() as session:
        picked = uuid.uuid4()
        assert await project_ids_for(
            session, ToolContext(project_id=picked, llm=LLMRouter(), user_id=user_id)
        ) == [picked]

        # 没选课题 = 我参与的全部课题
        everything = await project_ids_for(
            session, ToolContext(project_id=None, llm=LLMRouter(), user_id=user_id)
        )
        assert uuid.UUID(project_id) in everything

        # 没有用户（系统内部调用）：空列表，而不是「全世界」
        assert (
            await project_ids_for(
                session, ToolContext(project_id=None, llm=LLMRouter(), user_id=None)
            )
            == []
        )
