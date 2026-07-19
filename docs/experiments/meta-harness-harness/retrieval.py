"""Retrieval backends over the solution corpus: BM25 (CPU) and dense (BGE-M3 API).

Dense embeddings cached to disk (numpy float32) so repeated runs / models reuse them.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import numpy as np

from common import WORKDIR, embed

_CACHE = WORKDIR / "data_cache"


def _tok(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


class BM25:
    def __init__(self, docs: list[str]):
        from rank_bm25 import BM25Okapi
        self._impl = BM25Okapi([_tok(d) for d in docs])

    def top(self, query: str, k: int) -> list[int]:
        scores = self._impl.get_scores(_tok(query))
        return list(np.argsort(scores)[::-1][:k])


class Dense:
    def __init__(self, docs: list[str], tag: str):
        cache = _CACHE / f"emb_{tag}.json"  # corpus 向量与模型无关，多模型复用
        if cache.exists():
            mat = np.array(json.loads(cache.read_text()), dtype=np.float32)
        else:
            mat = np.array(embed(docs), dtype=np.float32)
            cache.write_text(json.dumps(mat.tolist()))
        self._mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
        self._qpath = _CACHE / "emb_queries.json"
        self._qcache: dict = json.loads(self._qpath.read_text()) if self._qpath.exists() else {}

    def _query_vec(self, query: str) -> np.ndarray:
        key = str(hash(query))
        if key not in self._qcache:
            self._qcache[key] = embed([query])[0]
            tmp = self._qpath.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._qcache))
            tmp.replace(self._qpath)
        v = np.array(self._qcache[key], dtype=np.float32)
        return v / (np.linalg.norm(v) + 1e-9)

    def top(self, query: str, k: int) -> list[int]:
        scores = self._mat @ self._query_vec(query)
        return list(np.argsort(scores)[::-1][:k])
