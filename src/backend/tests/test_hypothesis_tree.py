"""假设/实验树（#637）：结构不变量、状态机、剪枝级联、恢复排序与读 API。

树由服务层直写（引擎接入是 D2 的事）；API 只读，可见性复用任务口径。
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.hypothesis import HypothesisNode
from app.models.user import User
from app.models.voyage import VoyageRun
from app.services import hypothesis_tree as tree_service
from tests.conftest import register_and_login


async def _hdr(client, email):
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def _user_id(email: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        return (
            (await session.execute(select(User).where(User.email == email))).scalar_one().id
        )


async def _make_run(*, project_id=None, created_by=None) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind="discovery",
            goal="假设树测试任务",
            status="planning",
            cursor=0,
            project_id=project_id,
            created_by=created_by,
        )
        session.add(run)
        await session.commit()
        return run.id


async def _project(client, hdr, *, name="假设树课题") -> uuid.UUID:
    resp = await client.post(
        "/api/projects", json={"name": name, "statement": "s"}, headers=hdr
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def test_single_root_per_run(client):
    """每 run 单根：第二个 parent_id=None 的节点被拒；kind 也要在白名单里。"""
    run_id = await _make_run()
    other_run_id = await _make_run()
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        root = await tree_service.create_node(
            session, run, parent_id=None, kind="hypothesis", statement="根假设"
        )
        assert root.status == "open"
        assert root.parent_id is None
        with pytest.raises(ValueError):
            await tree_service.create_node(
                session, run, parent_id=None, kind="hypothesis", statement="第二个根"
            )
        with pytest.raises(ValueError):
            await tree_service.create_node(
                session, run, parent_id=root.id, kind="theorem", statement="非法 kind"
            )
        # 单根是按 run 算的：另一个 run 建自己的根不受影响
        other = await session.get(VoyageRun, other_run_id)
        other_root = await tree_service.create_node(
            session, other, parent_id=None, kind="hypothesis", statement="另一棵树的根"
        )
        assert other_root.run_id == other_run_id


async def test_parent_must_belong_to_same_run(client):
    """跨 run 挂父 = 父不存在：树是 run 私有资产，不能互相嫁接。"""
    run_a = await _make_run()
    run_b = await _make_run()
    async with get_sessionmaker()() as session:
        a = await session.get(VoyageRun, run_a)
        b = await session.get(VoyageRun, run_b)
        root_a = await tree_service.create_node(
            session, a, parent_id=None, kind="hypothesis", statement="A 的根"
        )
        with pytest.raises(ValueError):
            await tree_service.create_node(
                session, b, parent_id=root_a.id, kind="hypothesis", statement="嫁接"
            )
        with pytest.raises(ValueError):
            await tree_service.create_node(
                session, b, parent_id=uuid.uuid4(), kind="hypothesis", statement="幽灵父"
            )


async def test_status_transitions(client):
    """状态机：open→expanded→validated 合法；跳步 / 从终态出发一律拒绝。"""
    run_id = await _make_run()
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        node = await tree_service.create_node(
            session, run, parent_id=None, kind="hypothesis", statement="根"
        )
        # open 不能直达 validated / refuted
        with pytest.raises(ValueError):
            await tree_service.transition(session, node, "validated")
        with pytest.raises(ValueError):
            await tree_service.transition(session, node, "refuted")
        node = await tree_service.transition(session, node, "expanded")
        assert node.status == "expanded"
        # expanded 不能回 open，也没有 expanded→expanded
        with pytest.raises(ValueError):
            await tree_service.transition(session, node, "open")
        with pytest.raises(ValueError):
            await tree_service.transition(session, node, "expanded")
        node = await tree_service.transition(session, node, "validated")
        assert node.status == "validated"
        # validated 是终态
        with pytest.raises(ValueError):
            await tree_service.transition(session, node, "pruned")


async def test_prune_cascades_to_whole_subtree(client):
    """剪枝级联：被剪节点的整个子树置 pruned；兄弟分支与根不受影响。"""
    run_id = await _make_run()
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        root = await tree_service.create_node(
            session, run, parent_id=None, kind="hypothesis", statement="根"
        )
        a = await tree_service.create_node(
            session, run, parent_id=root.id, kind="hypothesis", statement="分支 A"
        )
        a1 = await tree_service.create_node(
            session, run, parent_id=a.id, kind="experiment", statement="A 的实验"
        )
        a2 = await tree_service.create_node(
            session, run, parent_id=a.id, kind="analysis", statement="A 的分析"
        )
        a1x = await tree_service.create_node(
            session, run, parent_id=a1.id, kind="analysis", statement="孙子节点"
        )
        b = await tree_service.create_node(
            session, run, parent_id=root.id, kind="hypothesis", statement="分支 B"
        )
        # 已 expanded 的子节点也会被级联剪掉（前提没了，留痕不删）
        await tree_service.transition(session, a1, "expanded")
        await tree_service.transition(session, a, "expanded")
        await tree_service.transition(session, a, "pruned")

        statuses = {
            n.id: n.status for n in await tree_service.tree_for_run(session, run_id)
        }
        assert statuses[a.id] == "pruned"
        assert statuses[a1.id] == "pruned"
        assert statuses[a2.id] == "pruned"
        assert statuses[a1x.id] == "pruned"
        assert statuses[root.id] == "open"
        assert statuses[b.id] == "open"
        # 留痕：被剪的行还在，没有删除
        assert len(statuses) == 6


async def test_best_open_node_ordering(client):
    """best_open_node：score 降序取 open 第一个；无评分排最后；空树 None。"""
    run_id = await _make_run()
    async with get_sessionmaker()() as session:
        # 空树 None 安全
        assert await tree_service.best_open_node(session, run_id) is None

        run = await session.get(VoyageRun, run_id)
        root = await tree_service.create_node(
            session, run, parent_id=None, kind="hypothesis", statement="根", score=0.9
        )
        low = await tree_service.create_node(
            session, run, parent_id=root.id, kind="hypothesis", statement="低分", score=0.2
        )
        high = await tree_service.create_node(
            session, run, parent_id=root.id, kind="hypothesis", statement="高分", score=0.8
        )
        unscored = await tree_service.create_node(
            session, run, parent_id=root.id, kind="hypothesis", statement="未评分"
        )
        # 根分最高，但 expanded 后不再是「未扩展」候选
        await tree_service.transition(session, root, "expanded")
        best = await tree_service.best_open_node(session, run_id)
        assert best is not None and best.id == high.id
        # 高分剪掉后轮到低分；未评分的排最后
        await tree_service.transition(session, high, "pruned")
        best = await tree_service.best_open_node(session, run_id)
        assert best is not None and best.id == low.id
        await tree_service.transition(session, low, "pruned")
        best = await tree_service.best_open_node(session, run_id)
        assert best is not None and best.id == unscored.id
        # 全部处理完 → None
        await tree_service.transition(session, unscored, "pruned")
        assert await tree_service.best_open_node(session, run_id) is None


async def test_setters_update_columns(client):
    """set_* 写对应列；grounding 三态结构原样存取（JSON 列 sqlite 兼容）。"""
    run_id = await _make_run()
    grounding = [
        {
            "subclaim": "X 提升 Y",
            "stance": "support",
            "paper_ids": ["p1", "p2"],
            "snippets": ["…证据句…"],
        }
    ]
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        node = await tree_service.create_node(
            session, run, parent_id=None, kind="hypothesis", statement="根"
        )
        await tree_service.set_grounding(session, node, grounding)
        await tree_service.set_novelty(session, node, {"novel": True})
        await tree_service.set_feasibility(session, node, {"gpu_hours": 4})
        await tree_service.set_score(session, node, 0.75)
    async with get_sessionmaker()() as session:
        fresh = await session.get(HypothesisNode, node.id)
        assert fresh.grounding == grounding
        assert fresh.novelty_report == {"novel": True}
        assert fresh.feasibility == {"gpu_hours": 4}
        assert fresh.score == 0.75


async def test_tree_read_api_and_visibility(client):
    """GET 树 / 单节点：主人可读；外人 404（不泄露存在性）；跨 run 节点 404。"""
    owner = await _hdr(client, "hyptree-owner@example.com")
    owner_id = await _user_id("hyptree-owner@example.com")
    project_id = await _project(client, owner)
    run_id = await _make_run(project_id=project_id, created_by=owner_id)
    other_run_id = await _make_run(project_id=project_id, created_by=owner_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        root = await tree_service.create_node(
            session, run, parent_id=None, kind="hypothesis", statement="根假设", score=0.5
        )
        child = await tree_service.create_node(
            session, run, parent_id=root.id, kind="experiment", statement="验证实验"
        )
        other = await session.get(VoyageRun, other_run_id)
        other_root = await tree_service.create_node(
            session, other, parent_id=None, kind="hypothesis", statement="别的树"
        )

    resp = await client.get(f"/api/voyages/{run_id}/hypothesis-tree", headers=owner)
    assert resp.status_code == 200, resp.text
    nodes = resp.json()
    # 不断言顺序：本机容器墙钟会 ±1 秒跳变（#234），created_at 序偶发翻车；
    # 父子结构本来就由前端按 parent_id 拼装，顺序不承载语义
    by_id = {n["id"]: n for n in nodes}
    assert set(by_id) == {str(root.id), str(child.id)}
    assert by_id[str(root.id)]["parent_id"] is None
    assert by_id[str(child.id)]["parent_id"] == str(root.id)
    assert by_id[str(child.id)]["kind"] == "experiment"

    resp = await client.get(
        f"/api/voyages/{run_id}/hypothesis-tree/{child.id}", headers=owner
    )
    assert resp.status_code == 200
    assert resp.json()["statement"] == "验证实验"

    # 节点必须属于路径里的 run：跨 run 直取 404
    resp = await client.get(
        f"/api/voyages/{run_id}/hypothesis-tree/{other_root.id}", headers=owner
    )
    assert resp.status_code == 404

    # 外人（非课题主人、非创建者）：树与单节点都 404
    stranger = await _hdr(client, "hyptree-stranger@example.com")
    resp = await client.get(f"/api/voyages/{run_id}/hypothesis-tree", headers=stranger)
    assert resp.status_code == 404
    resp = await client.get(
        f"/api/voyages/{run_id}/hypothesis-tree/{root.id}", headers=stranger
    )
    assert resp.status_code == 404
