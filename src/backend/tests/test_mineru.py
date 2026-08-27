"""MinerU Cloud upload/poll normalization contract tests."""

from pathlib import Path

import pytest

from app.core.config import get_settings
from app.services.mineru import MineruCloudParser


class FakeMineruClient:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def request(self, method, url, **_kwargs):
        self.calls.append((method, url))
        if method == "POST":
            return FakeResponse(
                json_data={"data": {"batch_id": "batch-1", "file_urls": [{"url": "https://upload"}]}},
            )
        return FakeResponse(
            json_data={
                "data": {
                    "extract_result_list": [
                        {"state": "done", "markdown_url": "https://markdown", "page_count": 2}
                    ]
                }
            },
        )

    async def put(self, url, **_kwargs):
        self.calls.append(("PUT", url))
        return FakeResponse()

    async def get(self, url, **_kwargs):
        self.calls.append(("GET", url))
        return FakeResponse(text="# Heading\n\nFull text from MinerU")


class FakeResponse:
    def __init__(self, *, json_data=None, text=""):
        self._json_data = json_data
        self.text = text
        self.content = text.encode("utf-8")

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_data


@pytest.mark.asyncio
async def test_mineru_cloud_upload_poll_and_normalize(tmp_path: Path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mineru_api_tokens", "key-a,key-b", raising=False)
    monkeypatch.setattr(settings, "mineru_poll_interval_seconds", 0.01, raising=False)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    client = FakeMineruClient()
    statuses: list[str] = []

    async def on_status(value: str) -> None:
        statuses.append(value)

    parser = MineruCloudParser(client=client)
    result = await parser.parse(pdf, on_status=on_status)

    assert result["parser"] == "mineru"
    assert result["pages"] == 2
    assert result["chunks"]
    assert statuses == ["mineru_uploading", "mineru_parsing"]
    assert [method for method, _url in client.calls] == ["POST", "PUT", "GET", "GET"]
