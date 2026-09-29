"""论文标识：arXiv 编号、DOI、PMID 的识别与规范化（#821）。

以前「论文从哪来」的输入只认 arXiv：不是 DOI 的一律当 arXiv 编号——一个 PMID
（``31452104``）会被存成 arxiv_id，然后在 arXiv 上查不到。三类标识在这里各有明确的
形状，认不出的就是认不出（返回 None），不再兜底成 arXiv。

前端 ``lib/paper-ref.ts`` 是同一套规则的镜像；两边的判定要一致，否则同一段粘贴内容
在表单上被识别成 DOI、到了服务端又被当成别的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

PaperRefKind = Literal["arxiv", "doi", "pmid"]

# 新式 2401.01234 / 2401.01234v2；老式 hep-th/9901001、math.GT/0309136
_ARXIV_NEW = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
_ARXIV_OLD = re.compile(r"^[a-z][a-z-]*(\.[A-Za-z]{2})?/\d{7}(v\d+)?$", re.IGNORECASE)
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")
# PubMed 编号是纯数字；上限 9 位，挡住把一长串数字（电话、ISBN）误认成 PMID
_PMID = re.compile(r"^\d{1,9}$")


@dataclass(frozen=True)
class PaperRef:
    kind: PaperRefKind
    value: str


def normalize_pmid(raw: str) -> str:
    """``PMID: 31452104`` / ``31452104`` / PubMed 链接 → ``31452104``；不是 PMID 抛 ValueError。"""
    value = raw.strip()
    match = re.search(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", value)
    if match:
        value = match.group(1)
    value = re.sub(r"^pmid\s*:?\s*", "", value, flags=re.IGNORECASE)
    # 前导零不算位数（有人会把编号补齐成定长）；全零不是编号
    digits = value.lstrip("0") if value.isdigit() else value
    if not digits or not _PMID.match(digits):
        raise ValueError(f"not a PubMed id: {raw!r}")
    return digits


def parse_paper_ref(raw: str) -> PaperRef | None:
    """一段粘贴内容 → (类型, 规范值)；认不出返回 None。

    接受链接（doi.org / arxiv.org/abs|pdf / pubmed.ncbi.nlm.nih.gov）与前缀
    （``doi:`` / ``arXiv:`` / ``PMID:``）。判定顺序：DOI → arXiv → PMID——
    纯数字只可能是 PMID，而 ``2401.01234`` 这种带点的只可能是 arXiv。
    """
    value = raw.strip()
    if not value:
        return None
    lowered = value.lower()

    if "doi.org/" in lowered:
        value = value[lowered.index("doi.org/") + len("doi.org/") :]
    elif lowered.startswith("doi:"):
        value = value[4:].strip()
    if _DOI.match(value):
        return PaperRef("doi", value)

    if "arxiv.org/" in lowered:
        tail = re.split(r"arxiv\.org/(?:abs|pdf)/", value, flags=re.IGNORECASE)[-1]
        value = re.sub(r"\.pdf$", "", tail.strip("/"), flags=re.IGNORECASE)
    elif lowered.startswith("arxiv:"):
        value = value[6:].strip()
    if _ARXIV_NEW.match(value) or _ARXIV_OLD.match(value):
        return PaperRef("arxiv", value)

    try:
        return PaperRef("pmid", normalize_pmid(raw))
    except ValueError:
        return None


def semantic_scholar_ref(kind: str, value: str) -> str:
    """Semantic Scholar 的论文引用写法：它按前缀区分标识类型。"""
    prefix = {"arxiv": "arXiv", "doi": "DOI", "pmid": "PMID"}[kind]
    return f"{prefix}:{value}"
