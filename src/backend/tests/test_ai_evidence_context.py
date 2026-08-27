"""AI 全文语料使用稳定证据编号，不允许凭空生成句子引用。"""

import uuid
from types import SimpleNamespace

from app.services.library_chat import LIBRARY_CHAT_SYSTEM_TEMPLATE, _evidence_lines, _evidence_ref


def test_evidence_reference_keeps_reader_locator():
    anchor_id = uuid.uuid4()
    paper_id = uuid.uuid4()
    anchor = SimpleNamespace(
        id=anchor_id,
        paper_id=paper_id,
        anchor_type="sentence",
        seq=3,
        paragraph_index=1,
        sentence_index=2,
        quoted_text="The observed response remained stable.",
        locator={"page_start": 4, "page_end": 4, "rects": [{"x0": 1.0, "y0": 2.0}]},
    )

    ref = _evidence_ref(anchor)

    assert ref["anchor_id"] == str(anchor_id)
    assert ref["sentence_no"] == 3
    assert ref["page_start"] == 4
    assert ref["href"].endswith(f"evidence={anchor_id}")


def test_evidence_prompt_only_exposes_supplied_sentence_numbers():
    rendered = _evidence_lines(
        2,
        [
            {"citation_no": 1, "quoted_text": "First grounded sentence."},
            {"citation_no": 2, "quoted_text": "Second grounded sentence."},
        ],
    )

    assert "[2·句1] First grounded sentence." in rendered
    assert "[2·句2] Second grounded sentence." in rendered
    assert "只有资料中明确提供的证据标记才能使用" in LIBRARY_CHAT_SYSTEM_TEMPLATE
