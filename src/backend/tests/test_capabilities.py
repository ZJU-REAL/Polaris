"""本机能力接口（GET /api/capabilities）。

以前「能不能编译」「有没有远程机器」只挂在研究详情里：没有研究就问不到，而且
latex 只认 tectonic——装了 TeX Live 却没装 tectonic 的机器被说成不能编译。
"""

import shutil

from app.services import latex_compile
from tests.conftest import register_and_login
from tests.test_ssh_credentials import PAYLOAD as CRED_PAYLOAD


def _only_on_path(monkeypatch, *present: str) -> None:
    """让 PATH 上只有这几个程序（别的一律找不到）。"""
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **kw: f"/usr/bin/{name}" if name in present else None
    )


async def _headers(client, email="caps@example.com") -> dict:
    return {"Authorization": f"Bearer {await register_and_login(client, email=email)}"}


async def test_anonymous_cannot_read_capabilities(client):
    assert (await client.get("/api/capabilities")).status_code == 401


async def test_capabilities_do_not_need_a_research_run(client, monkeypatch):
    """一次研究都没有的时候就要能问：写作页、实验页打开时靠它决定入口可不可用。"""
    _only_on_path(monkeypatch)
    headers = await _headers(client)
    resp = await client.get("/api/capabilities", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"latex": False, "remote_machine": False}

    created = await client.post("/api/ssh-credentials", json=CRED_PAYLOAD, headers=headers)
    assert created.status_code == 201, created.text
    resp = await client.get("/api/capabilities", headers=headers)
    assert resp.json() == {"latex": False, "remote_machine": True}


async def test_latex_is_available_with_tex_live_but_no_tectonic(client, monkeypatch):
    """只装了 TeX Live（latexmk + xelatex）也能编译，不能因为没有 tectonic 就说不行。"""
    _only_on_path(monkeypatch, "latexmk", "xelatex")
    headers = await _headers(client)
    assert (await client.get("/api/capabilities", headers=headers)).json()["latex"] is True


async def test_latex_is_available_with_tectonic_alone(client, monkeypatch):
    _only_on_path(monkeypatch, "tectonic")
    headers = await _headers(client)
    assert (await client.get("/api/capabilities", headers=headers)).json()["latex"] is True


async def test_an_engine_without_latexmk_does_not_count(client, monkeypatch):
    """pdflatex/xelatex 由 latexmk 驱动：只有引擎没有 latexmk，编译入口一样跑不起来。"""
    _only_on_path(monkeypatch, "xelatex", "pdflatex")
    headers = await _headers(client)
    assert (await client.get("/api/capabilities", headers=headers)).json()["latex"] is False


def test_compile_uses_whatever_engine_the_capability_promised(monkeypatch):
    """能力接口说能编译，编译入口就得真挑得出引擎：默认引擎（tectonic）不在时
    回退到可用的那个，而不是报「编译器未安装」。"""
    _only_on_path(monkeypatch, "latexmk", "xelatex")
    assert latex_compile.available_engines() == ["xelatex"]
    assert latex_compile._resolve_engine("tectonic") == ("xelatex", "/usr/bin/latexmk")
    # 请求的引擎可用就用它；两个都在时不可用的请求回退到 tectonic
    _only_on_path(monkeypatch, "tectonic", "latexmk", "xelatex")
    assert latex_compile._resolve_engine("xelatex") == ("xelatex", "/usr/bin/latexmk")
    assert latex_compile._resolve_engine("pdflatex") == ("tectonic", "/usr/bin/tectonic")
    _only_on_path(monkeypatch)
    assert latex_compile._resolve_engine("tectonic") is None
    assert latex_compile.latex_available() is False
