"""Dataset loading + retrieval-corpus construction, with decontamination.

Test set: MATH-500 level-5 (hardest), clean `answer` field, answer-checkable.
Corpus : NuminaMath-CoT competition-style solved problems (worked solutions).
Both downloaded from HF (mirror + proxy injected by the platform env.sh).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
from common import WORKDIR  # noqa: E402

CACHE = WORKDIR / "data_cache"
CACHE.mkdir(exist_ok=True)


def _norm_problem(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]", " ", s.lower())).strip()[:400]


def load_testset(level: int = 5, limit: int | None = None) -> list[dict]:
    """MATH-500 filtered to a difficulty level. Returns [{id, problem, answer}]."""
    cache = CACHE / f"test_math500_l{level}.json"
    if cache.exists():
        rows = json.loads(cache.read_text())
    else:
        from datasets import load_dataset
        ds = load_dataset("HuggingFaceH4/MATH-500", split="test")
        rows = [
            {"id": r["unique_id"], "problem": r["problem"], "answer": r["answer"],
             "subject": r.get("subject", ""), "level": str(r.get("level", ""))}
            for r in ds if str(r.get("level", "")) == str(level)
        ]
        cache.write_text(json.dumps(rows, ensure_ascii=False))
    return rows[:limit] if limit else rows


def load_corpus(limit: int, testset: list[dict]) -> list[dict]:
    """NuminaMath-CoT competition-style solutions, decontaminated against the
    test problems (exact-normalized match). Returns [{problem, solution}]."""
    cache = CACHE / f"corpus_numina_{limit}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    from datasets import load_dataset
    banned = {_norm_problem(t["problem"]) for t in testset}
    keep_sources = {"olympiads", "aops_forum", "amc_aime", "cn_k12", "math"}
    rows: list[dict] = []
    seen: set[str] = set()
    ds = load_dataset("AI-MO/NuminaMath-CoT", split="train", streaming=True)
    for r in ds:
        src = (r.get("source") or "").lower()
        if keep_sources and src not in keep_sources:
            continue
        prob = (r.get("problem") or "").strip()
        sol = (r.get("solution") or "").strip()
        if len(prob) < 20 or len(sol) < 40:
            continue
        key = _norm_problem(prob)
        if key in banned or key in seen:
            continue
        seen.add(key)
        rows.append({"problem": prob, "solution": sol[:2000]})
        if len(rows) >= limit:
            break
    cache.write_text(json.dumps(rows, ensure_ascii=False))
    return rows
