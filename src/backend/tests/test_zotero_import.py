"""Zotero 库导入（#638）：.bib 解析 + 三级去重 + 附件挂载 + 事件汇总，全离线。

直接驱动 worker 任务函数（fake redis + fake enrich），不依赖真实 ARQ/网络。
"""

import json
import uuid
import zipfile

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.events import paper_task_log_key
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper, new_paper
from app.models.user import User
from app.services.dedup import pool_dedup_key
from tests.conftest import make_project_with_library, register_and_login

# 8 条固定 fixture：新 DOI / DOI 重复 / arXiv 重复 / 标题重复 / 无标识符 /
# 缺 title / 带附件 / 异常字段（biblatex date、note 里的 arXiv、LaTeX 转义）
FIXTURE_BIB = r"""
@article{fresh2024,
  title = {A Fresh {DOI} Paper},
  author = {Lee, Ann and Bob Jones},
  year = {2024},
  journal = {Nature ML},
  doi = {10.5000/fresh},
  abstract = {Fresh work on agents.},
}

@article{dupdoi2020,
  title = {Different Title Entirely},
  author = {Bob, Sponge},
  year = {2020},
  doi = {10.4000/EXISTING},
}

@article{duparxiv2024,
  title = {Also A Different Name},
  author = {Carol, X},
  year = {2024},
  eprint = {2401.11111v2},
  archiveprefix = {arXiv},
}

@inproceedings{duptitle2022,
  title = {The  {Known} Paper: A Study!!},
  author = {Dave, Y},
  year = {2022},
  booktitle = {Proceedings of Nowhere},
}

@misc{standalone2019,
  title = {Standalone Note Without Identifiers},
  author = {Carol Zhang},
  year = {2019},
}

@article{broken2021,
  author = {Nobody},
  year = {2021},
}

@article{withpdf2023,
  title = {Paper With Attachment},
  author = {Dan, Q},
  year = {2023},
  doi = {10.5000/withpdf},
  file = {Full Text PDF:files/42/withpdf.pdf:application/pdf},
}

@software{weird2024,
  title = {An Online Tool with {\"U}nicode {Braces}},
  author = {M{\"u}ller, K.},
  date = {2024-05-01},
  url = {https://example.org/tool},
  note = {arXiv:2402.22222 [cs]},
}
"""


def _one_page_pdf() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Paper With Attachment full text.")
    return doc.tobytes()


async def _seed_library(client):
    """建库 + 三篇既有论文（DOI / arXiv / 标题 各一），返回 (library_id, user_id)。"""
    token = await register_and_login(client, email="zotero@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    _project_id, library_id = await make_project_with_library(client, headers, name="zotero-lib")
    async with get_sessionmaker()() as session:
        user_id = (
            await session.execute(select(User.id).where(User.email == "zotero@example.com"))
        ).scalar_one()
        seeds = [
            {"title": "Existing DOI Paper", "doi": "10.4000/existing", "arxiv_id": None},
            {"title": "Existing Arxiv Paper", "doi": None, "arxiv_id": "2401.11111"},
            # 标题重复靠规范化（小写去标点空白）对上，故意与 bib 里写法不同
            {"title": "The Known Paper -- A Study", "doi": None, "arxiv_id": None},
        ]
        for seed in seeds:
            paper = new_paper(
                source="manual",
                dedup_key=pool_dedup_key(
                    arxiv_id=seed["arxiv_id"], doi=seed["doi"], title=seed["title"]
                ),
                title=seed["title"],
                doi=seed["doi"],
                arxiv_id=seed["arxiv_id"],
            )
            session.add(paper)
            await session.flush()
            session.add(
                LibraryPaper(library_id=library_id, paper_id=paper.id, status="included")
            )
        await session.commit()
    return library_id, user_id, headers


async def _read_events(fake_redis, task_id):
    return [json.loads(raw) for raw in await fake_redis.lrange(paper_task_log_key(task_id), 0, -1)]


async def test_zotero_import_dedup_attachments_and_summary(
    client, fake_redis, tmp_path, monkeypatch
):
    """全链：8 条 fixture → 4 导入 / 3 重复（doi、arxiv、title）/ 1 无效，附件挂上。"""
    from app.services import paper_enrich
    from worker import tasks as worker_tasks

    library_id, user_id, _headers = await _seed_library(client)

    launched: list[uuid.UUID] = []

    async def _fake_launch(*, redis, paper_id, user_id, library_id=None, project_id=None):
        launched.append(paper_id)
        return None  # 不起真实补全：测试只关心导入与去重本身

    monkeypatch.setattr(paper_enrich, "launch_paper_enrichment", _fake_launch)

    bib_path = tmp_path / "library.bib"
    bib_path.write_text(FIXTURE_BIB, encoding="utf-8")
    zip_path = tmp_path / "files.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("export/files/42/withpdf.pdf", _one_page_pdf())

    totals = await worker_tasks.zotero_import(
        {"redis": fake_redis},
        task_id="ztest",
        bib_path=str(bib_path),
        zip_path=str(zip_path),
        library_id=str(library_id),
        user_id=str(user_id),
        project_id=None,
    )
    assert totals == {"created": 4, "existing": 3, "invalid": 1, "failed": 0}

    events = await _read_events(fake_redis, "ztest")
    items = [event["data"] for event in events if event["event"] == "batch_item"]
    assert [item["status"] for item in items] == [
        "created", "existing", "existing", "existing", "created", "invalid", "created", "created",
    ]
    # 三级去重理由逐级命中：DOI（大小写不敏感）→ arXiv（去版本号）→ 规范化标题
    assert [item["reason"] for item in items if item["status"] == "existing"] == [
        "doi", "arxiv", "title",
    ]
    assert items[5]["error"]  # 缺 title 的条目带原因
    assert items[6].get("attachment") is True
    assert events[-1] == {
        "event": "done",
        "data": {"total": 8, "created": 4, "existing": 3, "invalid": 1, "failed": 0},
    }
    assert len(launched) == 4  # 每条新导入都请求了后台补全

    async with get_sessionmaker()() as session:
        member_count = (
            await session.execute(
                select(LibraryPaper).where(LibraryPaper.library_id == library_id)
            )
        ).scalars().all()
        assert len(member_count) == 7  # 3 既有 + 4 新导入，重复条目没建新行

        attached = (
            await session.execute(select(Paper).where(Paper.doi == "10.5000/withpdf"))
        ).scalar_one()
        assert attached.pdf_path and attached.full_text_path  # 附件走上传入口，全文一并抽好

        weird = (
            await session.execute(
                select(Paper).where(Paper.title.ilike("%Online Tool%"))
            )
        ).scalar_one()
        # 异常字段兜底：biblatex date 取年份，note 里的 arXiv 线索提出来
        assert weird.year == 2024
        assert weird.arxiv_id == "2402.22222"
        assert weird.authors == [{"name": "Müller, K."}]


async def test_zotero_import_within_file_duplicate(client, fake_redis, tmp_path, monkeypatch):
    """同一个 .bib 里重复的条目也会被挡住（索引随导入即时更新）。"""
    from app.services import paper_enrich
    from worker import tasks as worker_tasks

    library_id, user_id, _headers = await _seed_library(client)

    async def _noop(**_kwargs):
        return None

    monkeypatch.setattr(paper_enrich, "launch_paper_enrichment", _noop)

    bib_path = tmp_path / "dups.bib"
    bib_path.write_text(
        "@article{a, title={Twice Imported Paper}, author={A}, year={2024}, doi={10.6000/twice}}\n"
        "@article{b, title={Twice Imported Paper vv}, author={A}, year={2024},"
        " doi={10.6000/TWICE}}\n",
        encoding="utf-8",
    )
    totals = await worker_tasks.zotero_import(
        {"redis": fake_redis},
        task_id="zdup",
        bib_path=str(bib_path),
        zip_path=None,
        library_id=str(library_id),
        user_id=str(user_id),
        project_id=None,
    )
    assert totals == {"created": 1, "existing": 1, "invalid": 0, "failed": 0}
    events = await _read_events(fake_redis, "zdup")
    items = [e["data"] for e in events if e["event"] == "batch_item"]
    assert items[1]["status"] == "existing" and items[1]["reason"] == "doi"


async def test_zotero_import_endpoint_enqueues_and_authz(client, fake_redis, queue_stub):
    """API：202 回 task_id/total 并入队；坏文件 422；非库管理者拒绝。"""
    token = await register_and_login(client, email="zotero-api@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/libraries",
        json={"name": "zotero-api-lib", "statement": "Zotero import test library"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    library_id = resp.json()["id"]

    files = {"bib": ("library.bib", FIXTURE_BIB.encode(), "text/x-bibtex")}
    resp = await client.post(
        f"/api/libraries/{library_id}/import/zotero", files=files, headers=headers
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["total"] == 8 and body["task_id"]
    assert queue_stub.jobs and queue_stub.jobs[0][0] == "zotero_import"
    kwargs = queue_stub.jobs[0][2]
    assert kwargs["library_id"] == library_id and kwargs["zip_path"] is None
    # 暂存文件已落盘，worker 可从共享数据卷读到
    from pathlib import Path

    assert Path(kwargs["bib_path"]).read_text(encoding="utf-8") == FIXTURE_BIB
    # 任务归属已登记：SSE 端点按它鉴权
    from app.services.paper_enrich import paper_task_owner_key

    owner = await fake_redis.get(paper_task_owner_key(body["task_id"]))
    assert owner is not None

    resp = await client.post(
        f"/api/libraries/{library_id}/import/zotero",
        files={"bib": ("broken.bib", b"not a bib file at all", "text/plain")},
        headers=headers,
    )
    assert resp.status_code == 422

    outsider = await register_and_login(client, email="zotero-outsider@example.com")
    resp = await client.post(
        f"/api/libraries/{library_id}/import/zotero",
        files=files,
        headers={"Authorization": f"Bearer {outsider}"},
    )
    assert resp.status_code in (403, 404)


async def test_zotero_import_endpoint_accepts_zip(client, fake_redis, queue_stub, tmp_path):
    """带附件 zip 的 multipart：zip 分块暂存到任务目录并随任务参数下发。"""
    import io

    token = await register_and_login(client, email="zotero-zip@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/libraries",
        json={"name": "zotero-zip-lib", "statement": "Zotero zip test library"},
        headers=headers,
    )
    library_id = resp.json()["id"]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("files/42/withpdf.pdf", b"%PDF-1.4 stub")
    resp = await client.post(
        f"/api/libraries/{library_id}/import/zotero",
        files={
            "bib": ("library.bib", FIXTURE_BIB.encode(), "text/x-bibtex"),
            "attachments": ("files.zip", buffer.getvalue(), "application/zip"),
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    kwargs = queue_stub.jobs[0][2]
    from pathlib import Path

    assert kwargs["zip_path"] and Path(kwargs["zip_path"]).exists()
    with zipfile.ZipFile(kwargs["zip_path"]) as zf:
        assert zf.namelist() == ["files/42/withpdf.pdf"]
