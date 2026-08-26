"""API contracts for the Polaris browser extension."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class DownloadApiKeyCreated(BaseModel):
    api_key: str
    key_prefix: str
    created_at: datetime


class DownloadTarget(BaseModel):
    library_id: uuid.UUID
    paper_id: uuid.UUID
    article_url: str | None = Field(default=None, max_length=4000)
    pdf_candidates: list[Any] | None = None


class DownloadBatchCreate(BaseModel):
    targets: list[DownloadTarget] = Field(min_length=1, max_length=500)


class DownloadBatchCreated(BaseModel):
    id: uuid.UUID
    status: str
    item_count: int


class DownloadItemRead(BaseModel):
    id: uuid.UUID
    batch_id: uuid.UUID
    library_id: uuid.UUID
    paper_id: uuid.UUID
    expected_identity: dict[str, Any]
    article_url: str | None
    pdf_candidates: list[Any] | None
    status: str
    lease_until: datetime | None
    lease_token: str | None = None
    attempt_count: int
    error: str | None = None
    result: dict[str, Any] | None = None


class DownloadBatchRead(BaseModel):
    id: uuid.UUID
    status: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    items: list[DownloadItemRead]


class DownloadHeartbeat(BaseModel):
    status: Literal["downloading", "uploading"] = "downloading"


class DownloadCacheAck(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    byte_size: int = Field(gt=0)


class DownloadResult(BaseModel):
    status: Literal["failed", "blocked", "cancelled"]
    error: str | None = Field(default=None, max_length=4000)
    evidence: dict[str, Any] | None = None


class DownloadArchiveMetadata(BaseModel):
    library_id: uuid.UUID
    paper_id: uuid.UUID
    nonce: str = Field(min_length=16, max_length=200)
    doi: str | None = Field(default=None, max_length=512)
    pmid: str | None = Field(default=None, max_length=64)
    pmcid: str | None = Field(default=None, max_length=64)
    arxiv_id: str | None = Field(default=None, max_length=128)
    title: str = Field(min_length=1, max_length=2000)
    source_url: str | None = Field(default=None, max_length=4000)


class DownloadArchiveResult(BaseModel):
    asset_id: uuid.UUID
    content_version_id: uuid.UUID
    status: str
