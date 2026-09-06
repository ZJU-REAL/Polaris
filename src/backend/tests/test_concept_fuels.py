"""概念燃料（P2.5 F3，#664）：共现挖掘 mine_unconnected_pairs 与 concept-pairs 端点。

小图夹具（权重 = 共现论文数）：

    alpha ──2── bridge-one ──2── gamma
    alpha ──1── bridge-two ──1── gamma
                bridge-two ──1── delta ──1── gamma

alpha–gamma 从未同篇出现 → 是最强候选：Σ min = min(2,2) + min(1,1) = 3。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary
from app.models.user import User
from app.services import concept_fuels

from .conftest import add_concept, add_paper, make_project_with_library, register_and_login

# 期望的完整挖掘结果：(概念名对, 总强度, [(桥名, 桥强度), ...])，按总强度降序
EXPECTED = [
    (("alpha", "gamma"), 3, [("bridge-one", 2), ("bridge-two", 1)]),
    (("bridge-one", "bridge-two"), 2, [("alpha", 1), ("gamma", 1)]),
    (("alpha", "delta"), 1, [("bridge-two", 1)]),
    (("bridge-one", "delta"), 1, [("gamma", 1)]),
]


async def _setup(client):
    """建库并造出模块 docstring 里的小图，返回 (headers, library_id)。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="fuel-proj")
    pid = uuid.UUID(project_id)

    async with get_sessionmaker()() as session:
        names = ["alpha", "bridge-one", "bridge-two", "gamma", "delta"]
        c = {}
        for name in names:
            c[name] = await add_concept(session, project_id=pid, name=name, slug=f"s-{name}")
        # 候选概念（还没转正）不参与挖掘
        cand = await add_concept(
            session, project_id=pid, name="cand-x", slug="s-cand-x", status="candidate"
        )
        groups = [
            ["alpha", "bridge-one"],
            ["alpha", "bridge-one"],
            ["bridge-one", "gamma"],
            ["bridge-one", "gamma"],
            ["alpha", "bridge-two"],
            ["bridge-two", "gamma", "delta"],
        ]
        for i, group in enumerate(groups):
            await add_paper(
                session,
                project_id=pid,
                title=f"P{i}",
                status="included",
                concepts=[c[n] for n in group],
            )
        # 未过筛选的论文（默认 status=candidate）不算共现：alpha–gamma 必须仍是未连接对
        await add_paper(
            session, project_id=pid, title="unscored", concepts=[c["alpha"], c["gamma"]]
        )
        # 候选概念同篇也不进图
        await add_paper(
            session,
            project_id=pid,
            title="with-cand",
            status="included",
            concepts=[c["alpha"], cand],
        )
        await session.commit()
    return headers, library_id


def _shape(pairs):
    """把服务返回压成 EXPECTED 的形状，便于整表断言。

    对内的名字按字典序放（展示序是 uuid 规范序，随机 uuid 下两种都可能），
    EXPECTED 里的名字对也都写成字典序。"""
    return [
        (
            tuple(sorted((p["concept_a"]["name"], p["concept_c"]["name"]))),
            p["strength"],
            [(b["name"], b["strength"]) for b in p["bridges"]],
        )
        for p in pairs
    ]


async def test_mine_unconnected_pairs_exact(client):
    _headers, library_id = await _setup(client)
    async with get_sessionmaker()() as session:
        pairs = await concept_fuels.mine_unconnected_pairs(session, library_id=library_id)

    assert _shape(pairs) == EXPECTED
    # 规范序：concept_a.id < concept_c.id（uuid 字符串序 == uuid 数值序）
    for p in pairs:
        assert p["concept_a"]["id"] < p["concept_c"]["id"]
    # 候选概念不出现在任何位置
    all_names = {
        n
        for p in pairs
        for n in (
            p["concept_a"]["name"],
            p["concept_c"]["name"],
            *[b["name"] for b in p["bridges"]],
        )
    }
    assert "cand-x" not in all_names


async def test_other_library_cooccurrence_does_not_leak(client):
    # 同两个概念在别的库共现，不影响本库的「从未共现」判定（共现是库维度的）
    headers, library_id = await _setup(client)
    project2, _lib2 = await make_project_with_library(client, headers, name="fuel-proj-2")
    pid2 = uuid.UUID(project2)
    async with get_sessionmaker()() as session:
        rows = await concept_fuels.library_cooccurrence(session, library_id=library_id)
        names = set(rows[0].values())
        assert names == {"alpha", "bridge-one", "bridge-two", "gamma", "delta"}
        # 在库 2 里让 alpha 与 gamma 同篇出现
        from app.models.paper import Concept

        c = {
            concept.name: concept
            for concept in (
                (await session.execute(select(Concept).where(Concept.name.in_(["alpha", "gamma"]))))
                .scalars()
                .all()
            )
        }
        await add_paper(
            session,
            project_id=pid2,
            title="elsewhere",
            status="included",
            concepts=[c["alpha"], c["gamma"]],
        )
        await session.commit()
        pairs = await concept_fuels.mine_unconnected_pairs(session, library_id=library_id)
    assert _shape(pairs)[0] == EXPECTED[0]  # (alpha, gamma) 仍是本库的未连接对


async def test_top_n_truncates(client):
    _headers, library_id = await _setup(client)
    async with get_sessionmaker()() as session:
        pairs = await concept_fuels.mine_unconnected_pairs(session, library_id=library_id, top_n=2)
    assert _shape(pairs) == EXPECTED[:2]


async def test_bridges_capped_at_five(client):
    # 6 条桥都成立时只保留最强的 5 条（总强度仍算全部桥）
    token = await register_and_login(client, email="cap@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="cap-proj")
    pid = uuid.UUID(project_id)
    async with get_sessionmaker()() as session:
        a = await add_concept(session, project_id=pid, name="left", slug="s-left")
        cc = await add_concept(session, project_id=pid, name="right", slug="s-right")
        for i in range(6):
            b = await add_concept(session, project_id=pid, name=f"mid-{i}", slug=f"s-mid-{i}")
            for pair in ([a, b], [b, cc]):
                await add_paper(
                    session,
                    project_id=pid,
                    title=f"p-{i}-{pair[0].name}",
                    status="included",
                    concepts=pair,
                )
        await session.commit()
        pairs = await concept_fuels.mine_unconnected_pairs(session, library_id=library_id)
    (pair,) = [
        p for p in pairs if {p["concept_a"]["name"], p["concept_c"]["name"]} == {"left", "right"}
    ]
    assert pair["strength"] == 6
    assert len(pair["bridges"]) == concept_fuels.MAX_BRIDGES_PER_PAIR
    assert [b["name"] for b in pair["bridges"]] == [f"mid-{i}" for i in range(5)]  # 并列按名字


async def test_prune_branch_keeps_top_degree(client, monkeypatch):
    # 概念数超阈值走防爆分支：按度数（并列按共现总权重）保留 top 4 → delta 被裁掉
    _headers, library_id = await _setup(client)
    monkeypatch.setattr(concept_fuels, "COOCCURRENCE_MAX_CONCEPTS", 4)
    async with get_sessionmaker()() as session:
        pairs = await concept_fuels.mine_unconnected_pairs(session, library_id=library_id)
    assert _shape(pairs) == [
        (("alpha", "gamma"), 3, [("bridge-one", 2), ("bridge-two", 1)]),
        (("bridge-one", "bridge-two"), 2, [("alpha", 1), ("gamma", 1)]),
    ]


async def test_concept_pairs_endpoint(client):
    headers, library_id = await _setup(client)
    resp = await client.get(f"/api/libraries/{library_id}/concept-pairs?top=1", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    pair = body[0]
    assert {pair["concept_a"]["name"], pair["concept_c"]["name"]} == {"alpha", "gamma"}
    assert pair["strength"] == 3
    assert [(b["name"], b["strength"]) for b in pair["bridges"]] == [
        ("bridge-one", 2),
        ("bridge-two", 1),
    ]


async def test_concept_pairs_personal_library_hidden(client):
    # 个人库仅创建者可见：陌生人访问按 404（与其他只读端点同口径）
    token_owner = await register_and_login(client, email="owner@example.com")
    owner_headers = {"Authorization": f"Bearer {token_owner}"}
    async with get_sessionmaker()() as session:
        owner = (
            await session.execute(select(User).where(User.email == "owner@example.com"))
        ).scalar_one()
        library = DirectionLibrary(
            name="private-lib", is_public=False, submitted_by=owner.id, created_by=owner.id
        )
        session.add(library)
        await session.commit()
        library_id = library.id

    token_other = await register_and_login(client, email="other@example.com")
    resp = await client.get(
        f"/api/libraries/{library_id}/concept-pairs",
        headers={"Authorization": f"Bearer {token_other}"},
    )
    assert resp.status_code == 404
    resp = await client.get(f"/api/libraries/{library_id}/concept-pairs", headers=owner_headers)
    assert resp.status_code == 200
    assert resp.json() == []  # 空库 → 空结果而不是报错
