"""LaTeX 编译服务（docs/api-m5-b.md §4，不 import fastapi）。

- 临时目录组装：稿件全部文件 + 自动生成 references.bib（fact-pack 固定 bibkey）
  + figures/（实验图 PDF/PNG 从 data_dir/experiments 拷贝）；
- subprocess tectonic（asyncio.to_thread，硬超时 120s，--keep-logs）；
- 诊断解析：undefined citation/reference、``! ...`` 错误（带 file:line）、
  Overfull \\hbox 警告；
- 产物落 ``{data_dir}/manuscripts/<id>/v<n>/``（main.pdf + compile.log +
  diagnostics.json），CompileResult 写回 Manuscript.latest_compile；
- tectonic 二进制不存在（本地 venv 测试环境）：status=error + 单条
  rule=other message="编译器未安装"。
"""

import asyncio
import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.experiment import Experiment
from app.models.manuscript import Manuscript, ManuscriptFile
from app.models.paper import Paper
from app.services import crdt_rooms, manuscript_versions
from app.services.citations import build_bibtex_for

COMPILE_TIMEOUT_SECONDS = 120.0
TECTONIC_BIN = "tectonic"
MAIN_TEX = "main.tex"
MISSING_COMPILER_MESSAGE = "编译器未安装（tectonic 不在 PATH，请使用 docker 镜像编译）"

_CITATION_RE = re.compile(
    r"(?:LaTeX|Package natbib) Warning: Citation [`']([^`']+)'.*?"
    r"(?:on input line (\d+))?\.?$"
)
_REFERENCE_RE = re.compile(r"LaTeX Warning: Reference [`']([^`']+)'.*?(?:on input line (\d+))?\.?$")
_OVERFULL_RE = re.compile(r"^Overfull \\[hv]box \([^)]*\) (?:in paragraph )?at lines? (\d+)")
_BANG_ERROR_RE = re.compile(r"^! ?(.+)$")
_ERROR_LINE_RE = re.compile(r"^l\.(\d+)")
# tectonic 把 TeX 错误转写为 "error: <file>:<line>: <msg>" 输出到 stderr
_TECTONIC_ERROR_RE = re.compile(r"^error: (?:([^:\s][^:]*):(\d+): )?(.+)$")


@dataclass(slots=True)
class TectonicRun:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


# ---- 产物路径 ----


def manuscript_data_dir(manuscript_id: uuid.UUID | str) -> Path:
    return Path(get_settings().data_dir) / "manuscripts" / str(manuscript_id)


def version_dir(manuscript_id: uuid.UUID | str, version: int) -> Path:
    return manuscript_data_dir(manuscript_id) / f"v{version}"


def pdf_path(manuscript_id: uuid.UUID | str, version: int) -> Path:
    return version_dir(manuscript_id, version) / "main.pdf"


# ---- 诊断解析 ----


def _diag(
    severity: str, rule: str, message: str, *, file: str | None = None, line: int | None = None
) -> dict[str, Any]:
    return {"severity": severity, "file": file, "line": line, "rule": rule, "message": message}


def parse_diagnostics(log_text: str, stderr: str = "") -> list[dict[str, Any]]:
    """解析 LaTeX log（--keep-logs）与 tectonic stderr → 契约诊断列表。"""
    diagnostics: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int | None]] = set()

    def add(diag: dict[str, Any]) -> None:
        key = (diag["rule"], diag["message"], diag["line"])
        if key not in seen:
            seen.add(key)
            diagnostics.append(diag)

    lines = log_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if m := _CITATION_RE.search(line):
            add(
                _diag(
                    "error",
                    "undefined_citation",
                    f"Citation `{m.group(1)}' undefined",
                    line=int(m.group(2)) if m.group(2) else None,
                )
            )
        elif m := _REFERENCE_RE.search(line):
            add(
                _diag(
                    "error",
                    "undefined_reference",
                    f"Reference `{m.group(1)}' undefined",
                    line=int(m.group(2)) if m.group(2) else None,
                )
            )
        elif m := _OVERFULL_RE.match(line):
            add(_diag("warning", "overfull", line.strip(), line=int(m.group(1))))
        elif m := _BANG_ERROR_RE.match(line):
            # ``! <message>``；随后数行内的 ``l.<n>`` 给出行号
            message = m.group(1).strip()
            err_line: int | None = None
            for j in range(i + 1, min(i + 8, len(lines))):
                if lm := _ERROR_LINE_RE.match(lines[j]):
                    err_line = int(lm.group(1))
                    break
            add(_diag("error", "latex_error", message, line=err_line))
        i += 1

    for line in stderr.splitlines():
        if m := _TECTONIC_ERROR_RE.match(line.strip()):
            file, line_no, message = m.group(1), m.group(2), m.group(3).strip()
            add(
                _diag(
                    "error",
                    "latex_error",
                    message,
                    file=file,
                    line=int(line_no) if line_no else None,
                )
            )
    return diagnostics


# ---- references.bib / figures 组装 ----


def _extra_bibtex(entries: list[dict[str, Any]]) -> str:
    """fact-pack 中非库内引用（source=s2）→ @misc 条目。"""
    blocks: list[str] = []
    for entry in entries:
        fields: list[tuple[str, str]] = [("title", str(entry.get("title") or ""))]
        authors = entry.get("authors") or []
        if authors:
            fields.append(("author", " and ".join(str(a) for a in authors)))
        if entry.get("year"):
            fields.append(("year", str(entry["year"])))
        if entry.get("venue"):
            fields.append(("howpublished", str(entry["venue"])))
        if entry.get("url"):
            fields.append(("url", str(entry["url"])))
        lines = [f"@misc{{{entry['bibkey']},"]
        lines += [f"  {name} = {{{value}}}," for name, value in fields]
        lines.append("}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


async def build_references_bib(session: AsyncSession, manuscript: Manuscript) -> str:
    """references.bib：库内论文按 fact-pack 固定 bibkey + S2 追加条目（@misc）。"""
    citations = (manuscript.fact_pack or {}).get("citations") or []
    library = [c for c in citations if c.get("source") != "s2" and c.get("paper_id")]
    extras = [c for c in citations if c.get("source") == "s2"]

    paper_ids = [uuid.UUID(str(c["paper_id"])) for c in library]
    keys_by_id = {uuid.UUID(str(c["paper_id"])): str(c["bibkey"]) for c in library}
    papers: list[Paper] = []
    if paper_ids:
        stmt = select(Paper).where(Paper.id.in_(paper_ids))
        found = {p.id: p for p in (await session.execute(stmt)).scalars().all()}
        papers = [found[pid] for pid in paper_ids if pid in found]
    bib = build_bibtex_for(papers, {p.id: keys_by_id[p.id] for p in papers})
    extra = _extra_bibtex(extras)
    if bib and extra:
        return bib + "\n" + extra
    return bib or extra


async def _copy_figures(
    session: AsyncSession, manuscript: Manuscript, workdir: Path
) -> list[dict[str, Any]]:
    """实验图拷入 figures/<fig_id>.pdf(+.png)；缺文件记 warning 诊断。"""
    fact_figures = (manuscript.fact_pack or {}).get("figures") or []
    if not fact_figures or manuscript.experiment_id is None:
        return []
    experiment = await session.get(Experiment, manuscript.experiment_id)
    if experiment is None:
        return []
    by_index = {
        int(f["index"]): f
        for f in experiment.figures or []
        if isinstance(f, dict) and f.get("index") is not None
    }
    diagnostics: list[dict[str, Any]] = []
    fig_dir = workdir / "figures"
    for fig in fact_figures:
        fig_id = str(fig.get("fig_id") or "")
        m = re.fullmatch(r"exp_fig_(\d+)", fig_id)
        entry = by_index.get(int(m.group(1))) if m else None
        png = Path(str(entry.get("path"))) if entry and entry.get("path") else None
        copied = False
        if png is not None:
            pdf = png.with_suffix(".pdf")
            fig_dir.mkdir(parents=True, exist_ok=True)
            if pdf.is_file():
                shutil.copyfile(pdf, fig_dir / f"{fig_id}.pdf")
                copied = True
            if png.is_file():
                shutil.copyfile(png, fig_dir / f"{fig_id}.png")
                copied = True
        if not copied:
            diagnostics.append(
                _diag("warning", "other", f"实验图表文件缺失，未拷入编译目录：{fig_id}")
            )
    return diagnostics


def _safe_relpath(path: str) -> Path | None:
    parts = path.replace("\\", "/").split("/")
    if not path or path.startswith("/") or ".." in parts:
        return None
    return Path(*parts)


async def assemble_workdir(
    session: AsyncSession,
    manuscript: Manuscript,
    workdir: Path,
    *,
    snapshot_label: str | None = None,
) -> list[dict[str, Any]]:
    """稿件文件（活跃 CRDT 房间以房间内容为准）+ references.bib + figures/。

    可写文件同时存一份版本快照（origin=compile，内容与本次编译一致；
    与上份相同则跳过），调用方 commit。
    """
    rooms = crdt_rooms.get_crdt_rooms()
    stmt = select(ManuscriptFile).where(ManuscriptFile.manuscript_id == manuscript.id)
    files = (await session.execute(stmt)).scalars().all()
    for file in files:
        rel = _safe_relpath(file.path)
        if rel is None:
            continue
        await rooms.flush(file.id)  # 有活跃房间：取消防抖并立即快照
        content = rooms.room_content(file.id)
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        text = content if content is not None else file.content
        target.write_text(text, encoding="utf-8")
        if not file.readonly:
            await manuscript_versions.snapshot_file(
                session, file, origin="compile", label=snapshot_label, content=text
            )
    (workdir / "references.bib").write_text(
        await build_references_bib(session, manuscript), encoding="utf-8"
    )
    return await _copy_figures(session, manuscript, workdir)


# ---- tectonic 执行 ----


def _find_tectonic() -> str | None:
    return shutil.which(TECTONIC_BIN)


def _run_tectonic(binary: str, workdir: Path) -> TectonicRun:
    """同步跑 tectonic（调用方用 asyncio.to_thread）；120s 硬超时。

    tectonic 内部自动多趟重跑（bibtex/交叉引用），此处限制 ≤3 趟。
    """
    try:
        proc = subprocess.run(  # noqa: S603 — 固定二进制 + 固定参数
            [binary, "--keep-logs", "--reruns", "3", MAIN_TEX],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT_SECONDS,
        )
        return TectonicRun(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return TectonicRun(-1, stdout, stderr, timed_out=True)


# ---- 入口 ----


async def compile_manuscript(session: AsyncSession, manuscript: Manuscript) -> dict[str, Any]:
    """同步编译（API/写作 voyage 共用）：产物落盘 + latest_compile 落库。"""
    version = int((manuscript.latest_compile or {}).get("version") or 0) + 1
    started = time.monotonic()
    diagnostics: list[dict[str, Any]] = []
    status = "error"
    pdf_available = False

    binary = _find_tectonic()
    if binary is None:
        diagnostics.append(_diag("error", "other", MISSING_COMPILER_MESSAGE))
    else:
        out_dir = version_dir(manuscript.id, version)
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="polaris-tex-") as tmp:
            workdir = Path(tmp)
            diagnostics.extend(
                await assemble_workdir(
                    session, manuscript, workdir, snapshot_label=f"编译 v{version}"
                )
            )
            run = await asyncio.to_thread(_run_tectonic, binary, workdir)

            log_file = workdir / "main.log"
            log_text = (
                log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
            )
            diagnostics.extend(parse_diagnostics(log_text, run.stderr))

            built_pdf = workdir / "main.pdf"
            if built_pdf.is_file():
                shutil.copyfile(built_pdf, out_dir / "main.pdf")
                pdf_available = True
            (out_dir / "compile.log").write_text(
                log_text or (run.stdout + "\n" + run.stderr), encoding="utf-8"
            )

            if run.timed_out:
                status = "timeout"
                diagnostics.append(
                    _diag(
                        "error",
                        "other",
                        f"编译超时（>{int(COMPILE_TIMEOUT_SECONDS)}s），已终止",
                    )
                )
            elif run.returncode == 0 and pdf_available:
                has_error = any(d["severity"] == "error" for d in diagnostics)
                status = "error" if has_error else "ok"
            else:
                status = "error"
                if not any(d["severity"] == "error" for d in diagnostics):
                    diagnostics.append(
                        _diag(
                            "error",
                            "other",
                            f"tectonic 退出码 {run.returncode}："
                            f"{(run.stderr or run.stdout)[-500:]}",
                        )
                    )

    result: dict[str, Any] = {
        "version": version,
        "status": status,
        "pdf_available": pdf_available,
        "diagnostics": diagnostics,
        "compiled_at": datetime.now(UTC).isoformat(),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    out_dir = version_dir(manuscript.id, version)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "diagnostics.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    manuscript.latest_compile = result
    # 编译成功：draft/writing → compiled（under_review/approved/submitted 不回退）
    if status == "ok" and manuscript.status in ("draft", "writing"):
        manuscript.status = "compiled"
    await session.commit()
    await session.refresh(manuscript)
    return result


def latest_ok_pdf(manuscript: Manuscript) -> Path | None:
    """最新一次成功编译的 PDF 路径（GET /manuscripts/{id}/pdf）。"""
    latest = manuscript.latest_compile or {}
    if latest.get("status") != "ok" or not latest.get("pdf_available"):
        return None
    path = pdf_path(manuscript.id, int(latest.get("version") or 0))
    return path if path.is_file() else None
