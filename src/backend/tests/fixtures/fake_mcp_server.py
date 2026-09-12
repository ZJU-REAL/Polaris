"""测试用的外部 MCP 服务器（stdio）。

刻意用官方 SDK 起一个**真的**服务器，而不是在测试里 mock 掉客户端：这条链路的
全部价值就是与真实 MCP 服务器互通，mock 掉协议等于什么都没验。

它还刻意做成**有状态**的（记住 set_shape 设的形状），用来证明会话是常驻的——
每次调用重连的话，状态就会丢，而 CAD→CAE 那类流水线正是靠这个状态活着。
"""

import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-cae")

_state: dict[str, object] = {}


@mcp.tool()
def set_shape(name: str, thickness: float) -> str:
    """记下当前模型的形状参数（模拟 CAD 里改一个尺寸）。"""
    _state["name"] = name
    _state["thickness"] = thickness
    return f"shape set: {name} t={thickness}"


@mcp.tool()
def current_shape() -> dict:
    """读回当前形状——会话若被重建，这里就会是空的。"""
    return dict(_state)


@mcp.tool()
def solve(load: float) -> dict:
    """假求解器：按厚度和载荷算一个位移，返回结构化结果。"""
    thickness = float(_state.get("thickness") or 1.0)
    return {"displacement": round(load / thickness, 4), "converged": True}


@mcp.tool()
def always_fails() -> str:
    """总是失败的工具：用来验证「工具自己报错」是数据而不是传输异常。"""
    raise RuntimeError("solver diverged")


if __name__ == "__main__":
    sys.exit(mcp.run("stdio"))
