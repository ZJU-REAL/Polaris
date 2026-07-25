"""邮箱验证码：注册验证、找回密码、冷却与限次。"""

import pytest

from app.core.config import get_settings
from tests.conftest import INVITE_CODE

EMAIL = "bob@example.com"


@pytest.fixture
def email_on():
    """打开邮件系统（Settings 是 lru_cache 单例，直接改字段再还原）。"""
    s = get_settings()
    before = (s.smtp_host, s.smtp_user, s.smtp_from)
    s.smtp_host, s.smtp_user, s.smtp_from = "smtp.test", "polaris@test", "polaris@test"
    yield
    s.smtp_host, s.smtp_user, s.smtp_from = before


@pytest.fixture
def sent(monkeypatch):
    """截获发信，返回 [(email, purpose, code), ...]。"""
    out: list[tuple[str, str, str]] = []

    async def fake_send(to, purpose, code, minutes):
        out.append((to, purpose, code))
        return True

    monkeypatch.setattr("app.services.email.send_verification_code", fake_send)
    return out


def _register_body(**overrides):
    body = {
        "email": EMAIL,
        "password": "str0ng-password",
        "display_name": "Bob",
        "username": "bob",
        "invite_code": INVITE_CODE,
    }
    body.update(overrides)
    return body


async def _get_code(client, sent, purpose, email=EMAIL):
    resp = await client.post("/api/auth/send-code", json={"email": email, "purpose": purpose})
    assert resp.status_code == 200, resp.text
    return sent[-1][2]


async def test_capabilities_follows_smtp_config(client, email_on):
    assert (await client.get("/api/auth/capabilities")).json() == {
        "email": True,
        "password_reset": True,
        "register_email_code": True,
    }


async def test_capabilities_off_without_smtp(client):
    body = (await client.get("/api/auth/capabilities")).json()
    assert body["email"] is False and body["password_reset"] is False


async def test_send_code_requires_email_configured(client):
    resp = await client.post(
        "/api/auth/send-code", json={"email": EMAIL, "purpose": "register"}
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "EMAIL_NOT_CONFIGURED"


async def test_register_requires_valid_email_code(client, email_on, sent):
    # 不带验证码
    resp = await client.post("/api/auth/register", json=_register_body())
    assert resp.status_code == 400
    assert resp.json()["detail"] == "INVALID_CODE"

    # 带错误验证码
    code = await _get_code(client, sent, "register")
    wrong = "000000" if code != "000000" else "111111"
    resp = await client.post("/api/auth/register", json=_register_body(email_code=wrong))
    assert resp.status_code == 400

    # 正确验证码
    resp = await client.post("/api/auth/register", json=_register_body(email_code=code))
    assert resp.status_code == 201, resp.text
    assert sent[0][1] == "register"


async def test_register_code_is_single_use(client, email_on, sent):
    code = await _get_code(client, sent, "register")
    assert (
        await client.post("/api/auth/register", json=_register_body(email_code=code))
    ).status_code == 201
    resp = await client.post(
        "/api/auth/register",
        json=_register_body(email="other@example.com", username="other", email_code=code),
    )
    assert resp.status_code == 400


async def test_send_code_rejects_registered_email(client, email_on, sent):
    code = await _get_code(client, sent, "register")
    await client.post("/api/auth/register", json=_register_body(email_code=code))

    resp = await client.post(
        "/api/auth/send-code", json={"email": EMAIL, "purpose": "register"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "REGISTER_USER_ALREADY_EXISTS"


async def test_send_code_cooldown(client, email_on, sent):
    assert (
        await client.post("/api/auth/send-code", json={"email": EMAIL, "purpose": "register"})
    ).json() == {"sent": True, "retry_after": 0}

    body = (
        await client.post("/api/auth/send-code", json={"email": EMAIL, "purpose": "register"})
    ).json()
    assert body["sent"] is False and 0 < body["retry_after"] <= 60
    assert len(sent) == 1  # 冷却期内不重复发信


async def test_reset_code_silent_for_unknown_email(client, email_on, sent):
    resp = await client.post(
        "/api/auth/send-code", json={"email": "nobody@example.com", "purpose": "reset"}
    )
    assert resp.json()["sent"] is True  # 不暴露邮箱是否注册过
    assert sent == []


async def test_reset_cooldown_identical_for_unknown_email(client, email_on, sent):
    """冷却行为必须与已注册邮箱一致，否则「第二次有没有冷却」就是账号枚举旁路。"""
    known = EMAIL
    code = await _get_code(client, sent, "register")
    await client.post("/api/auth/register", json=_register_body(email_code=code))

    async def second_call(email):
        await client.post("/api/auth/send-code", json={"email": email, "purpose": "reset"})
        r = await client.post("/api/auth/send-code", json={"email": email, "purpose": "reset"})
        return r.json()

    known_body = await second_call(known)
    unknown_body = await second_call("nobody@example.com")
    assert known_body["sent"] is False and known_body["retry_after"] > 0
    assert unknown_body["sent"] is False and unknown_body["retry_after"] > 0


async def test_reset_password_flow(client, email_on, sent):
    code = await _get_code(client, sent, "register")
    await client.post("/api/auth/register", json=_register_body(email_code=code))

    reset_code = await _get_code(client, sent, "reset")
    assert sent[-1][1] == "reset"

    # 错误验证码
    wrong = "000000" if reset_code != "000000" else "111111"
    resp = await client.post(
        "/api/auth/reset-password",
        json={"email": EMAIL, "code": wrong, "password": "n3w-password"},
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "INVALID_CODE"

    # 新密码也要过强度校验
    resp = await client.post(
        "/api/auth/reset-password",
        json={"email": EMAIL, "code": reset_code, "password": "alllettersonly"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "PASSWORD_NEEDS_LETTER_AND_DIGIT"

    resp = await client.post(
        "/api/auth/reset-password",
        json={"email": EMAIL, "code": reset_code, "password": "n3w-password"},
    )
    assert resp.status_code == 200, resp.text

    # 旧密码失效、新密码可登录
    assert (
        await client.post(
            "/api/auth/jwt/login", data={"username": EMAIL, "password": "str0ng-password"}
        )
    ).status_code == 400
    assert (
        await client.post(
            "/api/auth/jwt/login", data={"username": EMAIL, "password": "n3w-password"}
        )
    ).status_code == 200


async def test_reset_code_cannot_be_used_for_register(client, email_on, sent):
    """purpose 隔离：找回密码的码不能拿去注册，反之亦然。"""
    reg_code = await _get_code(client, sent, "register")
    await client.post("/api/auth/register", json=_register_body(email_code=reg_code))

    reset_code = await _get_code(client, sent, "reset")
    resp = await client.post(
        "/api/auth/register",
        json=_register_body(email="xavier@example.com", username="xavier", email_code=reset_code),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "INVALID_CODE"


async def test_code_attempts_are_capped(client, email_on, sent):
    code = await _get_code(client, sent, "register")
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        await client.post("/api/auth/register", json=_register_body(email_code=wrong))
    # 次数用尽后，正确的码也不再被接受
    resp = await client.post("/api/auth/register", json=_register_body(email_code=code))
    assert resp.status_code == 400
