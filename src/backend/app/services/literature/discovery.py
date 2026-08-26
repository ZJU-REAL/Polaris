"""文献发现的确定性合同和候选身份规则。"""

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from app.schemas.literature_discovery import LiteratureCandidate


def _normalized_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).lower().strip()
    return re.sub(r"\s+", " ", text)


def _normalized_identifier(value: str | None) -> str:
    return _normalized_text(value).rstrip(".")


def candidate_dedup_key(candidate: LiteratureCandidate | Mapping[str, Any]) -> str:
    """按稳定优先级生成跨来源候选身份键。"""

    data = candidate.model_dump() if isinstance(candidate, LiteratureCandidate) else candidate
    for field, prefix in (
        ("doi", "doi"),
        ("pmid", "pmid"),
        ("arxiv_id", "arxiv"),
        ("semantic_scholar_id", "s2"),
    ):
        value = _normalized_identifier(data.get(field))
        if value:
            return f"{prefix}:{value}"

    title = _normalized_text(data.get("title"))
    year = str(data.get("year") or "")
    authors = data.get("authors") or []
    first_author = ""
    if authors:
        first = authors[0]
        if isinstance(first, Mapping):
            first_author = _normalized_text(str(first.get("name") or first.get("family") or ""))
        else:
            first_author = _normalized_text(str(first))
    fingerprint = "|".join((title, year, first_author))
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return f"title:{digest}"


def validate_candidate(candidate: LiteratureCandidate) -> LiteratureCandidate:
    """统一清理可持久化候选，避免空字符串冒充有值字段。"""

    values = candidate.model_dump()
    for key in ("doi", "pmid", "arxiv_id", "semantic_scholar_id", "url", "pdf_url", "venue"):
        if isinstance(values.get(key), str):
            values[key] = values[key].strip() or None
    values["title"] = " ".join(candidate.title.split())
    values["source"] = candidate.source.strip().lower()
    return LiteratureCandidate.model_validate(values)
