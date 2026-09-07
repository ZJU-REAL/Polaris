"""fmu 后端的子进程 worker：``python -m app.services.runners.fmu_worker <workdir>``。

为什么单独一个进程入口（而不是在 API 进程里调 fmpy）：fmpy.simulate_fmu 是同步
长跑调用，且真正干活的是 FMU 里的模型二进制——它段错误/死循环都不能殃及 API。
本模块因此刻意做成**自包含脚本**：除标准库外只 import fmpy，不碰 app 的任何配置
/DB/事件循环，崩了就崩在自己进程里，终局一律落盘说话。

与父进程的全部通信走文件（poll/collect 只读文件，天然幂等、跨进程恢复友好）：
- status.json：running → succeeded/failed 的状态机，os.replace 原子落盘，
  读端永远不会看到半截 JSON；failed 时附 error 文本供修复循环消费；
- result.csv：time 列 + 每个 output_variable 一列（numpy 结构化数组逐行写出）；
- stdout/stderr：由父进程 launch 时重定向进 run.log。

安全铁律：本脚本只认 workdir 里的 model.fmu 与 sim.json，sim.json 经 JSON 解析后
按白名单键取值传给 fmpy API 关键字参数——LLM 产出的文本没有任何机会进入命令行。
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path


def _write_status(workdir: Path, payload: dict) -> None:
    """原子落盘 status.json（tmp + os.replace：poll 并发读也读不到半截文件）。"""
    tmp = workdir / "status.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, workdir / "status.json")


def _fail(workdir: Path, started_at: float, error: str) -> int:
    _write_status(
        workdir,
        {
            "state": "failed",
            "error": error,
            "pid": os.getpid(),
            "started_at": started_at,
            "finished_at": time.time(),
        },
    )
    print(f"FMU worker failed: {error}", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m app.services.runners.fmu_worker <workdir>", file=sys.stderr)
        return 2
    workdir = Path(argv[0])
    started_at = time.time()
    _write_status(workdir, {"state": "running", "pid": os.getpid(), "started_at": started_at})

    try:
        spec = json.loads((workdir / "sim.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return _fail(workdir, started_at, f"sim.json 读取失败：{e}")
    if not isinstance(spec, dict):
        return _fail(workdir, started_at, "sim.json 顶层必须是 JSON 对象")

    try:
        import fmpy
    except ImportError:
        return _fail(workdir, started_at, "未安装 fmpy（pip install fmpy）")

    output_variables = [str(v) for v in spec.get("output_variables") or []]
    kwargs = {
        "stop_time": spec.get("stop_time"),
        "output": output_variables or None,
        "start_values": dict(spec.get("parameters") or {}),
    }
    if spec.get("step_size"):
        kwargs["output_interval"] = spec["step_size"]

    try:
        # 返回 numpy 结构化数组：dtype.names = ('time', <各 output 变量>)
        result = fmpy.simulate_fmu(str(workdir / "model.fmu"), **kwargs)
    except Exception as e:  # 模型/求解器抛什么都可能——错误文本是修复循环的输入，全量收
        return _fail(workdir, started_at, f"{type(e).__name__}: {e}")

    columns = list(result.dtype.names or [])
    with (workdir / "result.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for row in result:
            # 统一转 Python float 写出；NaN/Inf 原样落 csv（"nan"/"inf"）——
            # 丢不丢由 collect 决定，worker 不做主
            writer.writerow([float(v) for v in row])

    _write_status(
        workdir,
        {
            "state": "succeeded",
            "pid": os.getpid(),
            "rows": int(len(result)),
            "columns": columns,
            "started_at": started_at,
            "finished_at": time.time(),
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
