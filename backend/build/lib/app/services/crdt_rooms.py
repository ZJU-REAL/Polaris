"""CRDT 协同房间（docs/api-m5-b.md §6，不 import fastapi）。

- pycrdt.websocket WebsocketServer 单例（懒启动），房间名 = ManuscriptFile.id；
- Y doc 结构：Text 命名 "content"；房间新建时从 ManuscriptFile.content 初始化；
- 快照：文档更新防抖 2s 写回 ManuscriptFile.content（编译/AI/REST 读到的即最新），
  服务重启后房间从库重建；
- apply_ai_edit：写作 voyage 分节写入——有活跃房间时经 Y 事务区间替换（协同者实时
  可见）并立即快照落库，无房间时直接改库。
"""

import asyncio
import logging
import re
import uuid

from pycrdt import Text
from pycrdt.websocket import WebsocketServer, YRoom

from app.core.db import get_sessionmaker
from app.models.manuscript import ManuscriptFile

logger = logging.getLogger("polaris.crdt")

SNAPSHOT_DEBOUNCE_SECONDS = 2.0

_ANY_SECTION_RE = re.compile(r"^[ \t]*%\s*POLARIS_SECTION(?:_END)?:", re.MULTILINE)


def section_span(content: str, section: str) -> tuple[int, int] | None:
    """定位 ``% POLARIS_SECTION: x`` 标记区间（begin 标记行之后到 END 标记行/下一
    标记行/文末），返回 (start, end) 字符偏移；无 begin 标记返回 None。"""
    begin_re = re.compile(
        rf"^[ \t]*%\s*POLARIS_SECTION:\s*{re.escape(section)}[ \t]*$", re.MULTILINE
    )
    m = begin_re.search(content)
    if m is None:
        return None
    start = content.find("\n", m.end())
    if start == -1:  # begin 标记就是最后一行
        return len(content), len(content)
    start += 1
    end_re = re.compile(
        rf"^[ \t]*%\s*POLARIS_SECTION_END:\s*{re.escape(section)}[ \t]*$", re.MULTILINE
    )
    m_end = end_re.search(content, start)
    if m_end is not None:
        return start, m_end.start()
    m_next = _ANY_SECTION_RE.search(content, start)
    return start, m_next.start() if m_next else len(content)


def replace_section(content: str, section: str, body: str) -> str:
    """区间替换；无标记时在文末追加带标记的新区块。"""
    body = body.strip("\n") + "\n"
    span = section_span(content, section)
    if span is None:
        suffix = f"\n% POLARIS_SECTION: {section}\n{body}% POLARIS_SECTION_END: {section}\n"
        return content + suffix
    start, end = span
    return content[:start] + body + content[end:]


class CRDTRoomManager:
    """WebsocketServer 单例封装：房间生命周期 + 防抖快照 + AI 区间写入。"""

    def __init__(self) -> None:
        self._server: WebsocketServer | None = None
        self._server_task: asyncio.Task | None = None
        self._debounce_tasks: dict[str, asyncio.Task] = {}
        self._initialized: set[str] = set()

    # ---- 服务器 / 房间 ----

    async def _ensure_server(self) -> WebsocketServer:
        if self._server is None:
            # auto_clean_rooms=False：客户端全部断开后房间保留（内容与库一致，
            # 内存占用可忽略；进程重启后房间自然从库重建）
            self._server = WebsocketServer(auto_clean_rooms=False, log=logger)
        if not self._server.started.is_set():
            if self._server_task is None or self._server_task.done():
                self._server_task = asyncio.create_task(self._server.start())
            await self._server.started.wait()
        return self._server

    def active_room(self, file_id: uuid.UUID | str) -> YRoom | None:
        if self._server is None:
            return None
        return self._server.rooms.get(str(file_id))

    def room_content(self, file_id: uuid.UUID | str) -> str | None:
        """活跃房间的当前文本（REST 读文件 / 编译组装时优先于库内容）。"""
        room = self.active_room(file_id)
        if room is None:
            return None
        return str(room.ydoc.get("content", type=Text))

    async def connect(self, *, file_id: uuid.UUID, db_content: str) -> YRoom:
        """取/建房间：新建时从库内容初始化 Text 并挂防抖快照观察者。"""
        server = await self._ensure_server()
        name = str(file_id)
        room = await server.get_room(name)
        if name not in self._initialized:
            self._initialized.add(name)
            text = room.ydoc.get("content", type=Text)
            if len(text) == 0 and db_content:
                text += db_content
            loop = asyncio.get_running_loop()
            room.ydoc.observe(
                lambda _event, fid=name: loop.call_soon_threadsafe(self._schedule_snapshot, fid)
            )
        return room

    async def serve(self, websocket) -> None:
        """把一个 Channel（path=房间名）接入服务器（api/ws.py 桥接后调用）。"""
        server = await self._ensure_server()
        await server.serve(websocket)

    # ---- 快照 ----

    def _schedule_snapshot(self, fid: str) -> None:
        pending = self._debounce_tasks.get(fid)
        if pending is not None and not pending.done():
            pending.cancel()
        self._debounce_tasks[fid] = asyncio.create_task(self._debounced_snapshot(fid))

    async def _debounced_snapshot(self, fid: str) -> None:
        try:
            await asyncio.sleep(SNAPSHOT_DEBOUNCE_SECONDS)
            await self.snapshot(fid)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 快照失败不炸房间，下次更新会再试
            logger.exception("CRDT 快照落库失败：file=%s", fid)

    async def snapshot(self, file_id: uuid.UUID | str) -> None:
        """把房间当前文本写回 ManuscriptFile.content（无房间/无变化则跳过）。"""
        content = self.room_content(file_id)
        if content is None:
            return
        async with get_sessionmaker()() as session:
            file = await session.get(ManuscriptFile, uuid.UUID(str(file_id)))
            if file is not None and file.content != content:
                file.content = content
                await session.commit()

    async def flush(self, file_id: uuid.UUID | str) -> None:
        """取消防抖并立即快照（编译组装前调用，保证读到最新）。"""
        pending = self._debounce_tasks.pop(str(file_id), None)
        if pending is not None and not pending.done():
            pending.cancel()
        await self.snapshot(file_id)

    # ---- AI 写入 ----

    async def apply_ai_edit(self, file_id: uuid.UUID, section_marker: str, content: str) -> bool:
        """把节内容写入 ``% POLARIS_SECTION: x`` 标记区间。

        有活跃房间：经 Y 事务区间替换（协同者实时可见）并立即快照落库，返回 True；
        无房间：直接读改写库，返回 False。
        """
        room = self.active_room(file_id)
        if room is not None:
            text = room.ydoc.get("content", type=Text)
            current = str(text)
            new = replace_section(current, section_marker, content)
            if new != current:
                span = section_span(current, section_marker)
                with room.ydoc.transaction():
                    if span is None:  # 无标记：整段追加
                        text += new[len(current) :]
                    else:
                        # pycrdt Text 索引是 UTF-8 字节偏移：字符区间换算成字节
                        start, end = span
                        start_b = len(current[:start].encode("utf-8"))
                        end_b = start_b + len(current[start:end].encode("utf-8"))
                        body = content.strip("\n") + "\n"
                        if end_b > start_b:
                            del text[start_b:end_b]
                        text.insert(start_b, body)
            await self.flush(file_id)
            return True

        async with get_sessionmaker()() as session:
            file = await session.get(ManuscriptFile, file_id)
            if file is None:
                raise ValueError(f"manuscript file not found: {file_id}")
            file.content = replace_section(file.content, section_marker, content)
            await session.commit()
        return False

    # ---- 生命周期 ----

    async def shutdown(self) -> None:
        for task in self._debounce_tasks.values():
            if not task.done():
                task.cancel()
        self._debounce_tasks.clear()
        if self._server is not None and self._server.started.is_set():
            await self._server.stop()
        if self._server_task is not None and not self._server_task.done():
            self._server_task.cancel()
        self._server = None
        self._server_task = None
        self._initialized.clear()


_manager: CRDTRoomManager | None = None


def get_crdt_rooms() -> CRDTRoomManager:
    global _manager
    if _manager is None:
        _manager = CRDTRoomManager()
    return _manager


async def reset_crdt_rooms() -> None:
    """测试用：关停并丢弃单例。"""
    global _manager
    if _manager is not None:
        await _manager.shutdown()
    _manager = None
