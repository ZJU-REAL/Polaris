"""恢复起点不再依赖墙钟（#784）。

``best_open_node`` 的承诺是「同分取先建」，而此前的排序键是 ``created_at``——Python
侧 ``utcnow()``，不单调。时钟回拨会让后建的节点拿到更小的时间戳，于是「取先建」选出
的是后建的那个；同一微秒批量建的兄弟节点更是由数据库随意返回。两种情况都不报错，
只是这次恢复走进了另一棵子树。

所以这里的用例**直接把时间戳改坏**，再断言顺序不受影响——这是唯一能证明「不再依赖
墙钟」的办法，光跑一遍正常流程是看不出区别的。
"""

import datetime as dt
import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.hypothesis import HypothesisNode
from app.services import hypothesis_tree
from tests.conftest import make_project_with_library, register_and_login


async def _run_with_nodes(client, *, count=3):
    """建一个 run + 一根 + count 个同分子节点，返回 (run_id, [子节点 id 按建的顺序])。"""
    from app.models.voyage import VoyageRun

    token = await register_and_login(client, email="hypseq@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="hypseq")

    async with get_sessionmaker()() as session:
        run = VoyageRun(
            project_id=uuid.UUID(project_id),
            library_id=library_id,
            kind="discovery",
            goal="seq 测试方向",
            status="running",
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        root = await hypothesis_tree.create_node(
            session, run, parent_id=None, kind="hypothesis", statement="root", score=0.5
        )
        children = []
        for i in range(count):
            child = await hypothesis_tree.create_node(
                session,
                run,
                parent_id=root.id,
                kind="hypothesis",
                statement=f"child {i}",
                score=0.25,  # 全部同分：决胜完全落在顺序上
            )
            children.append(child.id)
        # 根节点分更高，先把它挪出 open，否则它总是最优
        await hypothesis_tree.transition(session, root, "expanded")
        return run.id, children


async def test_seq_counts_from_one_within_a_run(client):
    run_id, children = await _run_with_nodes(client)
    async with get_sessionmaker()() as session:
        nodes = await hypothesis_tree.tree_for_run(session, run_id)
    assert [n.seq for n in nodes] == [1, 2, 3, 4]
    assert [n.id for n in nodes][1:] == children


async def test_the_best_open_node_is_the_first_created_among_equals(client):
    run_id, children = await _run_with_nodes(client)
    async with get_sessionmaker()() as session:
        best = await hypothesis_tree.best_open_node(session, run_id)
    assert best is not None
    assert best.id == children[0]


async def test_a_clock_that_runs_backwards_no_longer_changes_the_resume_point(client):
    """把后建节点的 created_at 改成最早——这正是本机容器每 10 秒跳一次 ±1 秒会造成的
    局面（#234 的根因）。改之前这会让恢复选中后建的那个。"""
    run_id, children = await _run_with_nodes(client)

    async with get_sessionmaker()() as session:
        # 最后一个子节点伪装成「最早创建」。走 ORM 而不是拼 SQL：UUID 在 sqlite 里
        # 存成无连字符的 32 位十六进制，用 str(uuid) 去 WHERE 一行都匹配不上，
        # UPDATE 变成空操作而用例照样「通过」——那是最难发现的一种假绿
        node = await session.get(HypothesisNode, children[-1])
        node.created_at = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
        await session.commit()

    async with get_sessionmaker()() as session:
        best = await hypothesis_tree.best_open_node(session, run_id)
        nodes = await hypothesis_tree.tree_for_run(session, run_id)
    assert best is not None
    assert best.id == children[0], "恢复起点被墙钟带偏了"
    # 树读取同样不受影响
    assert [n.seq for n in nodes] == [1, 2, 3, 4]


async def test_identical_timestamps_still_have_a_defined_order(client):
    """同一微秒批量建：按 created_at 排是完全并列，数据库返回哪一行都合法。"""
    run_id, children = await _run_with_nodes(client)

    async with get_sessionmaker()() as session:
        same = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        for node_id in children:
            node = await session.get(HypothesisNode, node_id)
            node.created_at = same
        await session.commit()

    async with get_sessionmaker()() as session:
        best = await hypothesis_tree.best_open_node(session, run_id)
    assert best is not None
    assert best.id == children[0]


async def test_two_runs_number_independently(client):
    """序号按 run 各自从 1 起：跨 run 连续只会让读日志的人误以为两者有关系。"""
    first_run, _ = await _run_with_nodes(client, count=2)

    from app.models.voyage import VoyageRun

    async with get_sessionmaker()() as session:
        first = await session.get(VoyageRun, first_run)
        second = VoyageRun(
            project_id=first.project_id,
            library_id=first.library_id,
            kind="discovery",
            goal="另一个方向",
            status="running",
        )
        session.add(second)
        await session.commit()
        await session.refresh(second)
        await hypothesis_tree.create_node(
            session, second, parent_id=None, kind="hypothesis", statement="other root"
        )
        seqs = (
            (
                await session.execute(
                    select(HypothesisNode.seq).where(HypothesisNode.run_id == second.id)
                )
            )
            .scalars()
            .all()
        )
    assert seqs == [1]
