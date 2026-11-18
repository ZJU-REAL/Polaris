"""会话签名密钥（core/session_key.py）：没配时生成并沿用、配短了不崩、默认配置下没有长度告警。

背景：默认 secret_key 只有 29 字节，PyJWT 每次验签都发 InsecureKeyLengthWarning；
进程里有别的线程把告警临时升级成异常时，正在验签的请求就 500 了。
"""

import stat
import threading
import warnings

import pytest
from fastapi_users.jwt import decode_jwt, generate_jwt
from jwt.warnings import InsecureKeyLengthWarning

from app.api.auth import get_jwt_strategy
from app.core import session_key
from app.core.config import LEGACY_DEFAULT_SECRET_KEY, get_settings

AUDIENCE = ["fastapi-users:auth"]


@pytest.fixture(autouse=True)
def _fresh_key_cache():
    session_key.reset_session_signing_key_cache()
    yield
    session_key.reset_session_signing_key_cache()


@pytest.fixture
def unconfigured(monkeypatch, tmp_path):
    """没人传 POLARIS_SECRET_KEY 的引擎：配置值就是公开默认值，数据目录是空的。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "secret_key", LEGACY_DEFAULT_SECRET_KEY, raising=False)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    return tmp_path


def _restart() -> None:
    """模拟引擎重启：进程里记着的密钥没了，只剩盘上的文件。"""
    session_key.reset_session_signing_key_cache()


# ---- 生成与沿用 ----


def test_key_is_generated_once_and_reused(tmp_path):
    first = session_key.load_or_create_key(tmp_path)
    path = tmp_path / session_key.KEY_FILE_NAME

    assert len(first.encode()) >= session_key.MIN_KEY_BYTES
    assert path.read_text().strip() == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [session_key.KEY_FILE_NAME]  # 没留临时文件

    assert session_key.load_or_create_key(tmp_path) == first
    assert session_key.load_or_create_key(tmp_path) == first


def test_each_data_dir_gets_its_own_key(tmp_path):
    a = session_key.load_or_create_key(tmp_path / "a")
    b = session_key.load_or_create_key(tmp_path / "b")
    assert a != b


def test_unusable_key_file_is_replaced(tmp_path):
    """文件被截断/清空：不拿半截内容当密钥，重新生成一把够长的。"""
    path = tmp_path / session_key.KEY_FILE_NAME
    path.write_text("short\n")
    key = session_key.load_or_create_key(tmp_path)
    assert len(key.encode()) >= session_key.MIN_KEY_BYTES
    assert path.read_text().strip() == key
    assert session_key.load_or_create_key(tmp_path) == key


def test_unwritable_data_dir_does_not_stop_the_engine(tmp_path):
    """数据目录写不进去：给一把本进程内有效的密钥，而不是让引擎起不来。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    key = session_key.load_or_create_key(blocker / "data")
    assert len(key.encode()) >= session_key.MIN_KEY_BYTES


# ---- 来源优先级 ----


def test_unconfigured_engine_signs_with_the_generated_key(unconfigured):
    key = session_key.session_signing_key()
    assert key != LEGACY_DEFAULT_SECRET_KEY
    assert key == (unconfigured / session_key.KEY_FILE_NAME).read_text().strip()
    _restart()
    assert session_key.session_signing_key() == key


def test_configured_key_wins_and_no_file_is_written(monkeypatch, tmp_path):
    settings = get_settings()
    configured = "configured-" + "k" * 40
    monkeypatch.setattr(settings, "secret_key", configured, raising=False)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)
    assert session_key.session_signing_key() == configured
    assert list(tmp_path.iterdir()) == []


def test_short_configured_key_is_stretched_not_rejected(monkeypatch, tmp_path):
    """配了但短于 32 字节：照常启动，签名密钥由它确定性派生（仍然是配置说了算）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "secret_key", "too-short", raising=False)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path), raising=False)

    key = session_key.session_signing_key()
    assert key != "too-short"
    assert len(key.encode()) >= session_key.MIN_KEY_BYTES
    _restart()
    assert session_key.session_signing_key() == key  # 重启后同一把
    assert list(tmp_path.iterdir()) == []  # 不落文件：密钥来自配置

    monkeypatch.setattr(settings, "secret_key", "also-short", raising=False)
    assert session_key.session_signing_key() != key  # 换配置就换密钥


# ---- 会话跨重启有效 ----


async def test_session_survives_a_restart(client, unconfigured):
    resp = await client.post("/api/auth/local-session")
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    assert (await client.get("/api/users/me", headers=headers)).status_code == 200

    _restart()
    assert (await client.get("/api/users/me", headers=headers)).status_code == 200

    # 反过来也成立：密钥文件没了就是另一把密钥，旧会话不再被接受
    (unconfigured / session_key.KEY_FILE_NAME).unlink()
    _restart()
    assert (await client.get("/api/users/me", headers=headers)).status_code == 401


# ---- 没有长度告警 ----


async def test_default_configuration_raises_no_key_length_warning(client, unconfigured):
    """默认配置下签发、校验会话都不发 InsecureKeyLengthWarning——升级成异常也不会 500。"""
    with warnings.catch_warnings():
        warnings.simplefilter("error", InsecureKeyLengthWarning)
        resp = await client.post("/api/auth/local-session")
        assert resp.status_code == 200, resp.text
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        for _ in range(3):
            assert (await client.get("/api/users/me", headers=headers)).status_code == 200


async def test_short_configured_key_raises_no_key_length_warning(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "secret_key", "per-install-secret-0123456789")
    with warnings.catch_warnings():
        warnings.simplefilter("error", InsecureKeyLengthWarning)
        resp = await client.post("/api/auth/local-session")
        assert resp.status_code == 200, resp.text
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        assert (await client.get("/api/users/me", headers=headers)).status_code == 200


def test_jwt_round_trip_survives_warnings_as_errors(unconfigured):
    """整个进程的告警都被当成异常时（过滤器是进程级的，别的线程一设这里就生效），
    用生成的密钥签名、验签照样成功。"""
    strategy = get_jwt_strategy()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        token = generate_jwt({"sub": "someone", "aud": AUDIENCE}, strategy.encode_key, 60)
        claims = decode_jwt(token, strategy.decode_key, AUDIENCE)
    assert claims["sub"] == "someone"


def test_jwt_verifies_while_another_thread_escalates_warnings(unconfigured):
    """复现线上的时序：一个工作线程反复进出「告警即异常」，主线程同时验签。"""
    strategy = get_jwt_strategy()
    token = generate_jwt({"sub": "someone", "aud": AUDIENCE}, strategy.encode_key, 60)
    stop = threading.Event()

    def escalate() -> None:
        while not stop.is_set():
            with warnings.catch_warnings():
                warnings.simplefilter("error")

    worker = threading.Thread(target=escalate)
    worker.start()
    try:
        for _ in range(2000):
            assert decode_jwt(token, strategy.decode_key, AUDIENCE)["sub"] == "someone"
    finally:
        stop.set()
        worker.join()
