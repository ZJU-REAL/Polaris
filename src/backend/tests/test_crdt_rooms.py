"""M5-B CRDT 协同房间测试（docs/task-system.md §7，pycrdt 直连房间不走 WS 传输层）：

- section_span / replace_section 标记区间定位与追加；
- apply_ai_edit 无房间路径：直写库；
- 房间路径：从库内容初始化 Y Text → 直连写入 → 防抖快照落库一致；
  apply_ai_edit 经 Y 事务区间替换（房间与库同步更新）；
- WS 端点鉴权关闭码所需的 file 查询（成员 / readonly）。
"""

import asyncio
import uuid

import pytest_asyncio
from pycrdt import Doc, Text

from app.core.db import get_sessionmaker
from app.models.manuscript import ManuscriptFile
from app.services import crdt_rooms
from app.services import manuscripts as manuscripts_service
from app.services.crdt_rooms import (
    get_crdt_rooms,
    replace_section,
    reset_crdt_rooms,
    section_span,
)
from tests.test_manuscripts import _create_manuscript, _setup_project

SKELETON = """\\documentclass{article}
\\begin{document}
\\section{Introduction}
% POLARIS_SECTION: introduction
（待撰写 / to be drafted）
% POLARIS_SECTION_END: introduction

\\section{Results}
% POLARIS_SECTION: results
old results body
% POLARIS_SECTION_END: results
\\end{document}
"""


@pytest_asyncio.fixture(autouse=True)
async def _clean_crdt():
    yield
    await reset_crdt_rooms()


def test_section_span_and_replace():
    start, end = section_span(SKELETON, "introduction")
    assert SKELETON[start:end] == "（待撰写 / to be drafted）\n"
    replaced = replace_section(SKELETON, "introduction", "New intro.\nSecond line.")
    assert "New intro.\nSecond line.\n% POLARIS_SECTION_END: introduction" in replaced
    assert "（待撰写 / to be drafted）" not in replaced
    assert "old results body" in replaced  # 其他节不受影响

    # 无标记 → 文末追加带标记的新区块
    appended = replace_section("no markers here\n", "conclusion", "The end.")
    assert appended.endswith(
        "% POLARIS_SECTION: conclusion\nThe end.\n% POLARIS_SECTION_END: conclusion\n"
    )
    assert section_span(appended, "conclusion") is not None

    # 无 END 标记：区间到下一标记行
    no_end = "% POLARIS_SECTION: a\nbody a\n% POLARIS_SECTION: b\nbody b\n"
    start, end = section_span(no_end, "a")
    assert no_end[start:end] == "body a\n"
    start, end = section_span(no_end, "b")  # 文末
    assert no_end[start:end] == "body b\n"


async def _seed_file(client) -> tuple[str, dict, uuid.UUID]:
    project_id, headers = await _setup_project(client)
    resp = await _create_manuscript(client, headers, project_id)
    ms_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        file = ManuscriptFile(manuscript_id=uuid.UUID(ms_id), path="draft.tex", content=SKELETON)
        session.add(file)
        await session.commit()
        return ms_id, headers, file.id


async def test_apply_ai_edit_without_room_writes_db(client):
    _, _, file_id = await _seed_file(client)
    via_room = await get_crdt_rooms().apply_ai_edit(file_id, "introduction", "AI intro body.")
    assert via_room is False
    async with get_sessionmaker()() as session:
        file = await session.get(ManuscriptFile, file_id)
        assert "AI intro body.\n% POLARIS_SECTION_END: introduction" in file.content
        assert "old results body" in file.content


async def test_room_snapshot_debounce_and_ai_edit(client, monkeypatch):
    monkeypatch.setattr(crdt_rooms, "SNAPSHOT_DEBOUNCE_SECONDS", 0.05)
    _, _, file_id = await _seed_file(client)
    manager = get_crdt_rooms()

    # 房间创建：从 ManuscriptFile.content 初始化 Y Text "content"
    room = await manager.connect(file_id=file_id, db_content=SKELETON)
    text = room.ydoc.get("content", type=Text)
    assert str(text) == SKELETON
    assert manager.active_room(file_id) is room
    assert manager.room_content(file_id) == SKELETON

    # 模拟协作者直连写入 → 防抖后库内容一致
    text += "% appended by collaborator\n"
    await asyncio.sleep(0.3)
    async with get_sessionmaker()() as session:
        file = await session.get(ManuscriptFile, file_id)
        assert file.content == str(text)
        assert file.content.endswith("% appended by collaborator\n")

    # AI 写入：有活跃房间 → 经房间事务替换区间 + 立即落库
    via_room = await manager.apply_ai_edit(file_id, "results", "New results body.")
    assert via_room is True
    current = str(room.ydoc.get("content", type=Text))
    assert "New results body.\n% POLARIS_SECTION_END: results" in current
    assert "old results body" not in current
    assert "% appended by collaborator" in current  # 协作者内容保留
    async with get_sessionmaker()() as session:
        file = await session.get(ManuscriptFile, file_id)
        assert file.content == current  # 立即快照，不等防抖

    # 重连同一房间不重复初始化（内容不翻倍）
    room2 = await manager.connect(file_id=file_id, db_content="ignored")
    assert room2 is room
    assert str(room2.ydoc.get("content", type=Text)) == current


async def test_room_rebuild_after_restart_does_not_duplicate_document(client):
    """#347：进程重启后房间按原 CRDT 历史恢复，幸存标签页重连不把文档变成两份。

    现场是「126 行变 249 行、\\documentclass 同时在第 3 行和第 127 行」。成因不是
    合并算法出错，而是重建方式：拿纯文本往空文档里灌，产生的是一套全新历史，
    和标签页手里那套互不认识——CRDT 忠实地把两套插入操作都保留下来，于是全文
    出现两份。这里把那条时间线原样走一遍。
    """
    _, _, file_id = await _seed_file(client)
    manager = get_crdt_rooms()

    # 1) 编辑中：房间在，浏览器标签页持有同一套历史（y-websocket 同步的效果）
    room = await manager.connect(file_id=file_id, db_content=SKELETON)
    tab = Doc()
    tab.apply_update(room.ydoc.get_update())
    tab_text = tab.get("content", type=Text)
    assert str(tab_text) == SKELETON

    # 标签页里敲一笔，同步回房间；随后落库（编译前的 flush 就是这一步）
    tab_text += "% typed in the tab\n"
    room.ydoc.apply_update(tab.get_update())
    await manager.flush(file_id)

    async with get_sessionmaker()() as session:
        file = await session.get(ManuscriptFile, file_id)
        before = file.content
        assert file.ydoc_state, "快照必须连 CRDT 状态一起落库，否则重启只能拿文本重建"

    # 2) 进程重启：房间与内存态全没了，库里只剩两列
    await reset_crdt_rooms()
    manager = get_crdt_rooms()
    assert manager.active_room(file_id) is None

    # 3) 标签页自动重连：房间按 ydoc_state 恢复，两边互相同步
    async with get_sessionmaker()() as session:
        file = await session.get(ManuscriptFile, file_id)
        room2 = await manager.connect(
            file_id=file_id, db_content=file.content, ydoc_state=file.ydoc_state
        )
    room2.ydoc.apply_update(tab.get_update())
    tab.apply_update(room2.ydoc.get_update())

    merged = str(room2.ydoc.get("content", type=Text))
    assert merged == before, "重连合并后内容必须与重启前一致"
    assert merged == str(tab_text), "房间与标签页收敛到同一份"
    # 逐份点名，别只看长度：重复时这些标记会各出现两次
    assert merged.count("\\documentclass") == 1
    assert merged.count("\\begin{document}") == 1
    assert merged.count("\\end{document}") == 1
    assert merged.count("% typed in the tab") == 1


async def test_room_rebuild_without_state_still_seeds_from_text(client):
    """存量文件（升级前落的库，ydoc_state 为 NULL）仍按老路子从文本播种。"""
    _, _, file_id = await _seed_file(client)
    manager = get_crdt_rooms()

    room = await manager.connect(file_id=file_id, db_content=SKELETON, ydoc_state=None)
    assert str(room.ydoc.get("content", type=Text)) == SKELETON


async def test_room_rebuild_prefers_db_text_edited_while_closed(client):
    """房间不在时库文本被直接改过（AI 分节写入 / 版本回滚 / REST 保存）：
    以库为准，但替换发生在恢复出来的那套历史里，重连的标签页照样收敛。"""
    _, _, file_id = await _seed_file(client)
    manager = get_crdt_rooms()

    room = await manager.connect(file_id=file_id, db_content=SKELETON)
    tab = Doc()
    tab.apply_update(room.ydoc.get_update())
    await manager.flush(file_id)

    await reset_crdt_rooms()
    manager = get_crdt_rooms()

    # 房间不在时有人直接改库（apply_ai_edit 的无房间路径就是这样）
    edited = SKELETON.replace("old results body", "rewritten while the room was closed")
    async with get_sessionmaker()() as session:
        file = await session.get(ManuscriptFile, file_id)
        state = file.ydoc_state
        file.content = edited
        await session.commit()

    room2 = await manager.connect(file_id=file_id, db_content=edited, ydoc_state=state)
    room2.ydoc.apply_update(tab.get_update())
    merged = str(room2.ydoc.get("content", type=Text))
    assert "rewritten while the room was closed" in merged
    assert "old results body" not in merged
    assert merged.count("\\documentclass") == 1


async def test_stream_section_open_delta_replace(client):
    """流式镜像：open 清空占位 → delta 逐块追加 → replace 覆盖对齐。"""
    _, _, file_id = await _seed_file(client)
    manager = get_crdt_rooms()

    # 无活跃房间 → no-op（worker DB 写为准）
    assert await manager.stream_section(file_id, "introduction", op="delta", text="x") is False

    room = await manager.connect(file_id=file_id, db_content=SKELETON)
    text = room.ydoc.get("content", type=Text)

    # open：清空「待撰写」占位（正文留单换行，END 标记独占一行的不变式）
    assert await manager.stream_section(file_id, "introduction", op="open") is True
    span = section_span(str(text), "introduction")
    assert str(text)[span[0] : span[1]] == "\n"
    assert "（待撰写" not in str(text)
    assert "% POLARIS_SECTION_END: introduction" in str(text)  # 标记完好

    # delta：逐块追加到该节末尾（含 CJK，标记始终完好）
    await manager.stream_section(file_id, "introduction", op="delta", text="Hello ")
    await manager.stream_section(file_id, "introduction", op="delta", text="world 世界.")
    span = section_span(str(text), "introduction")
    assert str(text)[span[0] : span[1]].strip() == "Hello world 世界."
    assert "Hello world 世界.\n% POLARIS_SECTION_END: introduction" in str(text)
    assert "old results body" in str(text)  # 其他节不受影响

    # replace：整节覆盖（去围栏后的干净稿）
    await manager.stream_section(file_id, "introduction", op="replace", text="Final intro.")
    current = str(text)
    assert "Final intro.\n% POLARIS_SECTION_END: introduction" in current
    assert "Hello world" not in current


async def test_ws_connect_guards_file_lookup(client):
    """WS on_connect 用的查询：非成员不可见；readonly 文件拒开房间（4403 依据）。"""
    ms_id, headers, file_id = await _seed_file(client)
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    sty = next(f for f in detail["files"] if f["path"].endswith(".sty"))

    async with get_sessionmaker()() as session:
        from sqlalchemy import select

        from app.models.user import User

        owner = (
            (await session.execute(select(User).where(User.email == "alice@example.com")))
            .scalars()
            .one()
        )
        file = await manuscripts_service.get_file_for_user(
            session, file_id=file_id, user_id=owner.id
        )
        assert file is not None and file.readonly is False
        sty_file = await manuscripts_service.get_file_for_user(
            session, file_id=uuid.UUID(sty["id"]), user_id=owner.id
        )
        assert sty_file is not None and sty_file.readonly is True  # → 4403
        # 非成员：查不到（→ 4404）
        assert (
            await manuscripts_service.get_file_for_user(
                session, file_id=file_id, user_id=uuid.uuid4()
            )
            is None
        )
