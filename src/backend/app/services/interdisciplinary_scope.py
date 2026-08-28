"""Generate an editable interdisciplinary scope with structured fallback."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from app.core.llm.base import Message
from app.core.llm.router import LLMRouter, get_llm_router
from app.schemas.interdisciplinary import (
    InterdisciplinaryScopeSuggestion,
    InterdisciplinaryScopeSuggestRequest,
)

_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Structural engineering",
        (
            "structure",
            "structural",
            "impact load",
            "failure mode",
            "结构",
            "冲击荷载",
            "破坏模式",
        ),
    ),
    ("Mechanics", ("mechanics", "stress", "strain", "fracture", "力学", "应力", "断裂")),
    (
        "Materials science",
        ("material", "composite", "fatigue", "corrosion", "材料", "复合材料", "腐蚀"),
    ),
    (
        "Computer vision",
        ("sam3", "segmentation", "computer vision", "image", "分割", "计算机视觉", "图像"),
    ),
    (
        "Artificial intelligence",
        ("machine learning", "deep learning", "neural", "机器学习", "深度学习", "人工智能"),
    ),
    ("Statistics", ("statistical", "causal", "bayesian", "统计", "因果", "贝叶斯")),
    ("Data science", ("data-driven", "multimodal", "数据驱动", "多模态", "数据融合")),
    ("Medicine", ("clinical", "patient", "diagnosis", "临床", "患者", "诊断", "医学")),
    ("Life sciences", ("biology", "genomic", "cell", "生物", "基因", "细胞")),
    (
        "Environmental engineering",
        ("environment", "climate", "emission", "环境", "气候", "排放"),
    ),
)
_METHOD_DOMAINS = {"Computer vision", "Artificial intelligence", "Statistics", "Data science"}


def _json_objects(text: str) -> list[str]:
    source = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.I).strip()
    output: list[str] = []
    start: int | None = None
    depth = 0
    quoted = False
    escaped = False
    for index, char in enumerate(source):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                output.append(source[start : index + 1])
                start = None
    return list(reversed(output or [source]))


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return next(
            (_text(value.get(key)) for key in ("text", "name", "value") if value.get(key)), ""
        )
    if isinstance(value, list):
        return "; ".join(part for item in value if (part := _text(item)))
    return str(value).strip() if value is not None else ""


def _list(value: object, *, limit: int) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[,，;；\n]", value)
    elif isinstance(value, dict):
        values = list(value.values())
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return list(dict.fromkeys(text for item in values if (text := _text(item))))[:limit]


def parse_suggestion(content: str, *, model: str) -> InterdisciplinaryScopeSuggestion:
    payload: dict[str, Any] | None = None
    for candidate in _json_objects(content):
        try:
            value = json.loads(re.sub(r",\s*([}\]])", r"\1", candidate))
        except json.JSONDecodeError:
            continue
        while isinstance(value, dict) and not any(
            key in value for key in ("research_scope", "scope", "交叉研究范围")
        ):
            previous = value
            value = next(
                (
                    value[key]
                    for key in ("data", "result", "suggestion", "proposal")
                    if isinstance(value.get(key), dict)
                ),
                value,
            )
            if value is previous:
                break
        if isinstance(value, dict):
            payload = value
            break
    if payload is None:
        raise ValueError("INTERDISCIPLINARY_SCOPE_INVALID")

    def first(*names: str) -> object:
        return next((payload[name] for name in names if name in payload), None)

    scope = _text(first("research_scope", "scope", "交叉研究范围", "研究范围"))
    questions = _list(
        first("core_questions", "core_question", "coreQuestion", "核心交叉问题"), limit=12
    )
    primary = _text(first("primary_domain", "primary_discipline", "primaryDiscipline", "主学科"))
    related = _list(
        first("related_domains", "related_disciplines", "relatedDisciplines", "关联学科"),
        limit=12,
    )
    rationale = _text(first("rationale", "reason", "依据", "理由"))
    try:
        return InterdisciplinaryScopeSuggestion(
            research_scope=scope,
            core_questions=questions,
            primary_domain=primary,
            related_domains=related,
            evidence_boundary=_text(first("evidence_boundary", "证据边界")) or None,
            validation_conditions=_list(first("validation_conditions", "验证条件"), limit=12)
            or None,
            user_questions=None,
            clarification_questions=_list(
                first("clarification_questions", "clarificationQuestions", "澄清问题"), limit=4
            ),
            rationale=rationale or "Domains are separated by research object, method and evidence.",
            model=model,
        )
    except ValueError as exc:
        raise ValueError("INTERDISCIPLINARY_SCOPE_INVALID") from exc


def _fallback(
    data: InterdisciplinaryScopeSuggestRequest, *, model: str
) -> InterdisciplinaryScopeSuggestion:
    combined = " ".join((data.name, data.statement, data.user_context or "")).casefold()
    matches = [name for name, signals in _SIGNALS if any(signal in combined for signal in signals)]
    substantive = [name for name in matches if name not in _METHOD_DOMAINS]
    primary = substantive[0] if substantive else (matches[0] if matches else "Systems science")
    related = [name for name in matches if name != primary]
    if not related:
        related = ["Data science" if primary != "Data science" else "Statistics"]
    definition = data.statement.strip().rstrip("。.!！?")
    related_text = ", ".join(related[:4])
    return InterdisciplinaryScopeSuggestion(
        research_scope=(
            f"Study {definition} within the stated object and operating conditions. "
            f"Use {primary} to define mechanisms, response variables and validation criteria; "
            f"introduce {related_text} for non-substitutable methods or evidence, and validate "
            "the cross-domain mapping with independent experimental data or an "
            "engineering scenario."
        ),
        core_questions=[
            f"How can evidence from {related_text} be aligned with the mechanisms and evaluation "
            f"criteria of {primary} to test {definition}?"
        ],
        primary_domain=primary,
        related_domains=related[:4],
        evidence_boundary="Claims remain limited to the stated objects, data and conditions.",
        validation_conditions=["Validate the cross-domain mapping with independent evidence."],
        user_questions=None,
        clarification_questions=[
            f"Which observable responses and evaluation criteria should be used in {primary}?",
            f"Which data or method from {related_text} is indispensable?",
            "Which experiment, independent dataset or engineering condition provides validation?",
        ],
        rationale="The fallback is inferred from explicit object, method and validation signals.",
        model=f"{model}:evidence-fallback",
    )


async def suggest_scope(
    data: InterdisciplinaryScopeSuggestRequest,
    *,
    user_id: uuid.UUID,
    llm: LLMRouter | None = None,
) -> InterdisciplinaryScopeSuggestion:
    llm = llm or get_llm_router()
    prompt = (
        f"Topic: {data.name}\nDefinition: {data.statement}\n"
        f"Context: {data.user_context or 'None'}\n"
        "Return one JSON object with research_scope, core_questions, primary_domain, "
        "related_domains, evidence_boundary, validation_conditions, clarification_questions, "
        "and rationale. Use concrete disciplines; never use unknown or pending placeholders."
    )
    try:
        result = await llm.complete(
            "default",
            [
                Message(
                    role="system",
                    content=(
                        "Design a testable interdisciplinary research scope. The primary domain "
                        "owns the scientific question; related domains provide indispensable "
                        "methods or evidence. Return strict JSON only."
                    ),
                ),
                Message(role="user", content=prompt),
            ],
            temperature=0.2,
            max_tokens=1800,
            user_id=user_id,
        )
    except Exception:
        return _fallback(data, model="unavailable")
    try:
        return parse_suggestion(result.content, model=result.model)
    except ValueError:
        try:
            repaired = await llm.complete(
                "default",
                [
                    Message(
                        role="system",
                        content="Repair this answer into the required strict JSON only.",
                    ),
                    Message(
                        role="user", content=f"{prompt}\nInvalid answer:\n{result.content[:8000]}"
                    ),
                ],
                temperature=0,
                max_tokens=1800,
                user_id=user_id,
            )
            return parse_suggestion(repaired.content, model=repaired.model)
        except Exception:
            return _fallback(data, model=result.model)
