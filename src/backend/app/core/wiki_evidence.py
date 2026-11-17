"""解读正文与证据定位信息分开存（#873）。

早期的图文编译把证据定位信息（哪一句对应全文里的哪个锚点）作为一段 HTML 注释
``<!-- polaris-ai-evidence:{json} -->`` 追加在解读正文末尾。正文是给人看的：渲染器
把注释当普通文字显示出来，导出、搜索和喂给模型的「论文解读」里也都带着这段 JSON。

现在定位信息单独存在 ``paper_wikis.evidence``。本模块是唯一认识旧格式的地方：
写入解读时（:class:`app.models.paper.PaperWiki` 的校验器）和存量迁移都靠它把旧格式
拆开，所以库里的正文永远不含这段注释。不 import 任何 app 模块，模型层可以直接用。
"""

from __future__ import annotations

import json
import re
from typing import Any

_MARKER = "<!-- polaris-ai-evidence:"
_MANIFEST_RE = re.compile(r"\s*<!-- polaris-ai-evidence:(\{.*?\}) -->[ \t]*\n?", re.DOTALL)


def split_evidence_manifest(text: str | None) -> tuple[str, dict[str, Any] | None]:
    """把正文里内嵌的证据注释拆出来，返回 ``(干净正文, 证据 | None)``。

    没有注释时正文原样返回。注释不论出现在哪、出现几次都删掉；证据取最后一段能
    解析的（JSON 坏掉也照删——用户不该看到它）。
    """
    value = text or ""
    if _MARKER not in value:
        return value, None
    evidence: dict[str, Any] | None = None
    for match in _MANIFEST_RE.finditer(value):
        try:
            parsed = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(parsed, dict):
            evidence = parsed
    body = _MANIFEST_RE.sub("\n\n", value).rstrip()
    return body, evidence
