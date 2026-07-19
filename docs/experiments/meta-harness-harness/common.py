"""Meta-Harness reproduction — shared utilities.

Retrieval-augmented math reasoning (paper §4.2 / Table 6): compare a fixed LLM's
pass@1 accuracy on hard competition math under three harnesses —
no-retrieval baseline vs BM25 vs dense (BGE-M3) retrieval of worked solutions.

Model + embeddings go through an OpenAI-compatible endpoint (LiteLLM), read from
llm_config.json (platform-injected) or POLARIS_LLM_* env. No GPU needed.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WORKDIR = Path(os.environ.get("POLARIS_WORKDIR", ".")).resolve()


# ---------------- LLM / embedding client (OpenAI-compatible) ----------------

def _llm_conf() -> dict:
    p = WORKDIR / "llm_config.json"
    if p.exists():
        c = json.loads(p.read_text())
        return {"base_url": c["base_url"].rstrip("/"), "api_key": c["api_key"]}
    return {
        "base_url": os.environ["POLARIS_LLM_BASE_URL"].rstrip("/"),
        "api_key": os.environ["POLARIS_LLM_API_KEY"],
    }


CONF = _llm_conf()


def _post(path: str, payload: dict, timeout: float = 180.0, retries: int = 8) -> dict:
    url = f"{CONF['base_url']}{path}"
    body = json.dumps(payload).encode()
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {CONF['api_key']}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:200]!r}"
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(min(30, 2 ** attempt * 2))
                continue
            raise RuntimeError(last)
        except Exception as e:  # noqa: BLE001 — network flake, retry
            last = f"{type(e).__name__}: {e}"
            time.sleep(min(30, 2 ** attempt * 2))
    raise RuntimeError(f"request failed after {retries} tries: {last}")


def chat(model: str, prompt: str, max_tokens: int = 4096, temperature: float = 0.4) -> tuple[str, int]:
    """Return (answer_text, completion_tokens). Reasoning models put the final
    answer in content; we also keep reasoning_content as a fallback for \\boxed."""
    payload = {"model": model, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    if temperature is not None:
        payload["temperature"] = temperature
    d = _post("/chat/completions", payload)
    if "choices" not in d:
        raise RuntimeError(f"bad completion: {str(d)[:200]}")
    msg = d["choices"][0].get("message", {}) or {}
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    text = content if _extract_boxed(content) else (content + "\n" + reasoning)
    tok = int((d.get("usage") or {}).get("completion_tokens") or 0)
    return text, tok


_EMBED_LOCK = __import__("threading").Lock()
_EMBED_MIN_INTERVAL = 2.2  # BGE-M3 endpoint caps ~30 req/min → serialize ≥2.2s apart
_embed_last = [0.0]


def embed(texts: list[str], model: str = "BGE-M3", batch: int = 32) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        with _EMBED_LOCK:  # global rate limit across worker threads
            wait = _EMBED_MIN_INTERVAL - (time.time() - _embed_last[0])
            if wait > 0:
                time.sleep(wait)
            d = _post("/embeddings", {"model": model, "input": chunk})
            _embed_last[0] = time.time()
        rows = sorted(d["data"], key=lambda x: x.get("index", 0))
        out.extend(r["embedding"] for r in rows)
    return out


def par_map(fn, items, workers: int = 8):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


# ---------------- math answer extraction + verification ----------------

def _extract_boxed(text: str) -> str | None:
    """Last \\boxed{...} with balanced braces."""
    idx = text.rfind("\\boxed")
    if idx < 0:
        return None
    i = text.find("{", idx)
    if i < 0:
        return None
    depth, j = 0, i
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j].strip()
        j += 1
    return None


def extract_answer(text: str) -> str | None:
    b = _extract_boxed(text)
    if b is not None:
        return b
    m = re.findall(r"(?:final answer|answer)\s*(?:is|:)?\s*\$?([^\n$]{1,60})", text, re.I)
    return m[-1].strip().rstrip(".") if m else None


def verify_answer(gold: str, pred: str | None) -> bool:
    """Use math_verify if available (robust symbolic check); else normalized string."""
    if pred is None:
        return False
    try:
        from math_verify import parse, verify  # type: ignore
        g = parse(f"${gold}$") if "\\" not in gold and "{" not in gold else parse(gold)
        p = parse(pred)
        if verify(g, p) or verify(parse(gold), parse(pred)):
            return True
    except Exception:  # noqa: BLE001 — fall back to string normalization
        pass
    return _norm(gold) == _norm(pred)


def _norm(s: str) -> str:
    s = s.strip().replace("\\left", "").replace("\\right", "").replace("$", "")
    s = s.replace("\\!", "").replace("\\,", "").replace(" ", "").replace("\\dfrac", "\\frac")
    s = re.sub(r"\\text\{[^}]*\}", "", s)
    s = s.rstrip(".").strip("{}")
    if re.fullmatch(r"-?\d+", s):
        return str(int(s))
    try:
        return str(int(float(s)))
    except Exception:  # noqa: BLE001
        return s.lower()


def metric(name: str, value, step: int = 0, **extra):
    rec = {"name": name, "step": step, "value": value}
    rec.update(extra)
    print("POLARIS_METRIC " + json.dumps(rec), flush=True)
