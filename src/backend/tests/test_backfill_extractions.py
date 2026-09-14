"""存量回填的口径（#770）。

回填 CLI 原本按注册表全量抽：装一个学科包，全库每篇论文都多跑一次那个学科的抽取。
代价是每篇一次白烧的 LLM 调用，产出的还是用错领域字段的卡——而且没有任何报错。
口径必须顺着「这篇论文在哪个库里、那个库声明了什么学科」反查。

这里测的是决定口径的那几个纯函数与一个查询：``_run`` 自己在 finally 里
``dispose_engine()``，整条跑进测试会把共用引擎拆掉。
"""

import uuid

from app.cli.backfill_extractions import _index_library, _library_contexts, _plan
from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary
from tests.conftest import add_paper, make_project_with_library, register_and_login

BUILTIN = {"skeleton", "method", "gaps"}


def _ids(plan):
    return {schema.id for schema, _library_id in plan}


def test_a_paper_outside_any_library_gets_builtin_schemas_only():
    """个人书架导入的论文没有成员行，口径与学科包出现之前完全一致。"""
    assert _ids(_plan([(None, None)])) == BUILTIN


def test_a_library_without_a_discipline_does_not_pay_for_installed_packs():
    """装了结构工程包，不该让没声明学科的库跟着多抽一遍——这就是原来的那个 bug。"""
    assert _ids(_plan([(uuid.uuid4(), None)])) == BUILTIN


def test_a_discipline_library_adds_its_own_card_on_top_of_the_builtins():
    library_id = uuid.uuid4()
    plan = _plan([(library_id, "structural")])
    assert _ids(plan) == BUILTIN | {"structural.method"}
    # 学科卡记在声明了这个学科的库名下，抽取用量才记得对
    owner = {schema.id: lib for schema, lib in plan}["structural.method"]
    assert owner == library_id


def test_a_paper_in_several_libraries_gets_the_union_and_pays_once_for_the_builtins():
    """同一篇可能同时在几个库里，各自学科不同：每个库都该拿到它那一版的卡，
    但内置 schema 只抽一次——按 id 去重，不是按库重复。"""
    plan = _plan([(uuid.uuid4(), "structural"), (uuid.uuid4(), None)])
    ids = [schema.id for schema, _ in plan]
    assert set(ids) == BUILTIN | {"structural.method"}
    assert len(ids) == len(set(ids)), "内置 schema 被按库重复排进了计划"


def test_index_library_prefers_the_one_that_declared_a_discipline():
    """双轴向量按论文存、不带 library_id，多库论文只能有一份：报那个声明了学科的库，
    否则学科卡的两根轴取不到。"""
    plain, scoped = uuid.uuid4(), uuid.uuid4()
    assert _index_library([(plain, None), (scoped, "structural")]) == scoped
    assert _index_library([(plain, None)]) == plain
    assert _index_library([(None, None)]) is None


async def test_library_contexts_reads_the_discipline_off_the_membership_row(client):
    token = await register_and_login(client, email="backfill@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(
        client, headers, name="backfill-proj"
    )

    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, library_id)
        library.discipline = "structural"
        # add_paper 建内容池论文的同时就写了起源库的成员行（见 conftest）
        paper = await add_paper(
            session, project_id=uuid.UUID(project_id), title="Backfill Paper", status="scored"
        )
        await session.commit()
        paper_id = paper.id

    async with get_sessionmaker()() as session:
        contexts = await _library_contexts(session, paper_id)
    assert contexts == [(library_id, "structural")]
    assert "structural.method" in _ids(_plan(contexts))


async def test_a_paper_with_no_membership_row_falls_back_to_the_empty_context(client):
    """个人书架导入的论文只在内容池里、不属于任何方向库，口径回到内置 schema。"""
    from app.models.paper import new_paper

    await register_and_login(client, email="backfill2@example.com")

    async with get_sessionmaker()() as session:
        paper = new_paper(title="Shelf Only Paper")
        session.add(paper)
        await session.commit()
        paper_id = paper.id

    async with get_sessionmaker()() as session:
        contexts = await _library_contexts(session, paper_id)
    assert contexts == [(None, None)]
