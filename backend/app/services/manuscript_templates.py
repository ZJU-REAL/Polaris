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
import io
import re
import shutil
import subprocess
import tempfile
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
_SKIP_SUFFIXES = {".pdf", ".log", ".aux", ".out", ".synctex", ".gz", ".zip", ".DS_Store"}


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
    members = _strip_common_prefix(members)
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


def _builtin_info(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": meta["key"],
        "name": meta.get("name", meta["key"]),
        "description": None,
        "source": "builtin",
        "scope": "global",
        "project_id": None,
        "engine": "tectonic",
        "page_limit": meta.get("page_limit"),
        "sections": meta.get("sections") or [],
        "unofficial": bool(meta.get("unofficial", True)),
        "downloadable": False,
        "file_count": 3,
    }


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
        "file_count": t.file_count,
    }


async def list_all(session: AsyncSession, *, project_id: uuid.UUID | None) -> list[dict[str, Any]]:
    infos = [_builtin_info(m) for m in manuscripts_service.list_templates()]
    infos += [_db_info(t) for t in await list_db_templates(session, project_id=project_id)]
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


async def template_section_keys(session: AsyncSession, ident: str) -> list[str]:
    """模板声明的可写分节（AI 起草选节用）；官方上传模板通常为空。"""
    if is_builtin(ident):
        return manuscripts_service.template_meta(ident).get("sections") or []
    tmpl = await get_db_template(session, ident)
    return (tmpl.sections if tmpl else None) or []


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
        "name": "浙江大学学位论文 (zjuthesis)",
        "description": "浙江大学本硕博学位论文 LaTeX 模板（XeLaTeX，含中文字体需求）",
        "git": "https://github.com/TheNetAdmin/zjuthesis.git",
        "engine": "xelatex",
        "sections": [],
        "page_limit": None,
    },
    {
        "key": "acl-official",
        "name": "ACL Rolling Review（官方样式）",
        "description": "ACL 官方 acl.sty / acl_natbib.bst，附示例 acl_latex.tex",
        "git": "https://github.com/acl-org/acl-style-files.git",
        "engine": "pdflatex",
        "sections": [],
        "page_limit": 8,
    },
    {
        "key": "iclr-official",
        "name": "ICLR（官方 Master-Template）",
        "description": "ICLR 官方投稿样式与示例",
        "git": "https://github.com/ICLR/Master-Template.git",
        "engine": "pdflatex",
        "sections": [],
        "page_limit": 10,
    },
    {
        "key": "neurips2026-official",
        "name": "NeurIPS 2026（官方样式）",
        "description": "NeurIPS 2026 官方 neurips_2026.sty 与示例",
        "zip": "https://media.neurips.cc/Conferences/NeurIPS2026/Formatting_Instructions_For_NeurIPS_2026.zip",
        "engine": "pdflatex",
        "sections": [],
        "page_limit": 9,
    },
    {
        "key": "icml2026-official",
        "name": "ICML 2026（官方样式）",
        "description": "ICML 2026 官方 icml2026.sty 与示例",
        "zip": "https://media.icml.cc/Conferences/ICML2026/Styles/icml2026.zip",
        "engine": "pdflatex",
        "sections": [],
        "page_limit": 8,
    },
]


def _git_clone_members(url: str) -> dict[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="polaris-tmpl-") as tmp:
        subprocess.run(
            ["git", "clone", "--depth", "1", url, tmp],
            check=True,
            capture_output=True,
            timeout=180,
        )
        root = Path(tmp)
        members: dict[str, bytes] = {}
        total = 0
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = _safe_member_path(path.relative_to(root).as_posix())
            if rel is None or path.stat().st_size > MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
            total += len(data)
            if total > MAX_TOTAL_BYTES or len(members) >= MAX_FILES:
                break
            members[rel] = data
        return members


async def _fetch_zip_members(url: str) -> dict[str, bytes]:
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return _read_zip(resp.content)


async def seed_one(
    session: AsyncSession, entry: dict[str, Any], *, created_by: uuid.UUID | None
) -> ManuscriptTemplate | None:
    """拉取并入库一个种子模板；已存在同 key 则跳过（幂等）。失败抛异常由调用方汇总。"""
    existing = await get_db_template(session, entry["key"])
    if existing is not None:
        return None
    if entry.get("git"):
        members = await asyncio.to_thread(_git_clone_members, entry["git"])
    else:
        members = await _fetch_zip_members(entry["zip"])
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
