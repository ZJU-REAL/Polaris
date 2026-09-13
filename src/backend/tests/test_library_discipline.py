"""文献库选学科，把学科包这条线接到用户手里（#763）。

闭环判据：建库时选学科 → 抽取按该学科口径 → **方法库检索得到**。最后一环最容易漏，
漏掉的表现是「装了学科包，方法卡也抽了，但方法库里一条都没有」——没有任何报错。
"""

import pytest

from app.services import discipline_packs as dp
from app.services import method_index
from tests.conftest import make_project_with_library, register_and_login


async def test_known_disciplines_includes_the_builtin_pack():
    assert "structural" in dp.known_disciplines()


async def test_library_can_declare_a_discipline(client):
    token = await register_and_login(client, email="owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    _project_id, library_id = await make_project_with_library(client, headers)

    resp = await client.patch(
        f"/api/libraries/{library_id}", json={"discipline": "structural"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["discipline"] == "structural"


async def test_unknown_discipline_is_refused_rather_than_stored(client):
    """存一个匹配不到任何 schema 的名字，表现是「选了学科但口径没变」——
    看起来生效了，其实静默无效。宁可当场拒绝。"""
    token = await register_and_login(client, email="owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    _project_id, library_id = await make_project_with_library(client, headers)

    resp = await client.patch(
        f"/api/libraries/{library_id}", json={"discipline": "astrology"}, headers=headers
    )
    assert resp.status_code == 400
    assert "UNKNOWN_DISCIPLINE" in resp.json()["detail"]


async def test_discipline_can_be_cleared(client):
    token = await register_and_login(client, email="owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    _project_id, library_id = await make_project_with_library(client, headers)

    await client.patch(
        f"/api/libraries/{library_id}", json={"discipline": "structural"}, headers=headers
    )
    resp = await client.patch(
        f"/api/libraries/{library_id}", json={"discipline": None}, headers=headers
    )
    assert resp.status_code == 200
    # 清空 = 回到只用跨学科通用的内置 schema
    assert resp.json()["discipline"] is None


async def test_method_library_looks_at_the_disciplines_cards_too(client):
    """闭环的最后一环：学科方法卡必须能被方法库检索到，否则装了等于没装。"""
    from app.core.db import get_sessionmaker
    from app.models.library_direction import DirectionLibrary

    token = await register_and_login(client, email="owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    _project_id, library_id = await make_project_with_library(client, headers)
    lib_uuid = library_id  # conftest 已给 UUID 对象

    async with get_sessionmaker()() as session:
        # 没声明学科：只认内置 method@1，与学科包出现之前完全一致
        assert await method_index.method_schema_ids(session, lib_uuid) == ["method"]

        library = await session.get(DirectionLibrary, lib_uuid)
        library.discipline = "structural"
        await session.commit()

    async with get_sessionmaker()() as session:
        ids = await method_index.method_schema_ids(session, lib_uuid)
    # 并集而不是替换：结构库里既有本学科论文，也有跨学科综述，两种卡都要检索得到
    assert ids == ["method", "structural.method"]


async def test_no_library_context_means_builtin_only():
    """个人书架导入等没有库上下文的路径不受学科影响。"""
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        assert await method_index.method_schema_ids(session, None) == ["method"]


@pytest.mark.parametrize("discipline", ["structural"])
async def test_declared_discipline_scopes_extraction(client, discipline):
    """库声明学科后，抽取范围 = 内置 + 该学科；别的学科的 schema 不掺进来。"""
    from app.services.extraction.schemas import schemas_for

    ids = {s.id for s in schemas_for(discipline)}
    assert "structural.method" in ids
    assert {"skeleton", "method", "gaps"} <= ids
