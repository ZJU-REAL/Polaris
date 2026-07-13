from tests.conftest import register_and_login


async def test_projects_require_auth(client):
    resp = await client.get("/api/projects")
    assert resp.status_code == 401


async def test_create_and_list_projects(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/projects",
        json={"name": "LLM 推理加速", "definition": {"topic": "inference"}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    project = resp.json()
    assert project["name"] == "LLM 推理加速"
    assert project["slug"]  # 自动生成
    assert project["status"] == "active"
    assert project["definition"] == {"topic": "inference"}

    resp = await client.post(
        "/api/projects", json={"name": "Second", "slug": "second"}, headers=headers
    )
    assert resp.status_code == 201
    assert resp.json()["slug"] == "second"

    resp = await client.get("/api/projects", headers=headers)
    assert resp.status_code == 200
    slugs = {p["slug"] for p in resp.json()}
    assert {project["slug"], "second"} <= slugs

    # get by id
    resp = await client.get(f"/api/projects/{project['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == project["id"]


async def test_get_project_not_member_404(client):
    token_a = await register_and_login(client, email="a@example.com")
    resp = await client.post(
        "/api/projects",
        json={"name": "private"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    project_id = resp.json()["id"]

    token_b = await register_and_login(client, email="b@example.com")
    resp = await client.get(
        f"/api/projects/{project_id}", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404

    # 非成员列表为空
    resp = await client.get("/api/projects", headers={"Authorization": f"Bearer {token_b}"})
    assert resp.json() == []
