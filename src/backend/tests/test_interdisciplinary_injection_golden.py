"""跨学科指引的注入等价：13 个注入点的**正文**始终是种子原文。

原本（#741）钉的是「与迁移前逐字节相同」。技能功能移除后外层措辞必须改——
旧文案写着「以下技能由用户启用」，而技能已经没有了，照抄就是一句假话。所以：

- **正文**仍逐字节比对（这才是迁移完整性的主张：种子内容没有在搬家途中被改动）；
- **外层**（标题行与那句引导语）随本次改名重录，并单独断言它不再提「技能」——
  否则下次有人把措辞改回去也没人发现。
"""

import json
from pathlib import Path

from app.agents.voyage.guidance import workflow_guidance
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
        "slug": seed["slug"],
        "name": seed["name"],
        "body": seed["body"],
        "manifest": {
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
    iw.apply_to_guidance(snapshot, context)

    rendered = {t: workflow_guidance({"guidance": snapshot}, t) for t in iw._GUIDANCE_TARGETS}
    assert set(rendered) == set(golden["rendered"])
    for target, text in golden["rendered"].items():
        assert rendered[target] == text, f"{target} 注入文本与录制时不一致"

    # 正文逐字节：种子内容不能在搬家或改名途中被动过
    body = context["body"].strip()
    for target, text in rendered.items():
        assert body in text, f"{target} 的注入文本里没有种子正文"

    # 外层不能再提「技能」——那个功能已经没有了，写着就是假话
    for text in rendered.values():
        assert "技能" not in text

    assert _strip_ids(snapshot["navigator.free_plan"]) == golden["navigator_entries"]
    assert _strip_ids(snapshot[iw._GUIDANCE_TARGETS[0]]) == golden["guidance_entries"]
