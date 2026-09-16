"""可选文献来源清单。

建库表单要先问「从哪里找文献」，可选项只能问注册表——写死一份名单的话，装一个源
不会自动可选、撤一个也不会自动消失。
"""

from tests.conftest import register_and_login


async def _auth(client, email):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def test_anonymous_cannot_list_sources(client):
    assert (await client.get("/api/literature-sources")).status_code == 401


async def test_every_searchable_source_is_listed(client):
    """名单来自能力探测，不是常量。"""
    from app.services.literature import sources as literature_sources

    headers = await _auth(client, "src1@example.com")
    rows = (await client.get("/api/literature-sources", headers=headers)).json()
    expected = {sid for sid, _ in literature_sources.sources_with_capability("search")}
    assert {r["id"] for r in rows} == expected
    assert len(rows) > 1, "只有一个源的话，这个选择器没有存在的意义"


async def test_sources_carry_a_hint_about_their_field(client):
    """"europepmc" 对一个做结构的人不构成任何提示——要能判断「这个源和我有没有关系」。"""
    headers = await _auth(client, "src2@example.com")
    rows = (await client.get("/api/literature-sources", headers=headers)).json()
    by_id = {r["id"]: r for r in rows}
    assert by_id["pubmed"]["title"] == "PubMed"
    assert "生物医学" in by_id["pubmed"]["description"]
    assert by_id["arxiv"]["description"]


async def test_only_arxiv_declares_category_support(client):
    """分类体系是 arXiv 独有的。表单据此决定要不要展示「arXiv 分类」那一项，
    而不是对所有人都摆出来——那正是这次改造要去掉的默认。"""
    headers = await _auth(client, "src3@example.com")
    rows = (await client.get("/api/literature-sources", headers=headers)).json()
    with_categories = [r["id"] for r in rows if r["supports_categories"]]
    assert with_categories == ["arxiv"]


async def test_order_follows_the_registry(client):
    """注册顺序即检索优先级；选择器里的顺序要和它一致。"""
    from app.services.literature import sources as literature_sources

    headers = await _auth(client, "src4@example.com")
    rows = (await client.get("/api/literature-sources", headers=headers)).json()
    expected = [sid for sid, _ in literature_sources.sources_with_capability("search")]
    assert [r["id"] for r in rows] == expected
