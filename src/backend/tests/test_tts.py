"""TTS settings, authenticated synthesis proxy, normalization, and cache."""

import httpx
import respx

from app.services.tts import text_for_speech
from tests.conftest import register_and_login


def _wav(payload: bytes = b"\x00\x00") -> bytes:
    size = 36 + len(payload)
    return (
        b"RIFF"
        + size.to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + (24_000).to_bytes(4, "little")
        + (48_000).to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data"
        + len(payload).to_bytes(4, "little")
        + payload
    )


async def _headers(client, email: str = "tts-admin@example.com") -> dict[str, str]:
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


def test_markdown_is_normalized_for_listening():
    source = """# 今日简报

- 阅读 **论文** [1]：[原文](https://example.org/paper)。
- `CosyVoice3` 很稳定。

```python
print('do not read me')
```
"""
    spoken = text_for_speech(source)
    assert "#" not in spoken
    assert "**" not in spoken
    assert "[1]" not in spoken
    assert "https://" not in spoken
    assert "print" not in spoken
    assert "阅读 论文 ：原文。" in spoken
    assert "代码块已略过" in spoken


async def test_tts_configuration_is_database_backed(client, monkeypatch):
    monkeypatch.setenv("POLARIS_TTS_ENABLED", "true")
    monkeypatch.setenv("POLARIS_TTS_BASE_URL", "http://ignored.example/v1")
    monkeypatch.setenv("POLARIS_TTS_MODEL", "ignored-model")
    admin = await _headers(client)

    response = await client.get("/api/admin/settings/tts", headers=admin)

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["base_url"] == "http://host.docker.internal:50000/v1"
    assert response.json()["model"] == "FunAudioLLM/Fun-CosyVoice3-0.5B-2512"


async def test_admin_and_personal_tts_settings(client):
    admin = await _headers(client)
    member = await _headers(client, "tts-member@example.com")

    denied = await client.get("/api/admin/settings/tts", headers=member)
    assert denied.status_code == 403

    payload = {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": "http://speech.test/v1/",
        "model": "FunAudioLLM/Fun-CosyVoice3-0.5B-2512",
        "default_voice": "default",
        "default_speed": 1.0,
        "max_chars": 8000,
    }
    saved = await client.put("/api/admin/settings/tts", headers=admin, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["base_url"] == "http://speech.test/v1"

    mine = await client.get("/api/tts/settings", headers=member)
    assert mine.status_code == 200
    assert mine.json()["available"] is True
    assert mine.json()["effective_model"] == payload["model"]

    updated = await client.put(
        "/api/tts/settings",
        headers=member,
        json={"enabled": True, "model": None, "voice": None, "speed": 1.25},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["effective_speed"] == 1.25

    invalid = await client.put(
        "/api/tts/settings",
        headers=member,
        json={"enabled": True, "model": "unapproved", "speed": 1.0},
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "INVALID_TTS_SETTING:model"


@respx.mock
async def test_admin_discovers_provider_voices(client):
    admin = await _headers(client)
    config = {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": "http://speech.test/v1",
        "model": "cosy-test",
        "default_voice": "default",
        "default_speed": 1.0,
        "max_chars": 2000,
    }
    respx.get("http://speech.test/v1/voices").mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "sample_rate": 24000,
                "data": [{"id": "default"}, {"id": "calm"}],
            },
        )
    )

    response = await client.post(
        "/api/admin/settings/tts/voices", headers=admin, json=config
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"voices": ["default", "calm"], "sample_rate": 24000}


async def test_invalid_admin_url_is_rejected(client):
    admin = await _headers(client)
    response = await client.put(
        "/api/admin/settings/tts",
        headers=admin,
        json={
            "enabled": True,
            "provider": "openai_compatible",
            "base_url": "file:///etc",
            "model": "model",
            "default_voice": "default",
            "default_speed": 1.0,
            "max_chars": 2000,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "INVALID_TTS_SETTING:base_url"


@respx.mock
async def test_speech_is_authenticated_and_cached(client):
    unauthenticated = await client.post(
        "/api/tts/speech", json={"text": "hello", "context": "assistant"}
    )
    assert unauthenticated.status_code == 401

    admin = await _headers(client)
    config = {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": "http://speech.test/v1",
        "model": "cosy-test",
        "default_voice": "default",
        "default_speed": 1.0,
        "max_chars": 2000,
    }
    assert (
        await client.put("/api/admin/settings/tts", headers=admin, json=config)
    ).status_code == 200
    upstream = respx.post("http://speech.test/v1/audio/speech").mock(
        return_value=httpx.Response(200, content=_wav())
    )

    for _ in range(2):
        response = await client.post(
            "/api/tts/speech",
            headers=admin,
            json={"text": "# Hello\n\n**world** [1]", "context": "assistant"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("audio/wav")
        assert response.content.startswith(b"RIFF")
    assert upstream.call_count == 1
    assert upstream.calls[0].request.read()
    body = upstream.calls[0].request.content.decode()
    assert "**" not in body
    assert "[1]" not in body


@respx.mock
async def test_speech_pcm_is_streamed_without_wav_buffering(client):
    admin = await _headers(client)
    config = {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": "http://speech.test/v1",
        "model": "cosy-test",
        "default_voice": "default",
        "default_speed": 1.25,
        "max_chars": 2000,
    }
    assert (
        await client.put("/api/admin/settings/tts", headers=admin, json=config)
    ).status_code == 200
    upstream = respx.post("http://speech.test/v1/audio/speech").mock(
        return_value=httpx.Response(
            200,
            content=b"\x00\x80\xff\x7f",
            headers={"X-Audio-Sample-Rate": "24000"},
        )
    )

    response = await client.post(
        "/api/tts/speech/stream",
        headers=admin,
        json={"text": "# Hello\n\n**stream** [1]", "context": "digest"},
    )

    assert response.status_code == 200, response.text
    assert response.content == b"\x00\x80\xff\x7f"
    assert response.headers["content-type"].startswith("audio/pcm")
    assert response.headers["x-audio-sample-rate"] == "24000"
    assert response.headers["x-audio-playback-rate"] == "1.25"
    request_body = upstream.calls[0].request.content.decode()
    assert '"response_format":"pcm"' in request_body
    assert '"speed":1.0' in request_body
    assert "**" not in request_body
    assert "[1]" not in request_body


async def test_disabled_tts_returns_service_unavailable(client):
    headers = await _headers(client)
    response = await client.post(
        "/api/tts/speech", headers=headers, json={"text": "hello", "context": "assistant"}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "TTS_DISABLED"
