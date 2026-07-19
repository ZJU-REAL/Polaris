"""Main driver: fixed model × harness condition × problems → pass@1 accuracy.

Reproduces the structure of Meta-Harness Table 6 (retrieval-augmented math):
  conditions = no_retrieval (baseline) | bm25 | dense
For each (model, condition) we emit POLARIS_METRIC accuracy plus mean context
tokens, so the platform can compare baseline vs treatment.
"""
from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

from common import WORKDIR, chat, extract_answer, metric, verify_answer
from data import load_corpus, load_testset
from retrieval import BM25, Dense

SOLVE_INSTR = (
    "Solve the following competition mathematics problem. Think step by step, "
    "then give the final answer on the last line as \\boxed{ANSWER}.\n\n"
)


def build_prompt(problem: str, exemplars: list[dict]) -> str:
    parts = []
    if exemplars:
        parts.append(
            "Here are worked solutions to related problems. Use any relevant "
            "techniques, but solve the target problem from scratch.\n"
        )
        for i, ex in enumerate(exemplars, 1):
            parts.append(f"[Example {i}]\nProblem: {ex['problem']}\nSolution: {ex['solution']}\n")
        parts.append("\n---\n")
    parts.append(SOLVE_INSTR + f"Problem: {problem}")
    return "\n".join(parts)


def make_retriever(condition: str, corpus: list[dict], k: int, tag: str):
    if condition == "no_retrieval":
        return lambda q: []
    docs = [c["problem"] for c in corpus]
    if condition == "bm25":
        idx = BM25(docs)
    elif condition == "dense":
        idx = Dense(docs, tag=f"corpus_{len(corpus)}")  # 与模型无关，多模型复用
    else:
        raise ValueError(condition)
    return lambda q: [corpus[i] for i in idx.top(q, k)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--models", default="dashscope/qwen-turbo,dashscope/qwen-plus")
    ap.add_argument("--conditions", default="no_retrieval,bm25,dense")
    ap.add_argument("--n-problems", type=int, default=120)
    ap.add_argument("--corpus-size", type=int, default=12000)
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--level", type=int, default=5)
    a = ap.parse_args()

    if a.smoke:
        a.n_problems, a.corpus_size, a.samples, a.workers = 4, 200, 1, 4
        a.models = a.models.split(",")[0]
        a.conditions = "no_retrieval,bm25"

    models = [m.strip() for m in a.models.split(",") if m.strip()]
    conditions = [c.strip() for c in a.conditions.split(",") if c.strip()]

    testset = load_testset(level=a.level, limit=a.n_problems)
    corpus = load_corpus(a.corpus_size, testset) if conditions != ["no_retrieval"] else []
    print(f"[setup] test={len(testset)} corpus={len(corpus)} models={models} "
          f"conditions={conditions} samples={a.samples} k={a.k}", flush=True)

    # 每条件跑完即落盘（results_partial.json），进程崩溃/限流中断后可跳过已完成条件
    partial_path = WORKDIR / "results_partial.json"
    results: dict = json.loads(partial_path.read_text()) if partial_path.exists() else {}
    lock = threading.Lock()

    for model in models:
        for cond in conditions:
            if results.get(model, {}).get(cond):
                print(f"[skip] {model} {cond} 已完成（{results[model][cond]['acc']:.1f}%）", flush=True)
                metric(f"accuracy/{model}/{cond}", results[model][cond]["acc"], model=model, condition=cond)
                continue
            # 惰性构建检索器：某条件（如 dense 建索引）失败不影响其它条件
            try:
                print(f"[build] {model} {cond} 检索器…", flush=True)
                retr = make_retriever(cond, corpus, a.k, model.replace("/", "_"))
            except Exception as e:  # noqa: BLE001
                print(f"[skip-cond] {model} {cond} 检索器构建失败：{type(e).__name__}: {e}", flush=True)
                continue
            tasks = [(t, s) for t in testset for s in range(a.samples)]

            def solve(task):
                t, _s = task
                ex = retr(t["problem"])
                prompt = build_prompt(t["problem"], ex)
                try:
                    text, tok = chat(model, prompt, max_tokens=a.max_tokens)
                except Exception as e:  # noqa: BLE001 — one failure shouldn't sink the run
                    return {"ok": False, "tok": 0, "ctx": len(prompt), "err": str(e)[:80]}
                ok = verify_answer(t["answer"], extract_answer(text))
                return {"ok": ok, "tok": tok, "ctx": len(prompt)}

            from common import par_map
            recs = par_map(solve, tasks, workers=a.workers)
            n = len(recs)
            acc = 100.0 * sum(r["ok"] for r in recs) / max(1, n)
            ctx = sum(r["ctx"] for r in recs) / max(1, n)
            errs = sum(1 for r in recs if r.get("err"))
            with lock:
                results.setdefault(model, {})[cond] = {"acc": acc, "ctx_chars": ctx, "n": n, "errs": errs}
                partial_path.write_text(json.dumps(results, ensure_ascii=False))  # 崩溃可续
            metric(f"accuracy/{model}/{cond}", round(acc, 2), model=model, condition=cond)
            metric(f"ctx_chars/{model}/{cond}", round(ctx, 1), model=model, condition=cond)
            print(f"[result] {model} {cond}: acc={acc:.1f}% ctx~{ctx:.0f}chars n={n} errs={errs}",
                  flush=True)

    # aggregate: average accuracy across models per condition + delta over baseline
    agg = {}
    for cond in conditions:
        vals = [results[m][cond]["acc"] for m in models if cond in results.get(m, {})]
        agg[cond] = sum(vals) / len(vals) if vals else 0.0
    base = agg.get("no_retrieval")
    for cond in conditions:
        d = f" ({agg[cond]-base:+.1f} vs baseline)" if base is not None and cond != "no_retrieval" else ""
        metric(f"avg_accuracy/{cond}", round(agg[cond], 2), condition=cond)
        print(f"[AGG] {cond}: {agg[cond]:.1f}%{d}", flush=True)

    (WORKDIR / "results.json").write_text(json.dumps(
        {"results": results, "avg": agg, "config": vars(a)}, ensure_ascii=False, indent=2))
    print("[done] wrote results.json", flush=True)


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
