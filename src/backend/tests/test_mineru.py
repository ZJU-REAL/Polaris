"""MinerU Cloud upload/poll normalization contract tests."""

import io
import zipfile
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
    assert statuses == [
        "mineru_uploading",
        "mineru_parsing",
        "mineru_polling",
        "mineru_downloading_result",
    ]
    assert [method for method, _url in client.calls] == ["POST", "PUT", "GET", "GET"]


def test_zip_result_keeps_markdown_images_and_tables():
    from app.services.mineru import _zip_result

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("paper.md", "# Title\n\n![Figure](images/figure.png)\n\n| A | B |")
        bundle.writestr("images/figure.png", b"png-bytes")
        bundle.writestr("tables/data.csv", "a,b\n1,2")
    result = _zip_result(buffer.getvalue(), pages=3)

    assert result["pages"] == 3
    assert result["markdown_path"] == "paper.md"
    assert {item["path"] for item in result["artifacts"]} == {
        "images/figure.png",
        "tables/data.csv",
    }
    assert result["manifest"]["images"] == ["images/figure.png"]
    assert result["manifest"]["tables"] == ["tables/data.csv"]


def test_zip_result_ignores_archive_traversal_entries():
    from app.services.mineru import _zip_result

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("paper.md", "# Safe")
        bundle.writestr("../outside.png", b"not-written")

    result = _zip_result(buffer.getvalue())

    assert result["manifest"]["images"] == []


def test_zip_result_rejects_invalid_archive():
    from app.services.mineru import MineruCloudError, _zip_result

    with pytest.raises(MineruCloudError, match="MINERU_ARCHIVE_INVALID"):
        _zip_result(b"not-a-zip")


def test_zip_result_rejects_invalid_markdown_encoding():
    from app.services.mineru import MineruCloudError, _zip_result

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("paper.md", b"\xff\xfe")

    with pytest.raises(MineruCloudError, match="MINERU_MARKDOWN_ENCODING_INVALID"):
        _zip_result(buffer.getvalue())


def test_scheduler_rotates_configured_tokens():
    from app.services.mineru import _MineruScheduler

    scheduler = _MineruScheduler(["key-a", "key-b"], concurrency=2)

    assert [scheduler.next_token() for _ in range(4)] == [
        "key-a",
        "key-b",
        "key-a",
        "key-b",
    ]
