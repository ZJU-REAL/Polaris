"""PDF asset and grant API schemas."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AssetSource = Literal["oa", "upload", "extension", "arxiv", "manual", "unknown"]
SharingScope = Literal["private", "library", "public"]


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


class AssetCreate(BaseModel):
    source: AssetSource = "upload"
    source_locator: str | None = Field(default=None, max_length=2048)
    identity_key: str | None = Field(default=None, max_length=512)
    identity_status: str = Field(default="verified", max_length=16)
    sharing_scope: SharingScope = "private"


class AssetReuseRequest(BaseModel):
    asset_id: uuid.UUID


class PaperAssetPage(BaseModel):
    items: list[PaperAssetRead]
    grants: list[AssetGrantRead]
