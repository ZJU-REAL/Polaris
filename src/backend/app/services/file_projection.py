"""常驻文件投影（#719，file-over-app 一期；审计 #715 第 4 项，设计报告 §17）。

一期语义 = 「常驻投影」：**DB 仍是唯一真源**，``<data_dir>/workspace/`` 只是一份
持续刷新的可见副本，让用户随时能用文件管理器 / Obsidian 看到自己的论文、笔记与解读
（desktop 档位下即 userData/engine/data/workspace，见 #718）：

    <data_dir>/workspace/
        README.md                      （投影说明：DB 为准、勿在此改动期待回写）
        papers/<citekey> - <标题>.pdf   （硬链接优先省空间，失败回退复制；
                                          <data_dir>/papers/<uuid>.pdf 原件不动，
                                          内部一切路径引用仍指向原件）
        notes/<论文名>.md               （复用全量导出的笔记渲染器 render_paper_notes_md）
        wiki/<库名>/…                   （复用 Obsidian vault 导出渲染，可直接用 Obsidian 打开）

冲突规则（一期诚实边界）：**DB wins**。用户在 workspace/ 里的改动不会回写平台，
且会在下一次对应数据变化时被整文件覆盖；「文件为源」的双向同步属后续阶段。

所有刷新都 best-effort：失败只记日志，绝不阻断业务保存路径——投影是旁路，
一次写文件失败不该让一次笔记保存 500。钩子受 ``POLARIS_FILE_PROJECTION`` 开关
控制（默认开；测试套件与 golden 链在 conftest 里关，投影专项测试单独开）。
存量数据回填走 ``python -m app.cli.build_file_projection``。
"""

import io
import logging
import os
import shutil
import uuid
import zipfile
from contextlib import suppress
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper, PaperHighlight, PaperNote

logger = logging.getLogger(__name__)

# 注意：本模块顶层只 import models / config，服务层（full_export / wiki_export /
# citations）一律函数内延迟 import——它们各自又 import 一大串 services，顶层引用
# 会和 notes.py / papers.py 等挂钩子的模块构成循环导入。

PDF_DIR = "papers"
NOTES_DIR = "notes"
WIKI_DIR = "wiki"

_README = """# Polaris workspace（文件投影 / file projection）

这里是你在 Polaris 里数据的一份**持续刷新的文件副本**，方便随时用文件管理器、
PDF 阅读器或 Obsidian 直接查看：

- `papers/`：已下载的论文 PDF，按「引用键 - 标题」命名；
- `notes/`：论文笔记与 PDF 划线，一篇论文一个 Markdown 文件；
- `wiki/<库名>/`：各文献库的论文解读，Obsidian vault 结构，可直接用 Obsidian 打开。

注意：**平台数据库才是唯一的真源**。在这个目录里改动或删除文件不会同步回平台，
且对应数据下次变化时这里的文件会被重新覆盖。想编辑笔记请回到平台里改；
想带走一份独立的完整数据，请用平台的「全量导出」。

Note: this folder is a continuously refreshed *projection* of your Polaris data.
The database remains the source of truth — edits made here are not synced back
and will be overwritten on the next refresh.
"""


def projection_enabled() -> bool:
    return get_settings().file_projection


def workspace_root() -> Path:
    """投影根目录（不负责创建；建目录统一走 :func:`_ensure_workspace`）。"""
    return Path(get_settings().data_dir) / "workspace"


def _ensure_workspace() -> Path:
    root = workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():  # 只在缺失时写：用户就算删了 README 也会补回，但不反复覆盖
        readme.write_text(_README, encoding="utf-8")
    return root


# ---- 命名 ----


def _display_name(title: str | None, paper_id: uuid.UUID) -> str:
    """标题 → 安全文件名成分（剔除路径分隔/控制字符 + 80 字符截断 + 空标题兜底）。

    直接复用全量导出的 safe_name（去重集合传空：投影按论文逐个刷新，没有一次
    导出那样的全局视角，同名论文由后写者覆盖——一期接受，见模块 docstring）。
    """
    from app.services.full_export import safe_name

    return safe_name(title, set(), fallback=str(paper_id)[:8])


def paper_pdf_filename(paper: Paper) -> str:
    """投影 PDF 文件名：``<citekey> - <安全化标题>.pdf``。

    citekey 复用引用导出的 assign_citation_keys（单篇视角＝无冲突后缀），
    与 papers.bib / 全量导出 pdfs/ 的命名同源，用户跨着看对得上。
    """
    from app.services.citations import assign_citation_keys

    key = assign_citation_keys([paper])[paper.id]
    return f"{key} - {_display_name(paper.title, paper.id)}.pdf"


def _notes_filename(paper: Paper) -> str:
    return f"{_display_name(paper.title, paper.id)}.md"


# ---- PDF 投影 ----


def project_paper_pdf(paper: Paper) -> Path | None:
    """把 ``<data_dir>/papers/<uuid>.pdf`` 原件投影成用户可辨认的别名文件。

    硬链接优先（零额外空间），文件系统不支持（跨设备 / 某些挂载卷）时回退复制。
    幂等：目标已指向同一原件直接返回；元数据变化导致旧别名残留时，按 samefile
    清掉旧硬链接再建新名（复制回退的旧副本识别不了，留给回填 CLI --force 兜底）。
    """
    if not projection_enabled():
        return None
    try:
        src = Path(paper.pdf_path) if paper.pdf_path else None
        if src is None or not src.is_file():
            return None
        pdf_dir = _ensure_workspace() / PDF_DIR
        pdf_dir.mkdir(parents=True, exist_ok=True)
        target = pdf_dir / paper_pdf_filename(paper)
        if target.exists():
            with suppress(OSError):
                if target.samefile(src):
                    return target  # 稳态：已投影且指向同一原件
            target.unlink(missing_ok=True)  # 同名但不是本原件（复制回退/罕见撞名）→ 覆盖重建
        # 旧别名清理：标题/citekey 变化后同一原件不该留两个投影名（仅硬链接可识别）
        for existing in pdf_dir.glob("*.pdf"):
            with suppress(OSError):
                if existing.samefile(src):
                    existing.unlink()
        try:
            os.link(src, target)
        except OSError:
            shutil.copyfile(src, target)  # 硬链接不可用（跨设备等）→ 回退复制
        return target
    except Exception:  # noqa: BLE001 — 投影是旁路，绝不打断业务写路径
        logger.warning("file projection: pdf link failed for %s", paper.id, exc_info=True)
        return None


def remove_paper_projection(paper: Paper) -> None:
    """论文本体被回收时清掉它的投影文件（PDF 别名 + 笔记 md）。

    必须在原件 unlink **之前**调用：旧硬链接别名要靠 samefile 对着原件识别。
    """
    if not projection_enabled():
        return
    try:
        root = workspace_root()
        pdf_dir = root / PDF_DIR
        with suppress(OSError):
            (pdf_dir / paper_pdf_filename(paper)).unlink(missing_ok=True)
        src = Path(paper.pdf_path) if paper.pdf_path else None
        if src is not None and src.is_file() and pdf_dir.is_dir():
            for existing in pdf_dir.glob("*.pdf"):
                with suppress(OSError):
                    if existing.samefile(src):
                        existing.unlink()
        with suppress(OSError):
            (root / NOTES_DIR / _notes_filename(paper)).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.warning("file projection: cleanup failed for %s", paper.id, exc_info=True)


# ---- 笔记 / 划线投影 ----


async def refresh_paper_notes(session: AsyncSession, paper_id: uuid.UUID) -> None:
    """重渲染一篇论文的 ``notes/<论文名>.md``；笔记划线都删光时移除文件。

    渲染器与全量导出共用（full_export.render_paper_notes_md）。投影落在服务器
    data_dir（desktop 档位即本人机器），所以带上该论文**全部作者**的笔记——
    这是数据卷层面的投影，不是某个用户的视角导出。
    """
    if not projection_enabled():
        return
    try:
        from app.services.full_export import render_paper_notes_md

        paper = await session.get(Paper, paper_id)
        if paper is None:
            return
        notes = list(
            (
                await session.execute(
                    select(PaperNote)
                    .where(PaperNote.paper_id == paper_id)
                    .order_by(PaperNote.created_at)
                )
            ).scalars()
        )
        highlights = list(
            (
                await session.execute(
                    select(PaperHighlight)
                    .where(PaperHighlight.paper_id == paper_id)
                    .order_by(PaperHighlight.page, PaperHighlight.created_at)
                )
            ).scalars()
        )
        target = workspace_root() / NOTES_DIR / _notes_filename(paper)
        if not notes and not highlights:
            with suppress(OSError):
                target.unlink(missing_ok=True)  # 最后一条删掉 → 文件一并清走
            return
        _ensure_workspace()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_paper_notes_md(paper, notes, highlights), encoding="utf-8")
    except Exception:  # noqa: BLE001 — 冲突规则 DB wins：写失败只记日志，下次变化再覆盖
        logger.warning("file projection: notes refresh failed for %s", paper_id, exc_info=True)


# ---- wiki vault 投影 ----


async def refresh_library_vault(session: AsyncSession, library: DirectionLibrary) -> None:
    """整库重建 ``wiki/<库名>/``（复用 Obsidian vault 导出 builder，zip 原地解包）。

    与 full_export._export_wiki 同一取舍：vault 里的图链重写 / 双链 / 笔记小节
    全在 builder 里，宁可 zip→解包一趟也不复制那段渲染逻辑。整目录先清后建，
    顺带带走已不在库里的论文页（DB wins）。
    笔记视角取库创建者（desktop 单用户即本人）；vault 内的笔记小节只在编译时
    刷新——逐条笔记保存只更新 notes/ 投影，不整库重建 vault（成本不成比例）。
    """
    if not projection_enabled():
        return
    try:
        from app.services.wiki_export import build_obsidian_zip_for_libraries

        data = await build_obsidian_zip_for_libraries(
            session,
            library_ids=[library.id],
            title=library.name,
            user_id=library.submitted_by,
        )
        name = _display_name(library.name, library.id)
        target = _ensure_workspace() / WIKI_DIR / name
        if target.exists():
            shutil.rmtree(target)  # target 恒在 workspace/wiki/ 之下（name 已安全化）
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(target)  # 条目名都是 builder 生成的 slug，无穿越风险
    except Exception:  # noqa: BLE001
        logger.warning("file projection: vault refresh failed for %s", library.id, exc_info=True)


async def refresh_wiki_vaults_for_paper(session: AsyncSession, paper_id: uuid.UUID) -> None:
    """一篇论文解读更新后，刷新所有含它的库 vault（每日池论文可能不属于任何库→跳过）。"""
    if not projection_enabled():
        return
    try:
        libraries = (
            (
                await session.execute(
                    select(DirectionLibrary)
                    .join(LibraryPaper, LibraryPaper.library_id == DirectionLibrary.id)
                    .where(LibraryPaper.paper_id == paper_id)
                )
            )
            .scalars()
            .all()
        )
        for library in libraries:
            await refresh_library_vault(session, library)
    except Exception:  # noqa: BLE001
        logger.warning("file projection: vaults refresh failed for %s", paper_id, exc_info=True)


async def refresh_library_vault_by_id(library_id: uuid.UUID) -> None:
    """批量编译（voyage wiki.compile）收尾用：自开 session、整批只重建一次 vault。"""
    if not projection_enabled():
        return
    try:
        from app.core.db import get_sessionmaker

        async with get_sessionmaker()() as session:
            library = await session.get(DirectionLibrary, library_id)
            if library is not None:
                await refresh_library_vault(session, library)
    except Exception:  # noqa: BLE001
        logger.warning("file projection: vault refresh failed for %s", library_id, exc_info=True)
