"""只读账号（游客）：看得见管理端，写不动任何东西，也花不掉一个 token。"""

import ast
import pathlib

from tests.conftest import register_and_login

API_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "api"


async def _make_guest(client, admin_token: str) -> str:
    """建一个游客号并登录：role=admin 才看得见管理端，read_only 才写不动。"""
    resp = await client.post(
        "/api/admin/users",
        json={
            "email": "guest@example.com",
            "password": "gu3st-pass",
            "display_name": "Guest",
            "username": "guest",
            "role": "admin",
            "read_only": True,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["read_only"] is True
    login = await client.post(
        "/api/auth/jwt/login", data={"username": "guest", "password": "gu3st-pass"}
    )
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


async def test_guest_reads_admin_views_but_cannot_write(client):
    admin = await register_and_login(client)
    guest = await _make_guest(client, admin)
    gh = {"Authorization": f"Bearer {guest}"}

    # 看得见：管理员才能看的用户列表照常 200
    assert (await client.get("/api/admin/users", headers=gh)).status_code == 200
    me = await client.get("/api/users/me", headers=gh)
    assert me.status_code == 200
    assert me.json()["read_only"] is True  # 前端据此挂只读横幅

    # 写不动：四种写方法一律 403 READ_ONLY_ACCOUNT
    post = await client.post("/api/projects", json={"name": "X", "statement": "x"}, headers=gh)
    assert post.status_code == 403
    assert post.json()["detail"] == "READ_ONLY_ACCOUNT"

    patch = await client.patch("/api/users/me/username", json={"username": "zzz"}, headers=gh)
    assert patch.status_code == 403
    assert patch.json()["detail"] == "READ_ONLY_ACCOUNT"

    # 连管理端的写也一样挡住——游客不能靠 admin 身份给自己解开只读
    unlock = await client.patch(
        "/api/admin/users/00000000-0000-0000-0000-000000000001",
        json={"read_only": False},
        headers=gh,
    )
    assert unlock.status_code == 403
    assert unlock.json()["detail"] == "READ_ONLY_ACCOUNT"


async def test_guest_cannot_spend_a_token(client):
    """写入闸门之外再挡一层：LLM 守卫对只读账号直接拒，不看 llm_access 设成了什么。"""
    from app.api.auth import _check_llm_access
    from app.models.user import User

    guest = User(read_only=True, llm_access="full")
    for chat in (True, False):
        try:
            _check_llm_access(guest, chat=chat)
        except Exception as e:  # HTTPException
            assert getattr(e, "detail", None) == "LLM_ACCESS_BLOCKED"
        else:
            raise AssertionError("只读账号不该通过 LLM 守卫")


async def test_normal_user_is_unaffected(client):
    """闸门只认 read_only：普通用户照常写。"""
    admin = await register_and_login(client)
    resp = await client.post(
        "/api/projects",
        json={"name": "Normal", "statement": "still writable"},
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert resp.status_code in (200, 201), resp.text


def test_no_get_handler_writes_or_calls_the_model():
    """闸门把「不安全方法」当成「写」，这条前提必须一直成立。

    哪天有人在 GET 里 commit 或挂上 LLM 守卫，这个断言就会先响——否则游客会从
    那个洞里写进去，而闸门一无所知。
    """
    offenders = []
    for path in sorted(API_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            is_read = any(
                isinstance(f := (d.func if isinstance(d, ast.Call) else d), ast.Attribute)
                and f.attr in ("get", "head")
                for d in node.decorator_list
            )
            if not is_read:
                continue
            dumped = ast.dump(node)
            for marker in ("commit", "enqueue", "require_llm", "require_forge"):
                if marker in dumped:
                    offenders.append(f"{path.name}:{node.lineno} {node.name} -> {marker}")
    assert not offenders, "GET/HEAD 处理器里出现了写入或模型调用：" + "; ".join(offenders)
