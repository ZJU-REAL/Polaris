"""Issue #454: content-addressed PDF assets and explicit grants."""

import uuid

import pymupdf
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import new_paper
from app.models.paper_assets import AssetGrant, PaperAsset, PdfBlob
from app.models.user import User
from app.services.paper_assets import (
    AssetPermissionError,
    create_or_reuse_asset,
    grant_existing_asset,
    grant_public_asset_for_paper,
    readable_asset,
)
from tests.conftest import register_and_login


def _pdf_bytes(text: str = "asset test") -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


async def _user(client, email: str) -> tuple[dict[str, str], uuid.UUID]:
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    async with get_sessionmaker()() as session:
        user_id = (await session.scalar(select(User.id).where(User.email == email)))
    return headers, user_id


async def _library(session, *, user_id: uuid.UUID, public: bool = False) -> DirectionLibrary:
    library = DirectionLibrary(
        name=f"asset-{uuid.uuid4().hex[:8]}",
        statement="PDF asset test",
        status="active",
        is_public=public,
        submitted_by=user_id,
        created_by=user_id,
    )
    session.add(library)
    await session.flush()
    return library


async def _paper_in_library(session, library: DirectionLibrary):
    paper = new_paper(title="Asset paper", doi="10.1234/asset")
    session.add(paper)
    await session.flush()
    session.add(LibraryPaper(library_id=library.id, paper_id=paper.id, status="included"))
    await session.flush()
    return paper


async def test_asset_is_content_addressed_and_idempotent(app, client):
    _headers, user_id = await _user(client, "asset-owner@example.com")
    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        assert user is not None
        library = await _library(session, user_id=user_id)
        paper = await _paper_in_library(session, library)
        content = _pdf_bytes()
        first = await create_or_reuse_asset(
            session,
            paper=paper,
            library=library,
            content=content,
            user=user,
            source="upload",
            sharing_scope="library",
        )
        second = await create_or_reuse_asset(
            session,
            paper=paper,
            library=library,
            content=content,
            user=user,
            source="upload",
            sharing_scope="library",
        )
        assert first.id == second.id
        assert (await session.scalar(select(PdfBlob.sha256))) is not None
        assert await session.scalar(select(PaperAsset.id)) == first.id
        assert await session.scalar(select(AssetGrant.id)) is not None
        await session.commit()
        row = await readable_asset(session, asset_id=first.id, library_id=library.id)
        assert row is not None
        assert row[1].byte_size > 0


async def test_private_asset_cannot_be_granted_to_another_library(app, client):
    _headers, user_id = await _user(client, "asset-private@example.com")
    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        assert user is not None
        source_library = await _library(session, user_id=user_id)
        target_library = await _library(session, user_id=user_id)
        paper = await _paper_in_library(session, source_library)
        asset = await create_or_reuse_asset(
            session,
            paper=paper,
            library=source_library,
            content=_pdf_bytes("private"),
            user=user,
            source="upload",
            sharing_scope="private",
        )
        await session.commit()
        try:
            await grant_existing_asset(
                session, asset_id=asset.id, target_library=target_library, user=user
            )
        except AssetPermissionError as exc:
            assert str(exc) == "ASSET_NOT_SHAREABLE_ACROSS_LIBRARIES"
        else:
            raise AssertionError("private asset was granted to another library")


async def test_public_asset_can_be_granted_without_copying_blob(app, client):
    _headers, user_id = await _user(client, "asset-public@example.com")
    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        assert user is not None
        source_library = await _library(session, user_id=user_id, public=True)
        target_library = await _library(session, user_id=user_id)
        paper = await _paper_in_library(session, source_library)
        session.add(
            LibraryPaper(
                library_id=target_library.id, paper_id=paper.id, status="included"
            )
        )
        asset = await create_or_reuse_asset(
            session,
            paper=paper,
            library=source_library,
            content=_pdf_bytes("public"),
            user=user,
            source="oa",
            sharing_scope="public",
        )
        grant = await grant_existing_asset(
            session, asset_id=asset.id, target_library=target_library, user=user
        )
        await session.commit()
        assert grant.library_id == target_library.id
        blob_count = len(list((await session.execute(select(PdfBlob.id))).scalars()))
        assert blob_count == 1


async def test_public_asset_can_be_reused_by_paper_identity(app, client):
    _headers, user_id = await _user(client, "asset-public-identity@example.com")
    async with get_sessionmaker()() as session:
        user = await session.get(User, user_id)
        assert user is not None
        source_library = await _library(session, user_id=user_id, public=True)
        target_library = await _library(session, user_id=user_id)
        paper = await _paper_in_library(session, source_library)
        session.add(
            LibraryPaper(
                library_id=target_library.id,
                paper_id=paper.id,
                status="included",
            )
        )
        asset = await create_or_reuse_asset(
            session,
            paper=paper,
            library=source_library,
            content=_pdf_bytes("identity"),
            user=user,
            source="oa",
            identity_key="doi:10.1234/asset",
            sharing_scope="public",
        )
        await session.commit()
        grant = await grant_public_asset_for_paper(
            session, paper_id=paper.id, target_library=target_library, user=user
        )
        await session.commit()
        assert grant.asset_id == asset.id


async def test_asset_http_upload_list_download_and_identity_check(app, client):
    headers, user_id = await _user(client, "asset-http@example.com")
    async with get_sessionmaker()() as session:
        library = await _library(session, user_id=user_id)
        paper = await _paper_in_library(session, library)
        await session.commit()
        library_id = str(library.id)
        paper_id = str(paper.id)

    response = await client.post(
        f"/api/libraries/{library_id}/papers/{paper_id}/assets",
        files={"file": ("paper.pdf", _pdf_bytes("http"), "application/pdf")},
        data={"source": "upload", "sharing_scope": "private"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    asset = response.json()
    assert asset["source"] == "upload"
    assert len(asset["sha256"]) == 64

    response = await client.get(
        f"/api/libraries/{library_id}/papers/{paper_id}/assets", headers=headers
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1
    asset_id = response.json()["items"][0]["id"]

    response = await client.get(
        f"/api/libraries/{library_id}/papers/{paper_id}/assets/{asset_id}/download",
        headers=headers,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content.startswith(b"%PDF-")

    response = await client.post(
        f"/api/libraries/{library_id}/papers/{paper_id}/assets",
        files={"file": ("wrong.pdf", _pdf_bytes("wrong"), "application/pdf")},
        data={
            "source": "extension",
            "identity_key": "doi:10.9999/not-this-paper",
            "identity_status": "verified",
        },
        headers=headers,
    )
    assert response.status_code == 422
    assert "identity" in response.json()["detail"].lower()


async def test_asset_http_private_library_is_not_visible_to_stranger(app, client):
    owner_headers, owner_id = await _user(client, "asset-http-owner@example.com")
    stranger_headers, _ = await _user(client, "asset-http-stranger@example.com")
    async with get_sessionmaker()() as session:
        library = await _library(session, user_id=owner_id)
        paper = await _paper_in_library(session, library)
        await session.commit()
        library_id = str(library.id)
        paper_id = str(paper.id)
    response = await client.post(
        f"/api/libraries/{library_id}/papers/{paper_id}/assets",
        files={"file": ("paper.pdf", _pdf_bytes("private"), "application/pdf")},
        headers=owner_headers,
    )
    assert response.status_code == 201
    response = await client.get(
        f"/api/libraries/{library_id}/papers/{paper_id}/assets", headers=stranger_headers
    )
    assert response.status_code == 404
