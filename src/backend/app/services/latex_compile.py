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
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
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
from app.services import manuscripts as manuscripts_service
from app.services.citations import build_bibtex_for

COMPILE_TIMEOUT_SECONDS = 120.0
TECTONIC_BIN = "tectonic"
LATEXMK_BIN = "latexmk"
MAIN_TEX = "main.tex"
# 可选编译器（Overleaf 式）：tectonic 自带 XeTeX（镜像必装）；其余走 latexmk（需 TeX Live）
ENGINES: tuple[str, ...] = ("tectonic", "pdflatex", "xelatex", "lualatex")
# latexmk 引擎开关（-pdf=pdflatex / -pdfxe=xelatex / -pdflua=lualatex）
_LATEXMK_ENGINE_FLAG = {"pdflatex": "-pdf", "xelatex": "-pdfxe", "lualatex": "-pdflua"}
MISSING_COMPILER_MESSAGE = "编译器未安装（tectonic 不在 PATH，请使用 docker 镜像编译）"


def normalize_engine(engine: str | None) -> str:
    """未知/空 → tectonic（默认，且镜像必装、最稳）。"""
    return engine if engine in ENGINES else "tectonic"


def _resolve_engine(requested: str) -> tuple[str, str] | None:
    """挑一个真正可用的编译器：请求 latexmk 系（pdf/xe/lua）但 latexmk 缺失时回退 tectonic。
    返回 (实际引擎, 二进制路径)；都不可用返回 None。"""
    requested = normalize_engine(requested)
    if requested != "tectonic":
        latexmk = shutil.which(LATEXMK_BIN)
        if latexmk:
            return requested, latexmk
    tectonic = shutil.which(TECTONIC_BIN)
    if tectonic:
        return "tectonic", tectonic
    return None


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


async def sync_references_bib(session: AsyncSession, manuscript: Manuscript) -> None:
    """把稿件的 references.bib 文件内容对齐当前 fact_pack（AI 起草改动引用后调用，
    保证 AI 新增的 \\cite 能在 bib 里找到）。文件不存在则创建。调用方负责 commit 已在内。"""
    content = await build_references_bib(session, manuscript)
    stmt = select(ManuscriptFile).where(
        ManuscriptFile.manuscript_id == manuscript.id,
        ManuscriptFile.path == "references.bib",
    )
    f = (await session.execute(stmt)).scalar_one_or_none()
    if f is None:
        session.add(
            ManuscriptFile(
                manuscript_id=manuscript.id, path="references.bib", content=content, readonly=False
            )
        )
    elif f.content != content:
        f.content = content
    await session.commit()
    # 有活跃协同房间时同步房间内容（避免房间旧内容回写覆盖）
    room = crdt_rooms.get_crdt_rooms()
    if f is not None:
        await room.set_content(f.id, content)


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
    take_snapshots: bool = True,
) -> list[dict[str, Any]]:
    """稿件文件（活跃 CRDT 房间以房间内容为准）+ references.bib + figures/。

    take_snapshots=True 时可写文件同时存一份版本快照（origin=compile），调用方 commit；
    导出等只读用途传 False，避免产生多余版本。
    """
    rooms = crdt_rooms.get_crdt_rooms()
    stmt = select(ManuscriptFile).where(ManuscriptFile.manuscript_id == manuscript.id)
    files = (await session.execute(stmt)).scalars().all()
    has_bib_file = False
    for file in files:
        rel = _safe_relpath(file.path)
        if rel is None:
            continue
        target = workdir / rel
        if file.is_folder:
            target.mkdir(parents=True, exist_ok=True)
            continue
        if file.is_binary:
            # 二进制资源（图片/字体/PDF 等）：字节从磁盘拷入编译目录
            data = manuscripts_service.read_binary_asset(manuscript.id, file.path)
            if data is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            continue
        await rooms.flush(file.id)  # 有活跃房间：取消防抖并立即快照
        content = rooms.room_content(file.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = content if content is not None else file.content
        target.write_text(text, encoding="utf-8")
        if file.path == "references.bib":
            has_bib_file = True
        if take_snapshots and not file.readonly:
            await manuscript_versions.snapshot_file(
                session, file, origin="compile", label=snapshot_label, content=text
            )
    # 稿件自带 references.bib（建稿时生成、用户可编辑）就用它；否则兜底自动生成
    if not has_bib_file:
        (workdir / "references.bib").write_text(
            await build_references_bib(session, manuscript), encoding="utf-8"
        )
    return await _copy_figures(session, manuscript, workdir)


# ---- tectonic 执行 ----


def _find_tectonic() -> str | None:
    return shutil.which(TECTONIC_BIN)


def _find_output(workdir: Path, stem: str, suffix: str) -> Path | None:
    """定位编译产物（jobname=<主文件名去扩展>）：先看目录根，再全目录兜底搜。"""
    direct = workdir / f"{stem}{suffix}"
    if direct.is_file():
        return direct
    matches = [p for p in workdir.rglob(f"{stem}{suffix}") if p.is_file()]
    return matches[0] if matches else None


def _engine_argv(engine: str, binary: str, main_name: str) -> list[str]:
    """按引擎拼编译命令行（tectonic 直接跑；pdf/xe/lua 走 latexmk 自动多趟 + bibtex）。"""
    if engine == "tectonic":
        return [binary, "--keep-logs", "--reruns", "3", main_name]
    return [
        binary,
        _LATEXMK_ENGINE_FLAG[engine],
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        main_name,
    ]


def _run_engine(engine: str, binary: str, workdir: Path, main_name: str) -> TectonicRun:
    """同步跑选定编译器（调用方用 asyncio.to_thread）；120s 硬超时。

    tectonic 内部自动多趟重跑；latexmk 亦自动重跑 + 跑 bibtex/biber。
    TEXINPUTS=.//: 让 kpathsea 递归搜子目录，主文件/样式在子目录时也能找到（如
    ICLR 模板把各年份样式放在 iclrYYYY/ 子目录）。
    """
    env = {**os.environ, "TEXINPUTS": ".//:" + os.environ.get("TEXINPUTS", "")}
    try:
        proc = subprocess.run(  # noqa: S603 — 固定二进制 + 固定参数
            _engine_argv(engine, binary, main_name),
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT_SECONDS,
            env=env,
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

    requested_engine = normalize_engine(manuscript.engine)
    resolved = _resolve_engine(requested_engine)
    if resolved is None:
        diagnostics.append(_diag("error", "other", MISSING_COMPILER_MESSAGE))
    else:
        engine, binary = resolved
        out_dir = version_dir(manuscript.id, version)
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="polaris-tex-") as tmp:
            workdir = Path(tmp)
            diagnostics.extend(
                await assemble_workdir(
                    session, manuscript, workdir, snapshot_label=f"编译 v{version}"
                )
            )
            # 入口主文件：优先用稿件设置；不存在则回退到目录里检测到的主 .tex
            main_name = manuscript.main_tex or MAIN_TEX
            if not (workdir / main_name).is_file():
                main_name = _find_workdir_main(workdir) or main_name
            stem = Path(main_name).stem

            # 请求的编译器不可用（如未装 TeX Live）→ 已回退 tectonic，提示一下
            if engine != requested_engine:
                diagnostics.append(
                    _diag(
                        "warning",
                        "other",
                        f"编译器 {requested_engine} 不可用，已改用 {engine} 编译",
                    )
                )

            def _attempt(
                eng: str, bin_: str
            ) -> tuple[TectonicRun, str, list[dict[str, Any]], Path | None]:
                run = _run_engine(eng, bin_, workdir, main_name)
                lf = _find_output(workdir, stem, ".log")
                lt = lf.read_text(encoding="utf-8", errors="replace") if lf else ""
                return (
                    run,
                    lt,
                    parse_diagnostics(lt, run.stderr),
                    _find_output(workdir, stem, ".pdf"),
                )

            run, log_text, eng_diags, built_pdf = await asyncio.to_thread(_attempt, engine, binary)

            # 所选引擎有问题（缺宏包/字体、未定义引用未被 bibtex 解析、或没编出 PDF）→ 用
            # tectonic 再试一次：它自带完整宏包库（按需补装“支持安装 package”）且 bibtex
            # 解析更稳（子目录 .bib 也能找到）。tectonic 结果更好（错误更少 / 补出了 PDF）才采用。
            chosen_errors = sum(1 for d in eng_diags if d["severity"] == "error")
            if not run.timed_out and engine != "tectonic" and (chosen_errors or built_pdf is None):
                tec = shutil.which(TECTONIC_BIN)
                if tec:
                    run2, log2, diags2, pdf2 = await asyncio.to_thread(_attempt, "tectonic", tec)
                    tec_errors = sum(1 for d in diags2 if d["severity"] == "error")
                    if (pdf2 is not None and built_pdf is None) or tec_errors < chosen_errors:
                        run, log_text, built_pdf = run2, log2, pdf2
                        eng_diags = [
                            _diag(
                                "warning",
                                "other",
                                f"{engine} 编译有问题（缺宏包或引用未解析），"
                                "已改用 tectonic（自带宏包库、按需补装）重新编译",
                            )
                        ] + diags2
                        engine = "tectonic"

            diagnostics.extend(eng_diags)
            if built_pdf is not None:
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
                            f"{engine} 退出码 {run.returncode}："
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


# ---- arXiv 清洁包导出（源文件 + .bbl，剔除 aux/log/pdf 等编译副产物） ----

# 打包时剔除的编译副产物后缀（.bbl 例外，arXiv 需要它且不一定跑 bibtex）
# 确定无疑的编译中间产物，可以按后缀一刀切。
_INTERMEDIATE_SUFFIXES = frozenset(
    {
        ".aux",
        ".log",
        ".out",
        ".blg",
        ".fls",
        ".fdb_latexmk",
        ".synctex",
        ".toc",
        ".lof",
        ".lot",
        ".bcf",
        ".nav",
        ".snm",
        ".vrb",
    }
)


def _is_build_artifact(rel: str, main_stem: str | None) -> bool:
    """是否是编译中间产物（打包时应剔除）。

    .pdf / .gz / .xml 不能按后缀一刀切：.pdf 是 LaTeX 最常见的插图格式，
    .gz 与 .xml 也可能是稿件自带的数据文件。只有编译产物本身该被剔除，
    所以这三类按**文件名**精确匹配（main.pdf 只在包根目录才算产物；
    figures/ 里的同名文件是插图）。
    """
    name = rel.rsplit("/", 1)[-1].lower()
    if Path(name).suffix in _INTERMEDIATE_SUFFIXES:
        return True
    if name.endswith(".synctex.gz") or name.endswith(".run.xml"):
        return True
    return bool(main_stem) and "/" not in rel and name == f"{str(main_stem).lower()}.pdf"


def _digest_of(workdir: Path, rels: list[str]) -> str:
    """源码包内容指纹：路径 + 内容各自入哈希，路径排序保证稳定。

    桌面端本地编译拿它当缓存 key——源变了 digest 就变，旧产物自然失效，
    不需要任何额外的失效逻辑。
    """
    outer = hashlib.sha256()
    for rel in rels:
        outer.update(rel.encode("utf-8"))
        outer.update(b"\0")
        outer.update(hashlib.sha256((workdir / rel).read_bytes()).digest())
        outer.update(b"\0")
    return outer.hexdigest()


async def _assemble_source_files(
    session: AsyncSession, manuscript: Manuscript, workdir: Path
) -> list[str]:
    """只读组装稿件源码到 workdir，返回应当入包的相对路径（已排序、已剔除中间产物）。"""
    await assemble_workdir(session, manuscript, workdir, take_snapshots=False)
    main_name = _find_workdir_main(workdir)
    main_stem = Path(main_name).stem if main_name else None
    rels: list[str] = []
    for path in sorted(workdir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(workdir).as_posix()
        if _is_build_artifact(rel, main_stem):
            continue
        rels.append(rel)
    return rels


async def build_source_bundle(
    session: AsyncSession, manuscript: Manuscript
) -> tuple[bytes, str]:
    """可编译的源码包（tar.gz）+ 内容指纹。

    与 arXiv 导出的区别：不重编、不生成 .bbl——这是给桌面端拿去**本地编译**的输入。
    组装本身深度依赖 DB 与活跃 CRDT 房间（见 assemble_workdir），所以只能在服务端做；
    本地编译的正确形态是「服务器组装 bundle → 客户端下载 → 本地编译」。
    """
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="polaris-bundle-") as tmp:
        workdir = Path(tmp)
        rels = await _assemble_source_files(session, manuscript, workdir)
        digest = _digest_of(workdir, rels)
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for rel in rels:
                tar.add(workdir / rel, arcname=rel)
    return buf.getvalue(), digest


async def build_source_digest(session: AsyncSession, manuscript: Manuscript) -> str:
    """只算指纹不打包——客户端用它判断本地缓存的编译产物是否还新鲜。"""
    with tempfile.TemporaryDirectory(prefix="polaris-digest-") as tmp:
        workdir = Path(tmp)
        rels = await _assemble_source_files(session, manuscript, workdir)
        return _digest_of(workdir, rels)


def _find_workdir_main(workdir: Path) -> str | None:
    """workdir 里的主 tex 文件名：main.tex 优先，否则含 \\documentclass 的 .tex。"""
    if (workdir / MAIN_TEX).is_file():
        return MAIN_TEX
    for path in sorted(workdir.rglob("*.tex")):
        try:
            if "\\documentclass" in path.read_text(encoding="utf-8", errors="ignore"):
                return path.relative_to(workdir).as_posix()
        except OSError:
            continue
    return None


async def build_arxiv_tarball(
    session: AsyncSession, manuscript: Manuscript
) -> tuple[bytes, list[str]]:
    """组装 arXiv 提交用清洁 tar.gz（源文件 + references.bib + figures + .bbl），
    剔除 aux/log/pdf 等。返回 (tar.gz 字节, 提示信息列表)。

    为保证 .bbl 与当前源一致，导出时**新编一遍**（有 tectonic 时）以生成 .bbl；
    无编译器则只打包源文件并提示。不产生版本快照、不改稿件状态。
    """
    notes: list[str] = []
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="polaris-arxiv-") as tmp:
        workdir = Path(tmp)
        # 只读组装（不打版本快照）
        await assemble_workdir(session, manuscript, workdir, take_snapshots=False)

        binary = _find_tectonic()
        main_name = _find_workdir_main(workdir)
        if binary is None:
            notes.append(
                "服务器未装编译器，未能生成 .bbl；如论文有参考文献，"
                "请本地编译后把 .bbl 一并放入包内再提交 arXiv。"
            )
        elif main_name is None:
            notes.append("未找到主 .tex 文件，未生成 .bbl。")
        else:
            run = await asyncio.to_thread(_run_tectonic_on, binary, workdir, main_name)
            # 无参考文献时本就没有 .bbl（正常）；仅超时时提示
            if run.timed_out and not (workdir / f"{Path(main_name).stem}.bbl").is_file():
                notes.append("生成 .bbl 超时；包内可能缺少 .bbl。")

        main_stem = Path(main_name).stem if main_name else None
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path in sorted(workdir.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(workdir).as_posix()
                # .bbl 要留（arXiv 需要），其余中间产物剔除
                if rel.lower().endswith(".bbl"):
                    tar.add(path, arcname=rel)
                    continue
                if _is_build_artifact(rel, main_stem):
                    continue
                tar.add(path, arcname=rel)
    return buf.getvalue(), notes


def _run_tectonic_on(binary: str, workdir: Path, main_name: str) -> TectonicRun:
    """对指定主文件跑一遍 tectonic（导出取 .bbl 用）。

    关键：加 --keep-intermediates，否则 tectonic 编译后会清掉 .bbl/.aux 等中间产物，
    arXiv 提交包就拿不到 .bbl。
    """
    try:
        proc = subprocess.run(  # noqa: S603 — 固定二进制 + 受控参数
            [binary, "--keep-intermediates", "--keep-logs", "--reruns", "3", main_name],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=COMPILE_TIMEOUT_SECONDS,
        )
        return TectonicRun(proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired:
        return TectonicRun(-1, "", "", timed_out=True)
