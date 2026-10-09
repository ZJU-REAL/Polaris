"""建库即可用、读写端点、删除（#842：单用户本地应用，没有个人/公共之分）。

- 建库 → 即刻可用（转公共审批流、公开开关都已删除）；
- 删除：登录即可，不看是谁建的。
"""

import uuid

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary
from tests.conftest import register_and_login


async def _hdr(client):
    return {"Authorization": f"Bearer {await register_and_login(client)}"}


async def _create(client, headers, name="用户建的库"):
    resp = await client.post(
        "/api/libraries",
        json={"name": name, "statement": "一句话方向陈述", "anchors": ["2401.00001"]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_created_library_is_ready_to_use(client):
    headers = await _hdr(client)
    lib_id = await _create(client, headers)
    async with get_sessionmaker()() as session:
        lib = await session.get(DirectionLibrary, uuid.UUID(lib_id))
        # 建库的人照样记下来（ingest 用量记在他名下），只是不再用于权限
        assert lib.submitted_by is not None
        assert lib.definition["anchor_papers"] == ["2401.00001"]


async def test_new_library_can_ingest_without_approval(client, queue_stub):
    headers = await _hdr(client)
    lib_id = await _create(client, headers)
    resp = await client.post(
        f"/api/libraries/{lib_id}/ingest/run", json={"mode": "bootstrap"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    assert queue_stub.jobs, "新库触发应入队"


async def test_library_read_endpoints_work(client):
    """库的只读端点（papers/concepts/graph/notes/建库同步状态）都读得到。"""
    headers = await _hdr(client)
    lib_id = await _create(client, headers, name="只读端点库")

    read_paths = [
        f"/api/libraries/{lib_id}/papers",
        f"/api/libraries/{lib_id}/concepts",
        f"/api/libraries/{lib_id}/graph",
        f"/api/libraries/{lib_id}/notes",
        f"/api/libraries/{lib_id}/ingest/state",
    ]
    for path in read_paths:
        resp = await client.get(path, headers=headers)
        assert resp.status_code == 200, (path, resp.status_code)


async def test_library_list_has_no_ownership_filter(client):
    """列表就是全部库：没有个人/公共的筛选，旧的 ``?type=`` 参数被忽略。"""
    headers = await _hdr(client)
    first = await _create(client, headers, name="甲")
    second = await _create(client, headers, name="乙")
    for url in ("/api/libraries", "/api/libraries?type=public"):
        resp = await client.get(url, headers=headers)
        assert resp.status_code == 200, resp.text
        assert {first, second} <= {x["id"] for x in resp.json()}


async def test_digest_list_and_detail_are_readable(client):
    """每日简报的列表与正文都能读到。"""
    import datetime as dt

    from app.models.research_digest import LibraryResearchDigest

    headers = await _hdr(client)
    lib_id = await _create(client, headers, name="有简报的库")

    async with get_sessionmaker()() as session:
        session.add(
            LibraryResearchDigest(
                library_id=uuid.UUID(lib_id),
                report_date=dt.date(2026, 7, 30),
                source="voyage",
                mode="incremental",
                counts={"kept": 2},
                source_diagnostics={},
                paper_insights=[],
                excluded_papers=[],
                cross_paper_signals=[],
                summary="本期两篇。",
                content="# 每日文献简报",
                rolling_trends=[],
                trend_content="",
            )
        )
        await session.commit()

    resp = await client.get(f"/api/libraries/{lib_id}/digests", headers=headers)
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1 and rows[0]["counts"]["kept"] == 2

    resp = await client.get(f"/api/libraries/{lib_id}/digests/{rows[0]['id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"].startswith("# 每日文献简报")


async def test_library_can_be_deleted(client):
    headers = await _hdr(client)
    lib_id = await _create(client, headers)
    resp = await client.delete(f"/api/libraries/{lib_id}", headers=headers)
    assert resp.status_code == 204, resp.text


async def test_library_without_a_recorded_creator_can_be_deleted(client):
    """老库可能没有 submitted_by（存量/系统建）：照样能删，不再看是谁建的。"""
    headers = await _hdr(client)
    lib_id = await _create(client, headers)
    async with get_sessionmaker()() as session:
        lib = await session.get(DirectionLibrary, uuid.UUID(lib_id))
        lib.submitted_by = None
        lib.is_public = True  # 遗留列：不该影响任何行为
        await session.commit()
    assert (await client.get(f"/api/libraries/{lib_id}", headers=headers)).status_code == 200
    resp = await client.delete(f"/api/libraries/{lib_id}", headers=headers)
    assert resp.status_code == 204, resp.text


def test_ingest_usage_is_recorded_under_the_creator():
    """ingest 的用量记在建库的人名下，不再看遗留的 is_public 列。"""
    from app.agents.voyage.actions_wiki import _ingest_billing_owner

    owner_id = uuid.uuid4()
    for flag in (True, False):
        library = DirectionLibrary(name="a", is_public=flag, submitted_by=owner_id)
        assert _ingest_billing_owner(library) == owner_id
