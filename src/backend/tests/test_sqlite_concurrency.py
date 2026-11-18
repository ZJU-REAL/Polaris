"""SQLite 在并发访问下（几个浏览器同时打开引擎、后台任务同时在写）不出错。

起因：四个浏览器同时加载引擎时报过 ``disk I/O error``，当时数据库放在 macOS 目录里、
挂进 docker 容器。复现结论（同一套引擎代码、同样的负载，30 秒）：

- 数据库在容器自己的文件系统或 docker volume 上：十万次以上请求 0 失败；
- 在挂进容器的 macOS 目录上、只有容器里的引擎在用：同样 0 失败；
- 同一个文件**同时被宿主机上的进程打开**（sqlite3 / 脚本读它）：容器里 84,033 次请求
  47,817 次失败（disk I/O error / unable to open database file），宿主机那边读到
  database disk image is malformed。WAL 的 -shm 共享内存文件在 macOS 和 Linux 虚拟机
  之间不是同一块内存，两边一起用就坏。

所以这是开发环境的挂载方式问题，不是引擎的并发问题；这个用例守的是后者——在本机文件
系统上，几十个并发请求加后台写入不出任何数据库错误。
"""

import asyncio
import uuid

from sqlalchemy import text

from app.core.db import get_sessionmaker
from tests.conftest import register_and_login

BROWSERS = 4
REQUESTS_PER_BROWSER = 12
ROUNDS = 5


async def test_concurrent_page_loads_and_writes_do_not_hit_database_errors(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/projects", json={"name": "并发", "statement": "并发访问"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]
    pages = ["/api/projects", "/api/research", f"/api/projects/{project_id}", "/api/health"]

    async with get_sessionmaker()() as session:
        await session.execute(text("create table if not exists probe_writes (id text)"))
        await session.commit()

    async def writer() -> None:
        # 后台任务在写：一条条小事务提交
        for _ in range(ROUNDS * 10):
            async with get_sessionmaker()() as session:
                await session.execute(
                    text("insert into probe_writes (id) values (:id)"), {"id": str(uuid.uuid4())}
                )
                await session.commit()
            await asyncio.sleep(0)

    async def browser(n: int) -> list[int]:
        codes: list[int] = []
        for _ in range(ROUNDS):
            responses = await asyncio.gather(
                *(
                    client.get(pages[(n + i) % len(pages)], headers=headers)
                    for i in range(REQUESTS_PER_BROWSER)
                )
            )
            codes += [r.status_code for r in responses]
        return codes

    results = await asyncio.gather(writer(), *(browser(n) for n in range(BROWSERS)))
    codes = [code for batch in results[1:] for code in batch]
    assert len(codes) == BROWSERS * REQUESTS_PER_BROWSER * ROUNDS
    assert set(codes) == {200}
    async with get_sessionmaker()() as session:
        written = (await session.execute(text("select count(*) from probe_writes"))).scalar_one()
    assert written == ROUNDS * 10
