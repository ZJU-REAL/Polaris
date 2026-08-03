"""技能加载工具：渐进披露的 L2 与 L3。

这两个工具存在的全部理由，是让**技能正文不必常驻 system prompt**。常驻的只有目录里
的一行 description；模型判断"这次用得上"才来取正文。正文作为工具结果追加，不改写
system prompt——改了会作废整个 prompt cache 前缀。
"""

from typing import Any

from app.core.db import get_sessionmaker
from app.services import agent_skills
from app.tools.context import ToolContext
from app.tools.registry import tool


@tool(
    name="skill_load",
    description=(
        "加载一个技能的正文。系统提示里的技能目录只给了名字和适用场景，"
        "判断某个技能这次用得上时，用它把完整做法取回来。"
    ),
    input_schema={
        "type": "object",
        "properties": {"slug": {"type": "string", "description": "技能目录里的名字"}},
        "required": ["slug"],
    },
    summarize=lambda args, result: (
        f"加载技能 {args.get('slug')}"
        + (f"（{len(result.get('files') or [])} 个附件）" if result.get("files") else "")
    ),
)
async def skill_load(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    slug = str(args.get("slug") or "").strip()
    if not slug:
        return {"error": "缺少 slug"}
    async with get_sessionmaker()() as session:
        loaded = await agent_skills.load_skill(session, slug=slug, user_id=ctx.user_id)
        await session.commit()
    if loaded is None:
        return {"error": f"没有这个技能：{slug}"}
    return loaded


@tool(
    name="skill_read_file",
    description=(
        "读技能目录里的附件（模板、参考资料、示例代码）。"
        "技能正文里提到某个文件时才用它，不要挨个试。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "path": {"type": "string", "description": "技能正文里给出的相对路径"},
        },
        "required": ["slug", "path"],
    },
    summarize=lambda args, result: f"读 {args.get('slug')}/{args.get('path')}",
)
async def skill_read_file(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    slug = str(args.get("slug") or "").strip()
    path = str(args.get("path") or "").strip()
    if not slug or not path:
        return {"error": "缺少 slug 或 path"}
    async with get_sessionmaker()() as session:
        content = await agent_skills.read_skill_file(
            session, slug=slug, path=path, user_id=ctx.user_id
        )
    if content is None:
        return {"error": f"技能 {slug} 里没有 {path}"}
    # 附件里的脚本**只能读、不能执行**：平台只有远程 SSH/容器 runner（命令走严格模板
    # 白名单），没有本地沙箱。它是给模型抄的参考实现，不是可运行的东西。
    return {"slug": slug, "path": path, "content": content}
