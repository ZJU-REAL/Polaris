"""论文读接口的 fulltext：PDF、正文、抽取中——两套存储（论文行的路径与分段 vs PDF 资产
与内容版本）合成一个答案。"""

import uuid

from app.core.db import get_sessionmaker
from app.models.paper import PaperChunk
from app.models.paper_assets import PaperAsset, PdfBlob
from app.models.paper_content import PaperContentVersion
from app.services import paper_enrich
from tests.conftest import add_paper, make_project_with_library, register_and_login


async def test_paper_reads_carry_one_fulltext_state(client, monkeypatch):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id, library_id = await make_project_with_library(client, headers, name="ft")
    async with get_sessionmaker()() as session:
        bare = await add_paper(
            session, project_id=project_id, status="compiled", title="Bare", abstract=None
        )
        abstract_only = await add_paper(
            session,
            project_id=project_id,
            status="compiled",
            title="Abstract only",
            abstract="An abstract.",
        )
        on_row = await add_paper(
            session,
            project_id=project_id,
            status="compiled",
            title="Old path",
            abstract="a",
            pdf_path="/data/x.pdf",
            full_text_path="/data/x.txt",
        )
        # 只在新存储里有东西：一份就绪的 PDF 资产 + 一个还在解析的内容版本
        in_assets = await add_paper(
            session, project_id=project_id, status="compiled", title="Asset", abstract="a"
        )
        blob = PdfBlob(sha256="1" * 64, byte_size=1, storage_key="blobs/1")
        session.add(blob)
        await session.flush()
        asset = PaperAsset(paper_id=in_assets.id, blob_id=blob.id, source="upload", state="ready")
        session.add(asset)
        await session.flush()
        session.add(
            PaperContentVersion(
                paper_id=in_assets.id, asset_id=asset.id, version_no=1, parser="x", status="parsing"
            )
        )
        # 正文只以全文分段的形式存在
        chunked = await add_paper(
            session, project_id=project_id, status="compiled", title="Chunks", abstract=None
        )
        session.add(PaperChunk(paper_id=chunked.id, seq=0, text="body", source="fulltext"))
        await session.commit()
        ids = {
            "bare": bare.id,
            "abstract": abstract_only.id,
            "row": on_row.id,
            "asset": in_assets.id,
            "chunks": chunked.id,
        }
    # 后台补全正在跑的那一篇
    monkeypatch.setattr(paper_enrich, "_ENRICHING", {ids["abstract"]: 1})

    resp = await client.get(f"/api/libraries/{library_id}/papers?size=50", headers=headers)
    assert resp.status_code == 200, resp.text
    states = {uuid.UUID(p["id"]): p["fulltext"] for p in resp.json()["items"]}
    assert states[ids["bare"]] == {"pdf": False, "text": "none", "extracting": False}
    assert states[ids["abstract"]] == {"pdf": False, "text": "abstract", "extracting": True}
    assert states[ids["row"]] == {"pdf": True, "text": "full", "extracting": False}
    assert states[ids["asset"]] == {"pdf": True, "text": "abstract", "extracting": True}
    assert states[ids["chunks"]] == {"pdf": False, "text": "full", "extracting": False}

    detail = await client.get(f"/api/papers/{ids['asset']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["fulltext"] == {"pdf": True, "text": "abstract", "extracting": True}
