"""可选执行后端与流程包的清单（#674）。

四个后端早就注册了，创建实验时却一个也选不到——``params.backend`` 的注释写着
「接入 openfoam/ngspice 等后端后在此暴露」，而那个条件早就满足了。没有这个端点，
前端要么写死一份名单（用户自己写的流程包就永远看不见），要么干脆不做选择器。
"""

from tests.conftest import register_and_login


async def _auth(client, email="expback@example.com"):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def test_anonymous_cannot_list_backends(client):
    assert (await client.get("/api/experiment-backends")).status_code == 401


async def test_an_ordinary_user_can_list_backends(client):
    """挑后端的人就是建实验的人；知道装了哪些后端不涉及权限。"""
    await register_and_login(client, email="owner@example.com")
    headers = await _auth(client, "second@example.com")
    assert (await client.get("/api/experiment-backends", headers=headers)).status_code == 200


async def test_every_registered_backend_is_listed(client):
    """名单来自注册表。写死的话，装一个后端不会自动可选、撤一个也不会自动消失。"""
    from app.services.runners import registry

    headers = await _auth(client)
    rows = (await client.get("/api/experiment-backends", headers=headers)).json()
    assert {r["backend"] for r in rows} == set(registry.known_backends())
    # 今天已经不止 python-ml 了——这正是该有选择器的理由
    assert len(rows) >= 2


async def test_exactly_one_backend_is_marked_default(client):
    """不选时跑的就是它，等于全部存量实验的行为。两个或零个都会让界面无从表达。"""
    headers = await _auth(client)
    rows = (await client.get("/api/experiment-backends", headers=headers)).json()
    defaults = [r["backend"] for r in rows if r["is_default"]]
    assert defaults == ["python-ml"]


async def test_a_backend_declares_what_you_need_before_choosing_it(client):
    """选完才发现跑不起来，对一次动辄几十分钟的实验代价太大：凭据、License、
    副作用等级都要在选之前看得到。"""
    headers = await _auth(client)
    rows = (await client.get("/api/experiment-backends", headers=headers)).json()
    row = next(r for r in rows if r["backend"] == "python-ml")
    assert row["interaction"] in {"batch", "session", "streaming"}
    assert row["side_effects"] in {"none", "filesystem", "network", "physical"}
    # python-ml 跑在远端机器上，要 SSH 凭据；这件事必须在选之前就说
    assert "ssh" in row["credential_kinds"]
    assert row["licenses"] == []


async def test_listing_backends_has_no_side_effects(client):
    """manifest 由无参探针实例给出。列个清单要是会去连 SSH 或起容器，
    打开创建表单就会卡住。"""
    headers = await _auth(client)
    first = (await client.get("/api/experiment-backends", headers=headers)).json()
    second = (await client.get("/api/experiment-backends", headers=headers)).json()
    assert first == second


# ---- 流程包 ----


async def test_builtin_process_packs_are_listed(client):
    from app.services import process_packs

    headers = await _auth(client)
    rows = (
        await client.get("/api/experiment-backends/process-packs", headers=headers)
    ).json()
    assert {r["name"] for r in rows} == set(process_packs.known_pack_names())
    assert rows, "一个流程包都列不出来的话，选择器没有意义"


async def test_a_process_pack_shows_its_phases(client):
    """选包等于选一条流程，阶段名是唯一看得见的依据。"""
    headers = await _auth(client)
    rows = (
        await client.get("/api/experiment-backends/process-packs", headers=headers)
    ).json()
    row = next(r for r in rows if r["phases"])
    assert all(isinstance(p, str) and p for p in row["phases"])


async def test_a_user_written_pack_shows_up_without_a_restart(client, tmp_path, monkeypatch):
    """file-over-app：流程包是磁盘上的数据，丢进数据目录就该能选到。"""
    import yaml

    from app.core.config import get_settings

    headers = await _auth(client)
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path), raising=False)
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir(parents=True)
    (packs_dir / "mine.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "research-process",
                "name": "mine",
                "phases": [{"id": "prepare", "actions": []}],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    rows = (
        await client.get("/api/experiment-backends/process-packs", headers=headers)
    ).json()
    names = {r["name"] for r in rows}
    assert "mine" in names, "现扫才有意义：丢进目录就该立刻能选到"


async def test_a_broken_user_pack_does_not_break_the_listing(client, tmp_path, monkeypatch):
    """手写的包写坏了是常态。一个打错的缩进不该让所有人都选不了流程。"""
    from app.core.config import get_settings

    headers = await _auth(client)
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path), raising=False)
    packs_dir = tmp_path / "packs"
    packs_dir.mkdir(parents=True)
    (packs_dir / "broken.yaml").write_text("kind: [not a mapping", encoding="utf-8")

    resp = await client.get("/api/experiment-backends/process-packs", headers=headers)
    assert resp.status_code == 200, resp.text
    assert {r["name"] for r in resp.json()}, "坏包不该把内置包一起带走"
