"""Golden transcript 归一化：把响应里的易变值替换成稳定占位符。

规则（顺序敏感，UUID 按首见顺序编号，跨步骤稳定）：
- UUID 字符串 → ``<uuid-N>``
- ISO 时间戳 → ``<ts>``
- JWT（三段 base64url）→ ``<token>``
- 临时目录绝对路径 → ``<path>``

其余内容必须逐字节稳定——fake provider 与 bibtex 离线导入保证这一点。
"""

import re
from typing import Any

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_UUID_INLINE_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$")
_PATH_RE = re.compile(r"(/private)?/(tmp|var)/[^\s\"]*")


class Normalizer:
    def __init__(self) -> None:
        self._uuids: dict[str, str] = {}

    def _uuid(self, value: str) -> str:
        if value not in self._uuids:
            self._uuids[value] = f"<uuid-{len(self._uuids) + 1}>"
        return self._uuids[value]

    def normalize(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: self.normalize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.normalize(v) for v in obj]
        if isinstance(obj, str):
            if _UUID_RE.match(obj):
                return self._uuid(obj)
            if _TS_RE.match(obj):
                return "<ts>"
            if _JWT_RE.match(obj):
                return "<token>"
            # 长文本里内嵌的 uuid / 路径（如 wiki 里的图片链接、错误信息）
            obj = _UUID_INLINE_RE.sub(lambda m: self._uuid(m.group(0)), obj)
            obj = _PATH_RE.sub("<path>", obj)
            return obj
        return obj
