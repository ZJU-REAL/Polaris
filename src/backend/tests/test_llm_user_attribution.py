"""每次模型调用都要说清「这是谁的」（#801 第 2 步）。

``user_id`` 同时决定两件事：走谁的模型配置（#803 起每人可以有自己那份），以及这笔
账记在谁头上。漏传的表现很安静——调用照样成功，只是永远走部署级配置、账也落不到人
头上。等到有人问「我配的 key 怎么没生效」，已经没有任何线索指向漏掉的那一处。

所以这条判据用静态扫描守着，而不是靠每次 review 记得看：新加一个调用点必须要么带上
user_id，要么在下面的豁免表里写清为什么不带。
"""

import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parent.parent
ROUTER_METHODS = {"complete", "stream", "embed", "rerank", "stream_events"}

#: 明确不带 user_id 的地方，以及理由。键是「文件:所在函数」。
#:
#: 加新条目前先确认它真的不属于某个人：绝大多数调用都是某个用户的任务引发的，
#: 哪怕它跑在后台队列里——发起人在 ``run.created_by``。
EXEMPT: dict[str, str] = {
    "app/services/embedding.py:adopt_routed_model": (
        "向量空间是整个部署共用的一件事（owner 在设置里换模型）；"
        "拿某个人的 key 去探维度等于让他替全平台买单"
    ),
    "app/services/llm_admin.py:probe_model": (
        "打的是直接构造出来的 provider 实例，不经过路由表——provider 的方法本来就没有"
        "这个参数；连通性探测也不记账"
    ),
}


def _router_calls() -> list[tuple[str, int, str]]:
    """所有疑似 LLM 调用点：(文件:函数, 行号, 方法名)。"""
    found: list[tuple[str, int, str]] = []
    roots = [BACKEND / "app", BACKEND / "worker"]
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "core/llm" in str(path):
                continue  # 路由器自己实现这些方法，不是调用方
            tree = ast.parse(path.read_text())
            rel = str(path.relative_to(BACKEND))
            # 先建一张「节点 → 所在函数名」的表
            owner: dict[ast.AST, str] = {}
            for fn in ast.walk(tree):
                if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    for child in ast.walk(fn):
                        owner.setdefault(child, fn.name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute) or func.attr not in ROUTER_METHODS:
                    continue
                recv = ast.unparse(func.value).lower()
                if "llm" not in recv and "router" not in recv:
                    continue
                kw = {k.arg for k in node.keywords if k.arg}
                if "user_id" in kw:
                    continue
                found.append((f"{rel}:{owner.get(node, '<module>')}", node.lineno, func.attr))
    return found


def test_every_model_call_says_whose_it_is():
    offenders = [
        f"{where} (第 {line} 行, .{method}())"
        for where, line, method in _router_calls()
        if where not in EXEMPT
    ]
    assert not offenders, (
        "这些模型调用没带 user_id：它们会永远走部署级配置、账也记不到人头上。\n"
        "要么把发起人传进去（后台任务里是 run.created_by），"
        "要么在 EXEMPT 里写清为什么不属于任何人。\n  " + "\n  ".join(offenders)
    )


def test_the_exemptions_are_still_real():
    """豁免表不能变成墓地：写在里面的地方，得确实还没带 user_id。

    某处后来补上了 user_id 而豁免条目没删，下一个人读到的就是一条假理由。
    """
    actual = {where for where, _line, _method in _router_calls()}
    stale = sorted(set(EXEMPT) - actual)
    assert not stale, f"这些豁免已经不成立（对应调用点已带上 user_id 或已不存在）：{stale}"
