"""Golden transcript：注册 → 建库 → 建课题 → bibtex 导入 → wiki 编译 → 索引状态。

整条链在 fake provider + sqlite 上确定性运行，每步响应归一化后与
``tests/golden/data/import_wiki.json`` 逐字节比对。

这是 B 轨（去实验室化移除）的回归闸门：**纯移除 PR 必须保持 golden 不变**。
golden 变了 = 行为变了，需要人工审查 diff 并说明理由。

更新方式（仅限本地，CI 只比对，纪律来自 DSH 的 snapshot 流程）::

    POLARIS_GOLDEN=record python -m pytest tests/golden -q
"""

import json
import os
from pathlib import Path

import pytest

from tests.conftest import INVITE_CODE
from tests.golden.normalize import Normalizer

GOLDEN_PATH = Path(__file__).parent / "data" / "import_wiki.json"

BIBTEX = """@article{golden2026probe,
  title = {Deterministic Golden Chain Probe},
  author = {Probe, Golden and Chain, Import},
  journal = {Journal of Reproducible Plumbing},
  year = {2026},
  abstract = {Synthetic record for the golden transcript harness: manual import,
fake librarian compile, and index bookkeeping with zero network access.}
}"""

pytestmark = pytest.mark.asyncio


async def test_import_wiki_chain_matches_golden(client):
    n = Normalizer()
    transcript: dict[str, object] = {}

    async def step(name: str, resp, expect: int) -> dict:
        assert resp.status_code == expect, f"{name}: {resp.status_code} {resp.text[:300]}"
        body = resp.json()
        transcript[name] = n.normalize(body)
        return body

    await step(
        "register",
        await client.post(
            "/api/auth/register",
            json={
                "email": "golden@example.com",
                "password": "str0ng-password",
                "display_name": "Golden Probe",
                "username": "goldenprobe",
                "invite_code": INVITE_CODE,
            },
        ),
        201,
    )
    login = await client.post(
        "/api/auth/jwt/login",
        data={"username": "golden@example.com", "password": "str0ng-password"},
    )
    await step("login", login, 200)
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    library = await step(
        "create_library",
        await client.post(
            "/api/libraries",
            json={
                "name": "Golden Library",
                "statement": (
                    "Deterministic golden-transcript library covering reproducible plumbing."
                ),
            },
            headers=headers,
        ),
        201,
    )
    project = await step(
        "create_project",
        await client.post(
            "/api/projects",
            json={
                "name": "Golden Project",
                "statement": "golden transcript chain",
                "source_library_ids": [library["id"]],
            },
            headers=headers,
        ),
        201,
    )
    paper = await step(
        "add_paper_bibtex",
        await client.post(
            f"/api/projects/{project['id']}/papers", json={"bibtex": BIBTEX}, headers=headers
        ),
        201,
    )
    await step(
        "recompile_wiki",
        await client.post(f"/api/papers/{paper['id']}/recompile", headers=headers),
        200,
    )
    await step(
        "index_rebuild",
        await client.post(f"/api/papers/{paper['id']}/index/rebuild", headers=headers),
        200,
    )
    await step(
        "index_status",
        await client.get(f"/api/papers/{paper['id']}/index-status", headers=headers),
        200,
    )

    rendered = json.dumps(transcript, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if os.environ.get("POLARIS_GOLDEN") == "record":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(rendered, encoding="utf-8")
        pytest.skip(f"golden recorded: {GOLDEN_PATH}")
    assert GOLDEN_PATH.exists(), (
        "golden 文件缺失：本地用 POLARIS_GOLDEN=record 录制一次并连同代码提交"
    )
    expected = GOLDEN_PATH.read_text(encoding="utf-8")
    assert rendered == expected, (
        "golden transcript 变了。若这是有意的行为变更：本地 POLARIS_GOLDEN=record 重录，"
        "人工审查 diff 后随代码一起提交；纯移除 PR 不允许改 golden。"
    )
