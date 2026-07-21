"""用户反馈 API schema。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

TYPE_PATTERN = r"^(bug|feature|task|ui|question|perf|other)$"
SEVERITY_PATTERN = r"^(blocker|high|normal|low)$"
STATUS_PATTERN = r"^(new|triaged|in_progress|resolved|closed|wontfix)$"


class FeedbackCreate(BaseModel):
    type: str = Field(default="bug", pattern=TYPE_PATTERN)
    severity: str = Field(default="normal", pattern=SEVERITY_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(default="", max_length=20000)
    # 前端自动带上；可选
    route: str | None = Field(default=None, max_length=255)
    context: dict[str, Any] | None = None


class FeedbackImageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    seq: int


class FeedbackAuthor(BaseModel):
    id: uuid.UUID
    display_name: str
    username: str | None


class FeedbackRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    severity: str
    title: str
    body: str
    route: str | None
    module: str | None
    context: dict[str, Any] | None
    status: str
    admin_note: str
    issue_draft: dict[str, Any] | None
    github_issue_number: int | None
    github_issue_url: str | None
    created_at: datetime
    images: list[FeedbackImageRead] = []
    author: FeedbackAuthor | None = None


class AdminFeedbackUpdate(BaseModel):
    status: str | None = Field(default=None, pattern=STATUS_PATTERN)
    severity: str | None = Field(default=None, pattern=SEVERITY_PATTERN)
    type: str | None = Field(default=None, pattern=TYPE_PATTERN)
    admin_note: str | None = Field(default=None, max_length=20000)


class IssueDraft(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=60000)
    labels: list[str] = []


class IssueCreateResult(BaseModel):
    number: int
    url: str
