"""论文分享 PPT：甲板校验/模板渲染/SKILL.md 导入/任务创建。"""

import io
import uuid

from pptx import Presentation as PptxFile

from app.services.presentation import DeckSpec, build_deck, validate_deck_spec
from tests.conftest import register_and_login

DECK = {
    "title": "测试分享",
    "slides": [
        {
            "kind": "cover",
            "title": "自我奖励语言模型",
            "subtitle": "arXiv 2024",
            "presenter": "汇报人：测试",
        },
        {"kind": "toc", "title": "目录", "items": ["背景", "方法", "结果"]},
        {
            "kind": "content",
            "title": "研究背景",
            "bullets": ["奖励模型受限于人类偏好数据", "- 质量随规模饱和"],
        },
        {
            "kind": "figure",
            "title": "训练闭环",
            "figure_index": 0,
            "caption": "模型给自己出题打分再训练，看左侧循环箭头",
        },
        {"kind": "closing", "title": "谢谢", "subtitle": "欢迎讨论"},
    ],
}

# 1x1 红点 PNG
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005050102cf9e3b2e0000000049454e44ae426082"
)


def test_validate_deck_spec_rules():
    spec = DeckSpec.model_validate(DECK)
    assert validate_deck_spec(spec, figure_indices={0}) == []

    bad = DeckSpec.model_validate(
        {
            "title": "t",
            "slides": [
                {
                    "kind": "cover",
                    "title": "一个特别长长长长长长长长长长长长长长的标题——还带破折号",
                },
                {"kind": "content", "title": "要点", "bullets": ["条目" + "很长" * 30]},
                {"kind": "figure", "title": "图", "figure_index": 9},
            ],
        }
    )
    errors = validate_deck_spec(bad, figure_indices={0})
    joined = "\n".join(errors)
    assert "破折号" in joined and "上限" in joined
    assert "figure_index 非法" in joined and "讲解" in joined


def test_build_deck_enforces_template_rules():
    spec = DeckSpec.model_validate(DECK)
    data = build_deck(spec, {0: _PNG})
    assert len(data) < 4_000_000  # 模板样例页与大媒体未混入产物
    prs = PptxFile(io.BytesIO(data))
    slides = list(prs.slides)
    assert len(slides) == 5
    # 标题 30pt、正文 18pt、二级要点 16pt
    sizes: set[float] = set()
    for slide in slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        if run.font.size:
                            sizes.add(run.font.size.pt)
    assert {30.0, 18.0, 16.0} <= sizes
    # figure 页图片已插入（占位符内嵌图：元素树里出现 blip 引用）
    assert any(sh._element.xpath('.//*[local-name()="blip"]') for sh in slides[3].shapes)  # noqa: SLF001


SKILL_MD = """---
name: 论文分享 PPT 制作
description: 分享用 PPT 的模板规范
---

# 论文分享 PPT 制作

标题短一点，正文一行一个短句。
"""


async def _setup(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "ppt-proj"}, headers=headers)
    return headers, resp.json()["id"]


async def test_import_skill_md(client):
    headers, _ = await _setup(client)
    resp = await client.post(
        "/api/skills/import-md",
        json={"content": SKILL_MD, "targets": ["present.slides", "present.outline"]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    skill = resp.json()
    assert skill["name"] == "论文分享 PPT 制作"
    assert skill["kind"] == "guidance"
    assert skill["current_version"]["manifest"]["targets"] == [
        "present.slides",
        "present.outline",
    ]
    assert "标题短一点" in skill["current_version"]["body"]
    # 再导入一次：slug 自动加后缀
    resp = await client.post(
        "/api/skills/import-md",
        json={"content": SKILL_MD, "targets": ["present.slides"]},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["slug"] != skill["slug"]


async def test_create_presentation_voyage(client, queue_stub):
    headers, project_id = await _setup(client)
    # 无论文 → 404；papers 属于项目校验
    resp = await client.post(
        f"/api/projects/{project_id}/presentations",
        json={"paper_ids": [str(uuid.uuid4())], "mode": "single"},
        headers=headers,
    )
    assert resp.status_code == 404

    from app.core.db import get_sessionmaker
    from app.models.paper import Paper

    async with get_sessionmaker()() as session:
        paper = Paper(
            project_id=uuid.UUID(project_id),
            title="Self-Rewarding Language Models",
            abstract="LLM as its own reward model.",
            status="compiled",
            source="manual",
        )
        session.add(paper)
        await session.commit()
        paper_id = str(paper.id)

    resp = await client.post(
        f"/api/projects/{project_id}/presentations",
        json={"paper_ids": [paper_id], "mode": "single", "notes": "面向组会"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    voyage = resp.json()
    assert voyage["kind"] == "presentation"
    assert ("run_voyage", (voyage["id"],), {}) in queue_stub.jobs
    # 文件未生成时下载 404
    resp = await client.get(f"/api/presentations/{voyage['id']}/file", headers=headers)
    assert resp.status_code == 404
    # survey 需要多篇由 mode 校验（single 传两篇 → 422）
    resp = await client.post(
        f"/api/projects/{project_id}/presentations",
        json={"paper_ids": [paper_id, paper_id], "mode": "single"},
        headers=headers,
    )
    assert resp.status_code == 422
