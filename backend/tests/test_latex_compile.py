"""M5-B 编译服务测试（docs/api-m5-b.md §4、§9）：

- tectonic 不可用（本地 venv）：status=error + rule=other「编译器未安装」；
- 假 tectonic（monkeypatch _run_tectonic）：组装目录（files + 自动 references.bib +
  figures/ 实验图）、产物落盘（main.pdf / compile.log / diagnostics.json）、
  latest_compile 落库与 pdf/compile-latest 端点；
- 真实 tectonic 日志 fixture 的诊断解析（undefined citation/reference / ``!`` 错误 /
  Overfull / tectonic stderr file:line）。
"""

import json
import uuid
from pathlib import Path

import pytest_asyncio

from app.core.db import get_sessionmaker
from app.models.experiment import Experiment
from app.models.manuscript import Manuscript
from app.services import latex_compile
from app.services.latex_compile import TectonicRun, parse_diagnostics
from tests.test_manuscripts import (
    _create_manuscript,
    _seed_experiment,
    _seed_idea,
    _seed_paper,
    _setup_project,
)

REAL_LOG_FIXTURE = r"""
This is Tectonic ~0.15, based on XeTeX
(./main.tex
LaTeX2e <2023-11-01>
Package natbib Warning: Citation `ghost2020missing' on page 1 undefined on input line 42.

LaTeX Warning: Reference `fig:unknown' on page 2 undefined on input line 57.

Overfull \hbox (15.3pt too wide) in paragraph at lines 88--90
[]\TU/lmr/m/n/10 Some very long unbreakable line here

! Undefined control sequence.
l.104 \badmacro
               {oops}
?
! Emergency stop.
"""


@pytest_asyncio.fixture(autouse=True)
async def _clean_crdt():
    from app.services.crdt_rooms import reset_crdt_rooms

    yield
    await reset_crdt_rooms()


def test_parse_diagnostics_real_log_fixture():
    diags = parse_diagnostics(
        REAL_LOG_FIXTURE, stderr="error: main.tex:104: Undefined control sequence\n"
    )
    by_rule = {}
    for d in diags:
        by_rule.setdefault(d["rule"], []).append(d)

    citation = by_rule["undefined_citation"][0]
    assert citation["severity"] == "error"
    assert "ghost2020missing" in citation["message"]
    assert citation["line"] == 42

    reference = by_rule["undefined_reference"][0]
    assert "fig:unknown" in reference["message"] and reference["line"] == 57

    overfull = by_rule["overfull"][0]
    assert overfull["severity"] == "warning" and overfull["line"] == 88

    errors = by_rule["latex_error"]
    assert any(d["line"] == 104 for d in errors)  # ``! ...`` + l.104
    assert any(d["file"] == "main.tex" for d in errors)  # tectonic stderr file:line


async def _make_manuscript_with_facts(client):
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    exp_id = await _seed_experiment(project_id, idea_id)
    await _seed_paper(project_id, "Attention Is All You Need", 2017)
    resp = await _create_manuscript(
        client, headers, project_id, idea_id=idea_id, experiment_id=exp_id
    )
    assert resp.status_code == 201
    return project_id, headers, resp.json()["id"], exp_id


async def test_compile_without_tectonic(client, monkeypatch):
    _, headers, ms_id, _ = await _make_manuscript_with_facts(client)
    monkeypatch.setattr(latex_compile, "_find_tectonic", lambda: None)

    resp = await client.post(f"/api/manuscripts/{ms_id}/compile", headers=headers)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["version"] == 1
    assert result["status"] == "error"
    assert result["pdf_available"] is False
    assert len(result["diagnostics"]) == 1
    diag = result["diagnostics"][0]
    assert diag["rule"] == "other" and "编译器未安装" in diag["message"]

    # latest_compile 落库；PDF 不可用
    resp = await client.get(f"/api/manuscripts/{ms_id}/compile/latest", headers=headers)
    assert resp.json()["status"] == "error"
    resp = await client.get(f"/api/manuscripts/{ms_id}/pdf", headers=headers)
    assert resp.status_code == 404
    # 状态不因失败编译变化
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert detail["status"] == "draft"


async def test_compile_success_with_stubbed_tectonic(client, monkeypatch, tmp_path):
    project_id, headers, ms_id, exp_id = await _make_manuscript_with_facts(client)

    # 实验图落盘（figures 拷贝源）：png + 同名 pdf
    from app.services import experiments as experiments_service

    fig_dir = experiments_service.figures_dir(exp_id)
    fig_dir.mkdir(parents=True, exist_ok=True)
    (fig_dir / "primary_metric.png").write_bytes(b"\x89PNG fake")
    (fig_dir / "primary_metric.pdf").write_bytes(b"%PDF-1.4 fake")
    async with get_sessionmaker()() as session:
        experiment = await session.get(Experiment, uuid.UUID(exp_id))
        figures = [dict(f) for f in experiment.figures]
        figures[0]["path"] = str(fig_dir / "primary_metric.png")
        experiment.figures = figures
        await session.commit()

    captured: dict = {}

    def fake_run(binary: str, workdir: Path) -> TectonicRun:
        captured["files"] = sorted(
            str(p.relative_to(workdir)) for p in workdir.rglob("*") if p.is_file()
        )
        captured["references"] = (workdir / "references.bib").read_text(encoding="utf-8")
        captured["main"] = (workdir / "main.tex").read_text(encoding="utf-8")
        (workdir / "main.pdf").write_bytes(b"%PDF-1.5 stub pdf")
        (workdir / "main.log").write_text(
            "Overfull \\hbox (3.0pt too wide) in paragraph at lines 10--11\n",
            encoding="utf-8",
        )
        return TectonicRun(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(latex_compile, "_find_tectonic", lambda: "/usr/bin/tectonic")
    monkeypatch.setattr(latex_compile, "_run_tectonic", fake_run)

    resp = await client.post(f"/api/manuscripts/{ms_id}/compile", headers=headers)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["status"] == "ok" and result["pdf_available"] is True
    assert result["version"] == 1
    assert [d["rule"] for d in result["diagnostics"]] == ["overfull"]  # warning 不挡 ok

    # 组装目录：稿件文件 + 自动 references.bib + 实验图 pdf/png
    assert "main.tex" in captured["files"]
    assert "polaris_neurips2026.sty" in captured["files"]
    assert "references.bib" in captured["files"]
    assert {"figures/exp_fig_0.pdf", "figures/exp_fig_0.png"} <= set(captured["files"])
    assert (
        "@inproceedings{smith2017attention,"
        in captured["references"].replace("@article", "@inproceedings")
        or "smith2017attention" in captured["references"]
    )

    # 产物落盘 {data_dir}/manuscripts/<id>/v1/
    vdir = latex_compile.version_dir(ms_id, 1)
    assert (vdir / "main.pdf").read_bytes() == b"%PDF-1.5 stub pdf"
    assert (vdir / "compile.log").is_file()
    diagnostics = json.loads((vdir / "diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostics["status"] == "ok"

    # 端点：pdf inline / compile-latest / 状态 draft→compiled / 版本自增
    resp = await client.get(f"/api/manuscripts/{ms_id}/pdf", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == b"%PDF-1.5 stub pdf"
    detail = (await client.get(f"/api/manuscripts/{ms_id}", headers=headers)).json()
    assert detail["status"] == "compiled"
    assert detail["latest_compile"]["status"] == "ok"

    resp = await client.post(f"/api/manuscripts/{ms_id}/compile", headers=headers)
    assert resp.json()["version"] == 2


async def test_compile_error_and_timeout_paths(client, monkeypatch):
    _, headers, ms_id, _ = await _make_manuscript_with_facts(client)
    monkeypatch.setattr(latex_compile, "_find_tectonic", lambda: "/usr/bin/tectonic")

    # 编译错误：无 pdf，退出码非 0，stderr 转 latex_error 诊断
    def fail_run(binary: str, workdir: Path) -> TectonicRun:
        (workdir / "main.log").write_text(
            "! Undefined control sequence.\nl.7 \\nope\n", encoding="utf-8"
        )
        return TectonicRun(returncode=1, stdout="", stderr="error: main.tex:7: oops\n")

    monkeypatch.setattr(latex_compile, "_run_tectonic", fail_run)
    result = (await client.post(f"/api/manuscripts/{ms_id}/compile", headers=headers)).json()
    assert result["status"] == "error" and result["pdf_available"] is False
    rules = {d["rule"] for d in result["diagnostics"]}
    assert "latex_error" in rules
    lines = {d["line"] for d in result["diagnostics"] if d["rule"] == "latex_error"}
    assert 7 in lines

    # 超时：status=timeout
    def timeout_run(binary: str, workdir: Path) -> TectonicRun:
        return TectonicRun(returncode=-1, stdout="", stderr="", timed_out=True)

    monkeypatch.setattr(latex_compile, "_run_tectonic", timeout_run)
    result = (await client.post(f"/api/manuscripts/{ms_id}/compile", headers=headers)).json()
    assert result["status"] == "timeout"
    assert any("超时" in d["message"] for d in result["diagnostics"])


async def test_compile_fact_pack_s2_extras_in_bib(client, monkeypatch):
    """fact-pack 内 source=s2 的引用 → references.bib 生成 @misc 条目。"""
    _, headers, ms_id, _ = await _make_manuscript_with_facts(client)
    async with get_sessionmaker()() as session:
        ms = await session.get(Manuscript, uuid.UUID(ms_id))
        pack = dict(ms.fact_pack)
        pack["citations"] = list(pack["citations"]) + [
            {
                "bibkey": "doe2024related",
                "title": "A Related S2 Paper",
                "year": 2024,
                "authors": ["Jane Doe"],
                "venue": "arXiv",
                "url": "https://example.org/abs/1",
                "source": "s2",
            }
        ]
        ms.fact_pack = pack
        await session.commit()

    captured: dict = {}

    def fake_run(binary: str, workdir: Path) -> TectonicRun:
        captured["references"] = (workdir / "references.bib").read_text(encoding="utf-8")
        (workdir / "main.pdf").write_bytes(b"%PDF ok")
        return TectonicRun(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(latex_compile, "_find_tectonic", lambda: "/usr/bin/tectonic")
    monkeypatch.setattr(latex_compile, "_run_tectonic", fake_run)
    result = (await client.post(f"/api/manuscripts/{ms_id}/compile", headers=headers)).json()
    assert result["status"] == "ok"
    assert "smith2017attention" in captured["references"]
    assert "@misc{doe2024related," in captured["references"]
    assert "Jane Doe" in captured["references"]
