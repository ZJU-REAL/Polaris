"""一键全量导出（#690）：服务级目录树/zip 断言 + worker 事件 + API 入队/409/下载鉴权。"""

import json
import uuid
import zipfile
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.events import paper_task_log_key
from app.models.experiment import Experiment, ExperimentRun
from app.models.hypothesis import HypothesisNode
from app.models.idea import Idea
from app.models.manuscript import Manuscript, ManuscriptFile
from app.models.paper import PaperHighlight, PaperNote
from app.models.voyage import VoyageRun
from app.services.full_export import export_zip_path, run_full_export
from tests.conftest import add_paper, make_project_with_library, register_and_login

pytestmark = pytest.mark.asyncio


async def _user_id_of(client, headers) -> uuid.UUID:
    resp = await client.get("/api/users/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["id"])


async def _seed_full_plane(client, headers) -> tuple[uuid.UUID, str]:
    """造一个小而全的数据面：库 + 2 论文（1 带 PDF/解读）+ 笔记/划线 +
    1 个 discovery run（树 + 产物）+ 1 实验 + 1 稿件。返回 (user_id, 库名)。"""
    user_id = await _user_id_of(client, headers)
    lib_name = "全量导出测试库"
    project_id, _library_id = await make_project_with_library(
        client, headers, name=lib_name
    )
    pid = uuid.UUID(project_id)
    async with get_sessionmaker()() as session:
        p1 = await add_paper(
            session,
            project_id=project_id,
            title="Paper Alpha",
            year=2024,
            authors=[{"name": "Alice Smith"}],
            arxiv_id="2401.00001",
            status="included",
            wiki_content="# Alpha 解读\n\n正文。",
        )
        await add_paper(
            session,
            project_id=project_id,
            title="Paper Beta",
            year=2023,
            authors=[{"name": "Bob Jones"}],
            status="compiled",
        )
        # 附件 PDF：落在 data_dir，pdf_path 是权威指针（与线上口径一致）
        pdf_dir = Path(get_settings().data_dir) / "papers"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_file = pdf_dir / f"{p1.id}.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake")
        p1.pdf_path = str(pdf_file)
        session.add(PaperNote(paper_id=p1.id, author_id=user_id, content="很重要的一篇"))
        session.add(
            PaperHighlight(
                paper_id=p1.id,
                author_id=user_id,
                page=2,
                rects=[{"x0": 0, "y0": 0, "x1": 1, "y1": 0.1}],
                selected_text="a key sentence",
                note="划线批注",
            )
        )
        run = VoyageRun(
            kind="discovery",
            mode="loop",
            goal="探索注意力机制的新假设",
            status="done",
            created_by=user_id,
            checkpoint={
                "artifacts": {
                    "discovery-summary.json": json.dumps(
                        {
                            "direction": "attention",
                            "summary": "两条存活假设。",
                            "hypotheses": [
                                {"statement": "假设一", "score": 0.8, "status": "expanded"}
                            ],
                            "pruned_appendix": [],
                        },
                        ensure_ascii=False,
                    ),
                    "discovery-disclosure.json": json.dumps({"version": 1, "queries": []}),
                }
            },
        )
        session.add(run)
        await session.flush()
        root_node = HypothesisNode(
            run_id=run.id, kind="hypothesis", statement="根假设", status="expanded"
        )
        session.add(root_node)
        await session.flush()
        session.add(
            HypothesisNode(
                run_id=run.id,
                parent_id=root_node.id,
                kind="experiment",
                statement="验证实验",
                status="open",
                score=0.5,
            )
        )
        idea = Idea(project_id=pid, title="An Idea About Attention")
        session.add(idea)
        await session.flush()
        experiment = Experiment(
            project_id=pid,
            idea_id=idea.id,
            status="done",
            metrics={"loss": [{"step": 1, "value": 0.5}]},
            figures=[{"index": 0, "name": "fig0.png", "caption": "loss curve", "path": "x"}],
        )
        session.add(experiment)
        await session.flush()
        session.add(
            ExperimentRun(
                experiment_id=experiment.id,
                seq=1,
                command="python train.py",
                status="succeeded",
                exit_code=0,
            )
        )
        manuscript = Manuscript(project_id=pid, title="伟大的论文草稿")
        session.add(manuscript)
        await session.flush()
        session.add(
            ManuscriptFile(
                manuscript_id=manuscript.id,
                path="main.tex",
                content="\\documentclass{article}",
            )
        )
        await session.commit()
    return user_id, lib_name


async def test_full_export_builds_expected_tree(client, tmp_path):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    user_id, lib_name = await _seed_full_plane(client, headers)

    async with get_sessionmaker()() as session:
        zip_path, manifest = await run_full_export(session, user_id, tmp_path)

    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        # 库面：设置+成员清单 / 引用文件 / PDF（citekey 命名，与 papers.bib 对应）
        lib = json.loads(zf.read(f"libraries/{lib_name}/library.json"))
        assert lib["name"] == lib_name
        assert {m["title"] for m in lib["members"]} == {"Paper Alpha", "Paper Beta"}
        bib = zf.read(f"libraries/{lib_name}/papers.bib").decode()
        assert "smith2024paper" in bib  # citekey 由既有规则派生
        csl = json.loads(zf.read(f"libraries/{lib_name}/papers.csl.json"))
        assert len(csl) == 2
        assert f"libraries/{lib_name}/pdfs/smith2024paper.pdf" in names
        # 笔记面：按论文分组，frontmatter 带论文标识
        note_md = zf.read("notes/Paper Alpha.md").decode()
        assert 'title: "Paper Alpha"' in note_md
        assert 'arxiv_id: "2401.00001"' in note_md
        assert "很重要的一篇" in note_md
        assert "a key sentence" in note_md
        # wiki 面：Obsidian vault 结构
        assert f"wiki/{lib_name}/index.md" in names
        assert any(n.startswith(f"wiki/{lib_name}/papers/") for n in names)
        # discovery 面：树 + 方案 + 披露
        tree = json.loads(zf.read("discovery/探索注意力机制的新假设/tree.json"))
        assert len(tree["nodes"]) == 2
        assert tree["nodes"][0]["parent_id"] is None
        assert tree["nodes"][1]["kind"] == "experiment"
        proposal = zf.read("discovery/探索注意力机制的新假设/proposal.md").decode()
        assert "假设一" in proposal
        disclosure = json.loads(zf.read("discovery/探索注意力机制的新假设/disclosure.json"))
        assert disclosure["version"] == 1
        # 实验面：run.json 带逐次运行与产物清单
        run_json = json.loads(zf.read("experiments/An Idea About Attention/run.json"))
        assert run_json["runs"][0]["command"] == "python train.py"
        assert run_json["artifacts"]["figures"][0]["name"] == "fig0.png"
        assert "path" not in run_json["artifacts"]["figures"][0]
        # 稿件面：源文件
        assert (
            zf.read("manuscripts/伟大的论文草稿/source/main.tex").decode()
            == "\\documentclass{article}"
        )
        # 顶层：manifest 计数与 README
        assert "README.md" in names
        m = json.loads(zf.read("manifest.json"))
        assert m["counts"] == {
            "libraries": 1,
            "papers": 2,
            "pdfs": 1,
            "notes": 1,
            "highlights": 1,
            "wiki_pages": 2,
            "discovery_runs": 1,
            "experiments": 1,
            "manuscripts": 1,
        }
        assert m["warnings"] == []
    assert manifest["counts"]["papers"] == 2


async def test_full_export_empty_user_yields_skeleton(client, tmp_path):
    token = await register_and_login(client, email="empty@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    user_id = await _user_id_of(client, headers)

    async with get_sessionmaker()() as session:
        zip_path, manifest = await run_full_export(session, user_id, tmp_path)

    with zipfile.ZipFile(zip_path) as zf:
        assert set(zf.namelist()) == {"README.md", "manifest.json"}
        m = json.loads(zf.read("manifest.json"))
        assert all(v == 0 for v in m["counts"].values())
        assert m["warnings"] == []
    assert manifest["warnings"] == []


async def test_full_export_worker_emits_events_and_places_zip(client, fake_redis):
    """worker 入口：进度/完成事件按 paper-task 口径落回放日志，zip 落下载位。"""
    from worker import tasks as worker_tasks

    token = await register_and_login(client, email="worker@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    user_id = await _user_id_of(client, headers)
    task_id = uuid.uuid4().hex

    result = await worker_tasks.full_export(
        {"redis": fake_redis}, task_id=task_id, user_id=str(user_id)
    )
    assert "counts" in result

    events = [
        json.loads(raw) for raw in await fake_redis.lrange(paper_task_log_key(task_id), 0, -1)
    ]
    kinds = [e["event"] for e in events]
    assert kinds[-1] == "done"
    assert "export_progress" in kinds
    done = events[-1]["data"]
    assert done["download_path"] == f"/export/full/{task_id}/download"
    assert export_zip_path(task_id).is_file()


async def test_full_export_api_enqueue_conflict_and_download(client, fake_redis, queue_stub):
    token = await register_and_login(client, email="api@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    user_id = await _user_id_of(client, headers)

    # 未登录一律 401
    resp = await client.post("/api/export/full")
    assert resp.status_code == 401

    resp = await client.post("/api/export/full", headers=headers)
    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_id"]
    assert queue_stub.jobs[0][0] == "full_export"
    assert queue_stub.jobs[0][2] == {"task_id": task_id, "user_id": str(user_id)}

    # 限并发 1：进行中（锁未释放）再点一次 → 409
    resp = await client.post("/api/export/full", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "EXPORT_ALREADY_RUNNING"

    # zip 未就绪 → 404（属主也一样）
    resp = await client.get(f"/api/export/full/{task_id}/download", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "EXPORT_NOT_READY"

    # 模拟 worker 完成：zip 就位后属主可下载
    path = export_zip_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # 空 zip
    resp = await client.get(f"/api/export/full/{task_id}/download", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    # 非属主/非法 task_id 一律 404（不泄露存在性）
    other = await register_and_login(client, email="other@example.com")
    resp = await client.get(
        f"/api/export/full/{task_id}/download",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404
    resp = await client.get("/api/export/full/../../etc/passwd/download", headers=headers)
    assert resp.status_code == 404
    path.unlink()


async def test_full_export_lock_released_after_worker_failure(app, fake_redis):
    """worker 失败也要放锁（error 事件 + 可重试），不能把用户锁死 2 小时。"""
    from app.services.full_export import export_active_key
    from worker import tasks as worker_tasks

    task_id = uuid.uuid4().hex
    # user_id 不是合法 uuid → run_full_export 前置就炸，走 error 分支
    await fake_redis.set(export_active_key("not-a-uuid"), task_id)
    result = await worker_tasks.full_export(
        {"redis": fake_redis}, task_id=task_id, user_id="not-a-uuid"
    )
    assert "error" in result
    events = [
        json.loads(raw) for raw in await fake_redis.lrange(paper_task_log_key(task_id), 0, -1)
    ]
    assert events[-1]["event"] == "error"
    assert await fake_redis.get(export_active_key("not-a-uuid")) is None
