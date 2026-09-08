"""Golden transcript：注册 → 建库 → 建课题 → 两篇 bibtex 论文 → 库索引 →
建 discovery 任务 → 引擎跑完 → 任务详情 + 假设树 + 披露产物。

D3（#648）的回归闸门：四段假设管线（generate → ground → novelty → feasibility →
score）在 fake provider + sqlite 上确定性运行——树的形状、每个节点的 grounding /
novelty_report / feasibility / score、步骤观测与 token 记账，归一化后与
``tests/golden/data/discovery_run.json`` 逐字节比对。golden 变了 = 管线行为变了，
需要人工审查 diff 并说明理由。

引擎驱动（VoyageEngine.run）不是 HTTP 步骤，不进 transcript；transcript 只收
API 面上的可观测结果——golden 钉的是对外行为，不是内部实现。

更新方式（仅限本地，CI 只比对；何时允许重录、diff 怎么审见 docs/golden-policy.md）::

    make golden-record   # 或：POLARIS_GOLDEN=record python -m pytest tests/golden -q
"""

import json
import os
import uuid
from pathlib import Path

import pytest

from app.agents.voyage.engine import VoyageEngine
from app.core.llm.router import LLMRouter
from tests.conftest import INVITE_CODE, RecordingBus
from tests.golden.normalize import Normalizer

GOLDEN_PATH = Path(__file__).parent / "data" / "discovery_run.json"

# 摘要里埋方向词（structured / agent / planning）：bibtex 论文没有全文，库索引
# 给它们建「标题+作者+摘要」兜底块，接地检索靠这些词命中
BIBTEX_ONE = """@article{golden2026planner,
  title = {Structured Agent Planning Probe},
  author = {Probe, Golden},
  journal = {Journal of Reproducible Plumbing},
  year = {2026},
  abstract = {Synthetic record one: structured agent planning with verifiable
checkpoints for the deterministic golden harness.}
}"""

BIBTEX_TWO = """@article{golden2026grounding,
  title = {Grounded Hypothesis Evidence Probe},
  author = {Chain, Import},
  journal = {Journal of Reproducible Plumbing},
  year = {2026},
  abstract = {Synthetic record two: structured agent planning evidence with
grounded citations for the deterministic golden harness.}
}"""

DIRECTION = "structured agent planning"

pytestmark = pytest.mark.asyncio


async def test_discovery_chain_matches_golden(client, queue_stub):
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
                "email": "discovery-golden@example.com",
                "password": "str0ng-password",
                "display_name": "Discovery Golden",
                "username": "discoverygolden",
                "invite_code": INVITE_CODE,
            },
        ),
        201,
    )
    login = await client.post(
        "/api/auth/jwt/login",
        data={"username": "discovery-golden@example.com", "password": "str0ng-password"},
    )
    await step("login", login, 200)
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    library = await step(
        "create_library",
        await client.post(
            "/api/libraries",
            json={
                "name": "Discovery Golden Library",
                "statement": "Deterministic golden-transcript library for the hypothesis pipeline.",
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
                "name": "Discovery Golden Project",
                "statement": "golden discovery chain",
                "source_library_ids": [library["id"]],
            },
            headers=headers,
        ),
        201,
    )
    for name, bibtex in (("add_paper_one", BIBTEX_ONE), ("add_paper_two", BIBTEX_TWO)):
        await step(
            name,
            await client.post(
                f"/api/projects/{project['id']}/papers", json={"bibtex": bibtex}, headers=headers
            ),
            201,
        )
    await step(
        "library_index_rebuild",
        await client.post(f"/api/libraries/{library['id']}/index/rebuild", headers=headers),
        200,
    )
    run = await step(
        "create_discovery",
        await client.post(
            "/api/voyages",
            json={
                "kind": "discovery",
                "project_id": project["id"],
                "goal": DIRECTION,
                "params": {
                    "direction": DIRECTION,
                    "library_id": library["id"],
                    "max_expansions": 1,
                },
            },
            headers=headers,
        ),
        201,
    )

    # 引擎驱动（非 HTTP 步骤）：queue 是 stub，不会有 worker 来跑，这里同步跑完
    await VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter()).run(uuid.UUID(run["id"]))

    await step(
        "voyage_detail",
        await client.get(f"/api/voyages/{run['id']}", headers=headers),
        200,
    )
    await step(
        "hypothesis_tree",
        await client.get(f"/api/voyages/{run['id']}/hypothesis-tree", headers=headers),
        200,
    )
    # 披露产物（#655 D6）：检索/阅读/剪枝/记账全录，经通用产物只读端点取回。
    # 归一化前先做不变量自检——golden 链是「行为没变」的闸门，披露自洽
    # （实引 ⊆ 检索集、被剪皆有因、零警告）是行为的一部分，必须常绿。
    disclosure = (
        await step(
            "discovery_disclosure",
            await client.get(
                f"/api/voyages/{run['id']}/artifacts/discovery-disclosure.json",
                headers=headers,
            ),
            200,
        )
    )["content"]
    assert disclosure["invariants"] == {
        "cited_subset_of_retrieved": True,
        "pruned_have_reasons": True,
    }
    assert disclosure["warnings"] == []
    assert disclosure["queries"], "披露里必须有检索留痕"
    # 燃料节（#670）：golden 的两篇 bibtex 论文没有全文 → 没有抽取产物/概念 →
    # 三路燃料合法为空，generate 行为与纯检索路等价（有燃料的差异走常规测试）。
    # 断言「节存在且为空」钉住这条降级路径本身。
    assert disclosure["fuels"] == {"methods": [], "concept_pairs": [], "gaps": []}

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
        "golden transcript 变了。若这是有意的行为变更：本地 make golden-record 重录，"
        "人工审查 diff 后随代码一起提交（规则见 docs/golden-policy.md）。"
    )
