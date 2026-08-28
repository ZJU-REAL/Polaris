"""Issue #513: authorized structured-content manifests and signed resources."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import LibraryPaper
from app.models.paper_assets import AssetGrant
from app.models.user import User
from app.services.paper_assets import create_or_reuse_asset
from app.services.paper_content import create_content_version, parse_content_version
from app.services.structured_content import (
    InvalidStructuredContentToken,
    StructuredContentError,
    create_token,
    verify_token,
)
from tests.test_paper_assets import _library, _paper_in_library, _pdf_bytes, _user


async def _mineru_content(client):
    headers, user_id = await _user(client, f"structured-{uuid.uuid4().hex}@example.com")
    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        assert user is not None
        library = await _library(session, user_id=user_id)
        paper = await _paper_in_library(session, library)
        asset = await create_or_reuse_asset(
            session,
            paper=paper,
            library=library,
            content=_pdf_bytes("structured source"),
            user=user,
            source="upload",
            sharing_scope="private",
        )
        version = await create_content_version(session, asset=asset)

        async def fake_mineru(_path):
            return {
                "parser": "mineru",
                "parser_version": "cloud-test",
                "markdown": (
                    "# 结构化原文\n\n"
                    "![冲击响应图](images/figure.png)\n\n"
                    "[试验数据](tables/results.csv)\n\n"
                    '<img src="images/figure.png" alt="response">'
                ),
                "text": "结构化原文 冲击响应",
                "pages": 3,
                "chunks": [
                    {
                        "text": "结构化原文 冲击响应",
                        "page_start": 2,
                        "page_end": 2,
                    }
                ],
                "markdown_path": "output/paper.md",
                "artifacts": [
                    {
                        "path": "output/images/figure.png",
                        "kind": "image",
                        "content": b"test-png-content",
                    },
                    {
                        "path": "output/tables/results.csv",
                        "kind": "table",
                        "content": b"load,response\n10,2.5\n",
                    },
                ],
                "manifest": {
                    "pages": 3,
                    "images": ["output/images/figure.png"],
                    "tables": ["output/tables/results.csv"],
                },
            }

        await parse_content_version(session, version=version, mineru_parser=fake_mineru)
        await session.commit()
        return headers, user_id, library.id, paper.id, asset.id, version.id


async def _pymupdf_content(client):
    headers, user_id = await _user(client, f"fallback-{uuid.uuid4().hex}@example.com")
    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        assert user is not None
        library = await _library(session, user_id=user_id)
        paper = await _paper_in_library(session, library)
        asset = await create_or_reuse_asset(
            session,
            paper=paper,
            library=library,
            content=_pdf_bytes("fallback full text"),
            user=user,
            source="upload",
            sharing_scope="private",
        )
        version = await create_content_version(session, asset=asset)

        async def failing_mineru(_path):
            raise RuntimeError("cloud unavailable")

        await parse_content_version(session, version=version, mineru_parser=failing_mineru)
        await session.commit()
        return headers, library.id, paper.id, version.id


async def test_mineru_manifest_serves_utf8_markdown_and_signed_assets(app, client):
    headers, _user_id, library_id, paper_id, _asset_id, version_id = await _mineru_content(
        client
    )
    response = await client.get(
        f"/api/libraries/{library_id}/papers/{paper_id}/structured-content",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    manifest = response.json()
    assert manifest["content_version_id"] == str(version_id)
    assert manifest["parser"] == "mineru"
    assert manifest["parse_status"] == "ready"
    assert manifest["page_count"] == 3
    assert manifest["content_format"] == "mineru_markdown"
    assert len(manifest["content_hash"]) == 64
    assert len(manifest["assets"]) == 2
    assert "paper-content" not in json.dumps(manifest)

    markdown = await client.get(manifest["markdown_url"])
    assert markdown.status_code == 200, markdown.text
    assert markdown.encoding.lower() == "utf-8"
    assert "结构化原文" in markdown.text
    assert markdown.text.count("/api/structured-content-assets/") == 3
    assert "images/figure.png" not in markdown.text
    assert markdown.headers["cache-control"].startswith("private, max-age=")
    assert len(markdown.headers["x-content-sha256"]) == 64

    by_kind = {item["kind"]: item for item in manifest["assets"]}
    image = await client.get(by_kind["image"]["url"])
    table = await client.get(by_kind["table"]["url"])
    assert image.status_code == 200 and image.content == b"test-png-content"
    assert table.status_code == 200 and b"load,response" in table.content
    assert "immutable" in image.headers["cache-control"]
    assert image.headers["etag"] == f'"{by_kind["image"]["sha256"]}"'


async def test_pymupdf_is_exposed_as_plain_text_without_mineru_assets(app, client):
    headers, library_id, paper_id, version_id = await _pymupdf_content(client)
    response = await client.get(
        f"/api/libraries/{library_id}/papers/{paper_id}/structured-content",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    manifest = response.json()
    assert manifest["content_version_id"] == str(version_id)
    assert manifest["parser"] == "pymupdf"
    assert manifest["parse_status"] == "ready_fallback"
    assert manifest["content_format"] == "plain_text"
    assert manifest["markdown_url"] is None
    assert manifest["assets"] == []

    text = await client.get(manifest["text_url"])
    assert text.status_code == 200
    assert "fallback full text" in text.text
    assert text.headers["content-type"].startswith("text/plain")


async def test_cross_library_access_is_denied_and_revocation_invalidates_signed_url(app, client):
    headers, user_id, library_id, paper_id, asset_id, _version_id = await _mineru_content(client)
    response = await client.get(
        f"/api/libraries/{library_id}/papers/{paper_id}/structured-content",
        headers=headers,
    )
    assert response.status_code == 200
    signed_markdown_url = response.json()["markdown_url"]

    async with get_sessionmaker()() as session:
        other_library = await _library(session, user_id=user_id)
        session.add(
            LibraryPaper(
                library_id=other_library.id,
                paper_id=paper_id,
                status="included",
            )
        )
        await session.commit()
        other_library_id = other_library.id
    denied = await client.get(
        f"/api/libraries/{other_library_id}/papers/{paper_id}/structured-content",
        headers=headers,
    )
    assert denied.status_code == 404

    async with get_sessionmaker()() as session:
        grant = await session.scalar(
            select(AssetGrant).where(
                AssetGrant.asset_id == asset_id,
                AssetGrant.library_id == library_id,
            )
        )
        assert grant is not None
        grant.can_read = False
        await session.commit()
    revoked = await client.get(signed_markdown_url)
    assert revoked.status_code == 404


def test_structured_content_tokens_reject_tampering_and_expiry():
    values = [uuid.uuid4() for _ in range(4)]
    token, claims = create_token(
        user_id=values[0],
        library_id=values[1],
        paper_id=values[2],
        version_id=values[3],
        relative_path="output/paper.md",
        expires_at=101,
        now=100,
    )
    assert verify_token(token, now=100) == claims
    with pytest.raises(InvalidStructuredContentToken):
        verify_token(token, now=101)
    with pytest.raises(InvalidStructuredContentToken):
        verify_token(f"{token[:-1]}x", now=100)
    with pytest.raises(StructuredContentError, match="STRUCTURED_CONTENT_PATH_INVALID"):
        create_token(
            user_id=values[0],
            library_id=values[1],
            paper_id=values[2],
            version_id=values[3],
            relative_path="../private.pdf",
        )
