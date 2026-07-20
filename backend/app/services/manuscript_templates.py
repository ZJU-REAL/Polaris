"""论文模板库（DB 元数据 + data_dir/templates/<id>/files 下的文件）。

三类模板统一对外：
- builtin：app/assets/templates 内的 3 个简化样式（不入库，读文件系统）
- seeded：从 git/zip 拉来的官方模板（入库 + 落盘，见 SEED_MANIFEST）
- uploaded：用户上传的 zip（入库 + 落盘）

对外主入口：
- list_all(session, project_id) → 合并 builtin + 库内可见模板的 TemplateInfo dict
- resolve(session, ident) → 统一解析（builtin key 或库内 id/key）
- expand_files(...) → [(path, data, readonly, is_binary)]，供 create_manuscript 展开
- create_from_zip / zip_bytes / delete / seed_manifest
"""

import asyncio
import contextlib
import io
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.manuscript import ManuscriptTemplate
from app.services import manuscripts as manuscripts_service

# 内置简化模板（保留在仓库里，读 app/assets/templates）
BUILTIN_KEYS = manuscripts_service.TEMPLATE_KEYS

READONLY_SUFFIXES = (".sty", ".cls", ".bst", ".bbl", ".bib", ".clo", ".def")
# zip 上传 / 种子拉取的上限，避免撑爆磁盘
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 400
_SKIP_DIRS = {".git", "__MACOSX", ".github", ".vscode", "node_modules"}
# 注：.pdf 不在此列——很多模板用 PDF 图作 \includegraphics 素材（如 ICML 的
# icml_numpapers.pdf）。编译产物样例 PDF（与某 .tex 同名）另在 _drop_output_pdfs 里剔除
_SKIP_SUFFIXES = {".log", ".aux", ".out", ".synctex", ".gz", ".zip", ".DS_Store"}


class TemplateError(Exception):
    """模板导入/校验失败（附大白话原因）。"""


# ---- 磁盘布局 ----


def _store_dir() -> Path:
    return Path(get_settings().data_dir) / "templates"


def template_files_dir(template_id: uuid.UUID | str) -> Path:
    return _store_dir() / str(template_id) / "files"


# ---- 文件分类 ----


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:64] or "template"


def is_binary_bytes(data: bytes) -> bool:
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def _readonly_for(path: str, is_binary: bool) -> bool:
    return is_binary or Path(path).suffix.lower() in READONLY_SUFFIXES


def _safe_member_path(name: str) -> str | None:
    """归一化 zip 成员路径；拒绝绝对路径/.. 穿越；跳过隐藏与噪声目录。"""
    norm = name.replace("\\", "/").strip("/")
    if not norm or norm.endswith("/"):
        return None
    parts = norm.split("/")
    if any(p in {"", "..", "."} for p in parts):
        return None
    if any(p in _SKIP_DIRS for p in parts[:-1]):
        return None
    if parts[-1].startswith(".") or Path(parts[-1]).suffix.lower() in _SKIP_SUFFIXES:
        return None
    return norm


def _detect_main_tex(paths: list[str], contents: dict[str, bytes]) -> str:
    """主文件：优先 main.tex / 顶层唯一 .tex / 含 \\documentclass 的 .tex。"""
    texs = [p for p in paths if p.lower().endswith(".tex")]
    if not texs:
        raise TemplateError("模板里没有 .tex 文件，无法确定主文件")
    for p in texs:
        if Path(p).name.lower() == "main.tex":
            return p
    top = [p for p in texs if "/" not in p]
    for p in top or texs:
        body = contents.get(p, b"")
        if b"\\documentclass" in body or b"\\begin{document}" in body:
            return p
    return (top or texs)[0]


# ---- 从文件字典落盘 + 建记录 ----


def _strip_common_prefix(members: dict[str, bytes]) -> dict[str, bytes]:
    """zip 常带一层包裹目录（repo 名/版本目录），若所有文件共享同一顶层目录则剥掉。"""
    tops = {p.split("/", 1)[0] for p in members if "/" in p}
    roots = {p for p in members if "/" not in p}
    if len(tops) == 1 and not roots:
        prefix = tops.pop() + "/"
        return {p[len(prefix) :]: b for p, b in members.items()}
    return members


def _drop_output_pdfs(members: dict[str, bytes]) -> dict[str, bytes]:
    """剔除「编译产物样例」PDF（与某 .tex 同名，如 example_paper.pdf 对 example_paper.tex），
    但保留作图片素材的 PDF（如 ICML 的 icml_numpapers.pdf，被 \\includegraphics 引用）。"""
    tex_stems = {p.rsplit(".", 1)[0] for p in members if p.lower().endswith(".tex")}
    return {
        p: b
        for p, b in members.items()
        if not (p.lower().endswith(".pdf") and p.rsplit(".", 1)[0] in tex_stems)
    }


def _select_subdir(members: dict[str, bytes], subdir: str) -> dict[str, bytes]:
    """只保留仓库内某个子目录（并把它拍平到根）。用于「一个仓库塞了多套模板」的情况
    （如 ICLR Master-Template 把各年份样式放在 iclrYYYY/ 子目录，且重名 .sty 互相干扰）。
    子目录不存在时退回全量（安全兜底）。"""
    flat = _strip_common_prefix(members)
    prefix = subdir.strip("/") + "/"
    selected = {p[len(prefix) :]: b for p, b in flat.items() if p.startswith(prefix)}
    return selected or flat


async def _persist(
    session: AsyncSession,
    *,
    name: str,
    description: str | None,
    source: str,
    scope: str,
    project_id: uuid.UUID | None,
    created_by: uuid.UUID | None,
    members: dict[str, bytes],
    engine: str,
    sections: list[str] | None,
    page_limit: int | None,
    meta: dict[str, Any] | None,
    key: str | None = None,
) -> ManuscriptTemplate:
    members = _drop_output_pdfs(_strip_common_prefix(members))
    if not members:
        raise TemplateError("压缩包为空或只含被忽略的文件")
    main_tex = _detect_main_tex(list(members), members)

    key = key or f"{_slugify(name)}-{uuid.uuid4().hex[:6]}"
    tmpl = ManuscriptTemplate(
        key=key,
        name=name[:256],
        description=(description or None),
        source=source,
        scope=scope,
        project_id=project_id,
        created_by=created_by,
        main_tex=main_tex,
        engine=engine,
        page_limit=page_limit,
        sections=sections,
        unofficial=False,
        file_count=len(members),
        meta=meta,
    )
    session.add(tmpl)
    await session.flush()  # 拿 id

    dest = template_files_dir(tmpl.id)
    dest.mkdir(parents=True, exist_ok=True)
    for rel, data in members.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    await session.commit()
    await session.refresh(tmpl)
    return tmpl


def _read_zip(zip_bytes: bytes) -> dict[str, bytes]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as e:
        raise TemplateError("不是有效的 zip 文件") from e
    members: dict[str, bytes] = {}
    total = 0
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = _safe_member_path(info.filename)
            if rel is None:
                continue
            if info.file_size > MAX_FILE_BYTES:
                continue
            data = zf.read(info)
            total += len(data)
            if total > MAX_TOTAL_BYTES or len(members) >= MAX_FILES:
                raise TemplateError("模板过大（超出 64MB / 400 文件上限）")
            members[rel] = data
    return members


async def create_from_zip(
    session: AsyncSession,
    *,
    name: str,
    zip_bytes: bytes,
    scope: str = "global",
    project_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
    description: str | None = None,
    engine: str = "tectonic",
    sections: list[str] | None = None,
    page_limit: int | None = None,
) -> ManuscriptTemplate:
    members = _read_zip(zip_bytes)
    return await _persist(
        session,
        name=name,
        description=description,
        source="uploaded",
        scope=scope,
        project_id=project_id,
        created_by=created_by,
        members=members,
        engine=engine,
        sections=sections,
        page_limit=page_limit,
        meta={"origin": "upload"},
    )


# ---- 查询 / 解析 ----


async def list_db_templates(
    session: AsyncSession, *, project_id: uuid.UUID | None
) -> list[ManuscriptTemplate]:
    cond = ManuscriptTemplate.scope == "global"
    if project_id is not None:
        cond = or_(cond, ManuscriptTemplate.project_id == project_id)
    rows = await session.execute(
        select(ManuscriptTemplate).where(cond).order_by(ManuscriptTemplate.created_at.desc())
    )
    return list(rows.scalars())


def _db_info(t: ManuscriptTemplate) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "name": t.name,
        "description": t.description,
        "source": t.source,
        "scope": t.scope,
        "project_id": str(t.project_id) if t.project_id else None,
        "engine": t.engine,
        "page_limit": t.page_limit,
        "sections": t.sections or [],
        "unofficial": t.unofficial,
        "downloadable": True,
        "downloaded": True,
        "download_key": None,
        "file_count": t.file_count,
    }


def _manifest_info(entry: dict[str, Any]) -> dict[str, Any]:
    """尚未下载的官方模板（画廊里显示为「未下载」，选用时按需下载）。"""
    return {
        "id": f"seed:{entry['key']}",
        "name": entry["name"],
        "description": entry.get("description"),
        "source": "seeded",
        "scope": "global",
        "project_id": None,
        "engine": entry.get("engine", "tectonic"),
        "page_limit": entry.get("page_limit"),
        "sections": entry.get("sections") or [],
        "unofficial": False,
        "downloadable": False,
        "downloaded": False,
        "download_key": entry["key"],
        "file_count": 0,
    }


async def list_all(session: AsyncSession, *, project_id: uuid.UUID | None) -> list[dict[str, Any]]:
    """画廊：只列「提供官方样式项目」的会议模板 + 用户上传。

    内置简化模板（neurips2026/iclr2026/acl）与官方 manifest 项重复且只是占位，
    不再在画廊显示（create_manuscript 仍接受其 key，供内部/测试使用）。
    """
    db_templates = await list_db_templates(session, project_id=project_id)
    infos = [_db_info(t) for t in db_templates]
    # manifest 里还没下载的官方模板 → 显示为「未下载」，供按需下载
    have_keys = {t.key for t in db_templates}
    infos += [_manifest_info(e) for e in SEED_MANIFEST if e["key"] not in have_keys]
    return infos


async def get_db_template(session: AsyncSession, ident: str) -> ManuscriptTemplate | None:
    try:
        tid = uuid.UUID(ident)
    except ValueError:
        row = await session.execute(
            select(ManuscriptTemplate).where(ManuscriptTemplate.key == ident)
        )
        return row.scalar_one_or_none()
    return await session.get(ManuscriptTemplate, tid)


def is_builtin(ident: str) -> bool:
    return ident in BUILTIN_KEYS


# 模板未声明分节时（官方样式模板通常不带 POLARIS_SECTION 标记）的兜底可写分节，
# 让 AI 起草仍可用（内容以 % POLARIS_SECTION 标记块追加，见 crdt_rooms.replace_section）
DEFAULT_DRAFT_SECTIONS = [
    "abstract",
    "introduction",
    "related_work",
    "method",
    "experimental_setup",
    "results",
    "conclusion",
]


async def template_section_keys(session: AsyncSession, ident: str) -> list[str]:
    """模板可写分节（AI 起草选节用）。模板未声明（官方样式模板常见）→ 用标准兜底，
    否则用会议模板起草会因「无合法分节」报 INVALID_SECTIONS。"""
    if is_builtin(ident):
        declared = manuscripts_service.template_meta(ident).get("sections") or []
    else:
        tmpl = await get_db_template(session, ident)
        declared = (tmpl.sections if tmpl else None) or []
    return declared or list(DEFAULT_DRAFT_SECTIONS)


# ---- 展开为稿件文件 ----


def _expand_db_files(
    template: ManuscriptTemplate, *, title: str
) -> list[tuple[str, Any, bool, bool]]:
    root = template_files_dir(template.id)
    out: list[tuple[str, Any, bool, bool]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        binary = is_binary_bytes(data)
        if binary:
            out.append((rel, data, True, True))
        else:
            text = data.decode("utf-8")
            if rel == template.main_tex:
                text = text.replace(
                    manuscripts_service._TITLE_PLACEHOLDER,
                    manuscripts_service._escape_latex(title),
                )
            out.append((rel, text, _readonly_for(rel, False), False))
    return out


async def expand_files(
    session: AsyncSession, ident: str, *, title: str
) -> list[tuple[str, Any, bool, bool]]:
    """→ [(path, data, readonly, is_binary)]。builtin 走 assets（全文本），
    库内模板走 fs（可含二进制）。未知 ident 抛 TemplateNotFoundError。"""
    if is_builtin(ident):
        return [
            (p, c, ro, False)
            for (p, c, ro) in manuscripts_service.template_files(ident, title=title)
        ]
    tmpl = await get_db_template(session, ident)
    if tmpl is None:
        raise manuscripts_service.TemplateNotFoundError(ident)
    return _expand_db_files(tmpl, title=title)


async def build_config(session: AsyncSession, ident: str) -> tuple[str, str]:
    """稿件的编译入口与编译器 (main_tex, engine)：builtin 用 main.tex + meta.engine，
    库内模板用检测到的主文件与其 engine。供 create_manuscript 初始化稿件设置。"""
    if is_builtin(ident):
        engine = manuscripts_service.template_meta(ident).get("engine") or "tectonic"
        return "main.tex", engine
    tmpl = await get_db_template(session, ident)
    if tmpl is None:
        raise manuscripts_service.TemplateNotFoundError(ident)
    return tmpl.main_tex or "main.tex", tmpl.engine or "tectonic"


# ---- 下载（打回 zip） ----


def zip_bytes(template: ManuscriptTemplate) -> bytes:
    root = template_files_dir(template.id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(root).as_posix())
    return buf.getvalue()


async def delete_template(session: AsyncSession, template: ManuscriptTemplate) -> None:
    tid = template.id
    await session.delete(template)
    await session.commit()
    shutil.rmtree(_store_dir() / str(tid), ignore_errors=True)


# ---- 种子：官方模板 ----

# 每条：从 git 仓库或 zip URL 拉取 → 过滤 → 建 seeded 模板
SEED_MANIFEST: list[dict[str, Any]] = [
    {
        "key": "zjuthesis",
        "name": "浙江大学学位论文",
        "description": "浙江大学本硕博学位论文 LaTeX 模板（XeLaTeX，含中文字体需求）",
        "git": "https://github.com/TheNetAdmin/zjuthesis.git",
        "engine": "xelatex",
        "sections": [],
        "page_limit": None,
    },
    {
        "key": "acl-official",
        "name": "ACL Rolling Review",
        "description": "ACL 官方 acl.sty / acl_natbib.bst，附示例 acl_latex.tex",
        "git": "https://github.com/acl-org/acl-style-files.git",
        "engine": "pdflatex",
        "sections": [],
        "page_limit": 8,
    },
    {
        "key": "iclr-official",
        "name": "ICLR 2026",
        "description": "ICLR 2026 官方投稿样式与示例",
        "git": "https://github.com/ICLR/Master-Template.git",
        # Master-Template 仓库把各年份塞在 iclrYYYY/ 子目录且重名 .sty 互相干扰，
        # 只取最新一年拍平到根，避免「找不到 iclr2026_conference.sty」类报错
        "subdir": "iclr2026",
        "engine": "pdflatex",
        "sections": [],
        "page_limit": 10,
    },
    {
        "key": "neurips2026-official",
        "name": "NeurIPS 2026",
        "description": "NeurIPS 2026 官方 neurips_2026.sty 与示例",
        "zip": "https://media.neurips.cc/Conferences/NeurIPS2026/Formatting_Instructions_For_NeurIPS_2026.zip",
        "engine": "pdflatex",
        "sections": [],
        "page_limit": 9,
    },
    {
        "key": "icml2026-official",
        "name": "ICML 2026",
        "description": "ICML 2026 官方 icml2026.sty 与示例",
        "zip": "https://media.icml.cc/Conferences/ICML2026/Styles/icml2026.zip",
        "engine": "pdflatex",
        "sections": [],
        "page_limit": 8,
    },
]


MANIFEST_BY_KEY: dict[str, dict[str, Any]] = {e["key"]: e for e in SEED_MANIFEST}


# ---- 下载进度（按需自动下载官方模板，供进度条 SSE 读取） ----
#
# 进度在 API 进程内存里（单机 dev 足够）；多进程部署时 SSE 若命中别的 worker
# 会读不到进度——那种场景应改用 redis/worker，见 TODO。下载本身事务化（末尾
# 一次性 _persist 入库），进程重启中断只是没建成模板，重试即可。

# key → {key, name, phase, percent, detail, template_id?, error?}
# phase ∈ pending | downloading | extracting | done | failed
_download_progress: dict[str, dict[str, Any]] = {}
_download_locks: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    lock = _download_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _download_locks[key] = lock
    return lock


def _set_progress(key: str, **fields: Any) -> None:
    cur = _download_progress.setdefault(
        key,
        {
            "key": key,
            "name": MANIFEST_BY_KEY.get(key, {}).get("name", key),
            "phase": "pending",
            "percent": 0,
            "detail": "",
        },
    )
    cur.update(fields)


def get_progress(key: str) -> dict[str, Any] | None:
    return _download_progress.get(key)


_GITHUB_RE = re.compile(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$")


def _github_archive_url(git_url: str) -> str:
    """GitHub 仓库 → 默认分支 zip 归档（api.github.com zipball 302 到 codeload）。

    改用归档 zip 下载而非 git clone：容器无需装 git，且能拿到字节进度。
    """
    m = _GITHUB_RE.search(git_url)
    if m is None:
        raise TemplateError(f"无法解析 GitHub 仓库地址：{git_url}")
    return f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/zipball"


def _source_url(entry: dict[str, Any]) -> str:
    """manifest 条目 → 下载 URL（git 源转成 GitHub 归档 zip）。"""
    return _github_archive_url(entry["git"]) if entry.get("git") else entry["zip"]


async def _fetch_zip_members(url: str, on_percent: Any | None = None) -> dict[str, bytes]:
    # GitHub API 要求带 User-Agent，否则 403
    headers = {"User-Agent": "Polaris-Template-Fetch"}
    async with httpx.AsyncClient(timeout=120, follow_redirects=True, headers=headers) as client:
        if on_percent is None:
            resp = await client.get(url)
            resp.raise_for_status()
            return _read_zip(resp.content)
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            got = 0
            chunks: list[bytes] = []
            async for chunk in resp.aiter_bytes(64 * 1024):
                chunks.append(chunk)
                got += len(chunk)
                if total > 0:
                    on_percent(min(99, int(got * 100 / total)))
                else:
                    # 无 content-length（GitHub 归档 zip 是动态生成的分块响应）：
                    # 用字节数估一个渐近百分比（永不到 100），让进度条能动起来
                    on_percent(min(90, int(got / (got + 2 * 1024 * 1024) * 100)))
        return _read_zip(b"".join(chunks))


async def download_template(key: str, *, created_by: uuid.UUID | None) -> ManuscriptTemplate:
    """按需拉取并入库一个官方模板（幂等，带进度）。已存在同 key 直接返回。

    进度写 _download_progress[key]，供 SSE 端点读取。
    """
    from app.core.db import get_sessionmaker

    entry = MANIFEST_BY_KEY.get(key)
    if entry is None:
        raise TemplateError(f"未知的官方模板：{key}")

    async with _lock_for(key):
        async with get_sessionmaker()() as session:
            existing = await get_db_template(session, key)
            if existing is not None:
                _set_progress(key, phase="done", percent=100, template_id=str(existing.id))
                return existing

        _set_progress(
            key, name=entry["name"], phase="downloading", percent=0, detail="", error=None
        )
        try:
            _set_progress(key, detail="下载模板压缩包…")

            def _cb(pct: int) -> None:
                _set_progress(key, phase="downloading", percent=pct)

            members = await _fetch_zip_members(_source_url(entry), _cb)
            if entry.get("subdir"):
                members = _select_subdir(members, entry["subdir"])

            _set_progress(key, phase="extracting", percent=100, detail="解压入库…")
            async with get_sessionmaker()() as session:
                tmpl = await _persist(
                    session,
                    name=entry["name"],
                    description=entry.get("description"),
                    source="seeded",
                    scope="global",
                    project_id=None,
                    created_by=created_by,
                    members=members,
                    engine=entry.get("engine", "tectonic"),
                    sections=entry.get("sections") or [],
                    page_limit=entry.get("page_limit"),
                    meta={"origin": entry.get("git") or entry.get("zip")},
                    key=key,
                )
            _set_progress(key, phase="done", percent=100, detail="", template_id=str(tmpl.id))
            return tmpl
        except Exception as e:  # noqa: BLE001 — 记进度，供前端提示
            _set_progress(key, phase="failed", error=f"{type(e).__name__}: {e}"[:300])
            raise


_download_tasks: set[asyncio.Task] = set()


def spawn_download(key: str, *, created_by: uuid.UUID | None) -> dict[str, Any]:
    """幂等地在后台启动一个官方模板下载；已在下载/已完成则不重复启动。
    返回当前进度快照供前端立刻显示。"""
    if key not in MANIFEST_BY_KEY:
        raise TemplateError(f"未知的官方模板：{key}")
    prog = get_progress(key)
    if prog and prog["phase"] in ("downloading", "extracting", "done"):
        return prog
    _set_progress(key, name=MANIFEST_BY_KEY[key]["name"], phase="pending", percent=0, error=None)

    task = asyncio.create_task(download_template(key, created_by=created_by))
    _download_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _download_tasks.discard(t)
        with contextlib.suppress(Exception):
            t.exception()  # 消费异常，避免 "Task exception never retrieved" 警告

    task.add_done_callback(_done)
    return get_progress(key) or {}


async def seed_one(
    session: AsyncSession, entry: dict[str, Any], *, created_by: uuid.UUID | None
) -> ManuscriptTemplate | None:
    """拉取并入库一个种子模板；已存在同 key 则跳过（幂等）。失败抛异常由调用方汇总。"""
    existing = await get_db_template(session, entry["key"])
    if existing is not None:
        return None
    members = await _fetch_zip_members(_source_url(entry))
    if entry.get("subdir"):
        members = _select_subdir(members, entry["subdir"])
    return await _persist(
        session,
        name=entry["name"],
        description=entry.get("description"),
        source="seeded",
        scope="global",
        project_id=None,
        created_by=created_by,
        members=members,
        engine=entry.get("engine", "tectonic"),
        sections=entry.get("sections") or [],
        page_limit=entry.get("page_limit"),
        meta={"origin": entry.get("git") or entry.get("zip")},
        key=entry["key"],
    )
