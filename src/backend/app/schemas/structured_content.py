"""Public contracts for authorized structured paper content."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class StructuredContentAssetRead(BaseModel):
    kind: Literal["image", "table"]
    path: str
    media_type: str
    byte_size: int
    sha256: str
    url: str
    expires_at: datetime


class StructuredContentManifestRead(BaseModel):
    content_version_id: uuid.UUID
    paper_id: uuid.UUID
    asset_id: uuid.UUID
    version_no: int
    parser: str
    parser_version: str | None
    parse_status: str
    page_count: int
    chunk_count: int
    document_vector_state: str
    chunk_vector_state: str
    content_format: Literal["mineru_markdown", "plain_text", "unavailable"]
    content_hash: str | None
    markdown_url: str | None
    text_url: str | None
    assets: list[StructuredContentAssetRead]
    urls_expire_at: datetime | None
