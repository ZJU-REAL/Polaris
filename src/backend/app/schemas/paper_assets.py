"""PDF asset and grant API schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PaperAssetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    paper_id: uuid.UUID
    blob_id: uuid.UUID
    source: str
    source_locator: str | None
    identity_key: str | None
    identity_status: str
    sharing_scope: str
    state: str
    is_preferred: bool
    byte_size: int
    sha256: str
    created_at: datetime
    updated_at: datetime


class AssetGrantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    asset_id: uuid.UUID
    library_id: uuid.UUID
    status: str
    can_read: bool
    can_process: bool
    granted_by: uuid.UUID | None
    revoked_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class AssetReuseRequest(BaseModel):
    asset_id: uuid.UUID


class PaperAssetPage(BaseModel):
    items: list[PaperAssetRead]
    grants: list[AssetGrantRead]


class PaperContentVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    paper_id: uuid.UUID
    asset_id: uuid.UUID
    version_no: int
    parser: str
    parser_version: str | None
    status: str
    error_code: str | None
    error_detail: str | None
    attempt: int
    page_count: int
    chunk_count: int
    document_vector_state: str
    chunk_vector_state: str
    is_current: bool
    created_at: datetime
    updated_at: datetime


class PaperContentChunkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_version_id: uuid.UUID
    seq: int
    text: str
    page_start: int | None
    page_end: int | None
    rects: list | None
    section_path: list[str] | None
    anchor_meta: dict | None
