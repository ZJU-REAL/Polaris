"""论文稿件与其 LaTeX 文件。"""

import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class Manuscript(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "manuscripts"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    idea_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ideas.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)

    files: Mapped[list["ManuscriptFile"]] = relationship(
        back_populates="manuscript", cascade="all, delete-orphan"
    )


class ManuscriptFile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "manuscript_files"

    manuscript_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("manuscripts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    path: Mapped[str] = mapped_column(String(1024), nullable=False)  # e.g. main.tex
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)  # latex

    manuscript: Mapped[Manuscript] = relationship(back_populates="files")
