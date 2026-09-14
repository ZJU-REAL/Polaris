"""多源订阅的管理面（#778）。

``daily.subscriptions`` 这个键此前只有服务层能写——也就是说非 arXiv 的源即使能供
日更，用户也没有任何办法订上它。这条线上反复犯的就是这个错：能力做完了，入口没有，
于是看起来什么都没变。
"""

from tests.conftest import register_and_login


async def _owner(client):
    token = await register_and_login(client, email="dailysub@example.com")
    return {"Authorization": f"Bearer {token}"}


async def test_anonymous_cannot_read_subscriptions(client):
    resp = await client.get("/api/daily/subscriptions")
    assert resp.status_code == 401


async def test_a_second_user_cannot_change_subscriptions(client):
    await register_and_login(client, email="dailysub@example.com")
    second = await register_and_login(client, email="second@example.com")
    headers = {"Authorization": f"Bearer {second}"}
    # 读是所有人的事（池子是共享的），写是 owner 的事
    assert (await client.get("/api/daily/subscriptions", headers=headers)).status_code == 200
    resp = await client.put(
        "/api/daily/subscriptions",
        json={"subscriptions": [{"source": "pubmed", "terms": ["neuroscience"]}]},
        headers=headers,
    )
    assert resp.status_code == 403


async def test_available_sources_come_from_the_registry(client):
    """名单写死的话，装上一个能日更的源不会自动可订、撤掉也不会自动消失。"""
    headers = await _owner(client)
    body = (await client.get("/api/daily/subscriptions", headers=headers)).json()
    assert "arxiv" in body["available_sources"]
    assert "pubmed" in body["available_sources"]
    # 只会检索、不能日更的源不该出现在可订清单里
    assert "crossref" not in body["available_sources"]


async def test_a_non_arxiv_subscription_round_trips(client):
    headers = await _owner(client)
    resp = await client.put(
        "/api/daily/subscriptions",
        json={"subscriptions": [{"source": "pubmed", "terms": ["neuroscience", "glioma"]}]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    saved = resp.json()["subscriptions"]
    assert saved == [
        {"source": "pubmed", "terms": ["neuroscience", "glioma"], "supports_daily": True}
    ]
    # 回读一致：写进去的是新键，不该被 arXiv 那条旧键的读路径吃掉
    again = (await client.get("/api/daily/subscriptions", headers=headers)).json()
    assert again["subscriptions"] == saved


async def test_arxiv_and_another_source_coexist(client):
    """arXiv 走旧键（扁平分类列表，有 legacy 读路径），其余源走新键；两者必须并存。"""
    headers = await _owner(client)
    await client.put(
        "/api/daily/subscriptions",
        json={
            "subscriptions": [
                {"source": "arxiv", "terms": ["cs.AI"]},
                {"source": "pubmed", "terms": ["neuroscience"]},
            ]
        },
        headers=headers,
    )
    body = (await client.get("/api/daily/subscriptions", headers=headers)).json()
    by_source = {row["source"]: row["terms"] for row in body["subscriptions"]}
    assert by_source == {"arxiv": ["cs.AI"], "pubmed": ["neuroscience"]}
    # 旧端点看到的仍是原来的形状
    cats = (await client.get("/api/daily/categories", headers=headers)).json()
    assert cats["categories"] == ["cs.AI"]


async def test_subscribing_to_a_source_that_cannot_do_daily_is_flagged_not_refused(client):
    """源可能只是暂时没配密钥或被撤下。连订阅一起丢掉等于让人重填；就地标出来，
    界面据此提示，池子为什么空着才有线索。"""
    headers = await _owner(client)
    resp = await client.put(
        "/api/daily/subscriptions",
        json={"subscriptions": [{"source": "crossref", "terms": ["catalysis"]}]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()["subscriptions"][0]
    assert row["source"] == "crossref"
    assert row["supports_daily"] is False


async def test_an_invalid_arxiv_category_is_refused(client):
    """arXiv 的词是分类，格式固定；存一个错的等于订了个永远抓不到的分类。"""
    headers = await _owner(client)
    resp = await client.put(
        "/api/daily/subscriptions",
        json={"subscriptions": [{"source": "arxiv", "terms": ["not a category"]}]},
        headers=headers,
    )
    assert resp.status_code == 422
    assert "INVALID_CATEGORY" in resp.json()["detail"]


async def test_clearing_every_subscription_is_allowed(client):
    """取消全部订阅是合法意图（池子从此不进新论文），不是输入错误。"""
    headers = await _owner(client)
    await client.put(
        "/api/daily/subscriptions",
        json={"subscriptions": [{"source": "pubmed", "terms": ["neuroscience"]}]},
        headers=headers,
    )
    resp = await client.put(
        "/api/daily/subscriptions", json={"subscriptions": []}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["subscriptions"] == []
