"""可选学科清单（#775）。

这个接口存在的唯一理由：学科包是磁盘上的数据，前端写死一份名单的话，用户照着文档
写了自己的包、扔进 ``<data_dir>/disciplines/``、然后在界面上找不到它——而那正是
file-over-app 要避免的事。所以用例重点在「用户目录里的包也能被列出来」。
"""

import yaml

from tests.conftest import register_and_login


def _pack(name: str) -> dict:
    return {
        "name": name,
        "title": f"{name} 学科",
        "description": "用户自己写的包",
        "schemas": [
            {
                "id": "method",
                "prompt": "抽取：\n{fields_spec}\n只输出 JSON。",
                "fields": [
                    {"name": "purpose", "kind": "text", "max_len": 200},
                    {"name": "mechanism", "kind": "text", "max_len": 200},
                ],
            }
        ],
    }


async def test_anonymous_cannot_list_disciplines(client):
    resp = await client.get("/api/disciplines")
    assert resp.status_code == 401


async def test_the_builtin_packs_are_listed(client):
    token = await register_and_login(client, email="disc@example.com")
    resp = await client.get(
        "/api/disciplines", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {row["name"] for row in body}
    assert "structural" in names
    row = next(r for r in body if r["name"] == "structural")
    # 选择器要展示的东西：名字之外还得有人看得懂的标题，以及它到底带来几条 schema
    assert row["title"]
    assert row["schema_count"] >= 1


async def test_a_pack_dropped_into_the_data_dir_shows_up_without_a_restart(
    client, tmp_path, monkeypatch
):
    from app.core.config import get_settings

    token = await register_and_login(client, email="disc2@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    before = await client.get("/api/disciplines", headers=headers)
    assert "mine" not in {r["name"] for r in before.json()}

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path), raising=False)
    user_dir = tmp_path / "disciplines"
    user_dir.mkdir(parents=True)
    (user_dir / "mine.yaml").write_text(
        yaml.safe_dump(_pack("mine"), allow_unicode=True), encoding="utf-8"
    )

    after = await client.get("/api/disciplines", headers=headers)
    names = {r["name"] for r in after.json()}
    assert "mine" in names, "现扫才有意义：丢进目录就该立刻能选到"
    # 内置包仍在：用户目录是追加，不是替换
    assert "structural" in names


async def test_a_broken_pack_does_not_break_the_listing(client, tmp_path, monkeypatch):
    """手写的包写坏了是常态。列表要照常返回其余的，而不是 500——否则一个打错的
    缩进就让所有人都选不了学科。"""
    from app.core.config import get_settings

    token = await register_and_login(client, email="disc3@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path), raising=False)
    user_dir = tmp_path / "disciplines"
    user_dir.mkdir(parents=True)
    (user_dir / "broken.yaml").write_text("name: [not a mapping", encoding="utf-8")
    (user_dir / "good.yaml").write_text(
        yaml.safe_dump(_pack("good"), allow_unicode=True), encoding="utf-8"
    )

    resp = await client.get("/api/disciplines", headers=headers)
    assert resp.status_code == 200, resp.text
    names = {r["name"] for r in resp.json()}
    assert "good" in names
    assert "broken" not in names


async def test_the_listing_is_ordered_so_the_picker_is_stable(client):
    token = await register_and_login(client, email="disc4@example.com")
    resp = await client.get(
        "/api/disciplines", headers={"Authorization": f"Bearer {token}"}
    )
    titles = [r["title"] for r in resp.json()]
    assert titles == sorted(titles), "顺序不定的话，同一个下拉菜单每次打开都在跳"
