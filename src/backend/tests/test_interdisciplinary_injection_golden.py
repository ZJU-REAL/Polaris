"""#741 注入等价：指引搬离 v1 后，11+ 个注入点的产出与迁移前逐字节相同。

fixture 是在改动**之前**的代码上录的（v1 builtin 种子 + 原 apply_to_skill_snapshot），
固定 profile 字段与占位 UUID。这里用新存储的种子内容走同一条渲染管线，逐字节比对：
- 13 个 guidance 注入点的 skill_guidance() 渲染文本；
- navigator.free_plan 的 workflow 条目（steps/body 等，剔除随机 id 键）。
比对通过 = 种子内容原文迁移无损 + 渲染行为未变。
"""

import json
from pathlib import Path

from app.agents.voyage.skillset import skill_guidance
from app.services import interdisciplinary_workflows as iw

FIXTURE = Path(__file__).parent / "fixtures" / "interdisciplinary_injection_golden.json"


def _context_from_new_seed(fixed: dict) -> dict:
    """按 snapshot_for_project 的新逻辑组 context，profile 字段与占位 id 取自 fixture。"""
    seed = iw.GUIDANCE_SEED
    return {
        **fixed,
        # fixture 落盘用了 sort_keys，dict 键序被排序过；渲染按插入序拼接，
        # 这里按录制时的原始插入序还原
        "evidence_balance": {"Structural engineering": 0.5, "Computer vision": 0.25},
        # 迁移后两个键都指 guidance_documents 行；不参与渲染，随便给个占位
        "skill_id": "44444444-4444-4444-4444-444444444444",
        "skill_version_id": "55555555-5555-5555-5555-555555555555",
        "skill_slug": seed["slug"],
        "skill_name": seed["name"],
        "skill_body": seed["body"],
        "skill_manifest": {
            "targets": list(seed["targets"]),
            "steps": [dict(step) for step in seed["steps"]],
        },
    }


def _strip_ids(entries: list[dict]) -> list[dict]:
    return [
        {k: v for k, v in entry.items() if k not in ("skill_id", "skill_version_id")}
        for entry in entries
    ]


def test_injection_outputs_match_pre_migration_golden():
    golden = json.loads(FIXTURE.read_text(encoding="utf-8"))
    context = _context_from_new_seed(golden["context_fixed"])

    snapshot: dict = {}
    iw.apply_to_skill_snapshot(snapshot, context)

    rendered = {t: skill_guidance({"skills": snapshot}, t) for t in iw._GUIDANCE_TARGETS}
    assert set(rendered) == set(golden["rendered"])
    for target, text in golden["rendered"].items():
        assert rendered[target] == text, f"{target} 注入文本与迁移前不一致"

    assert _strip_ids(snapshot["navigator.free_plan"]) == golden["navigator_entries"]
    assert _strip_ids(snapshot[iw._GUIDANCE_TARGETS[0]]) == golden["guidance_entries"]
