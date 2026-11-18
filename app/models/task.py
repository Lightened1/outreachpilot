from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TaskRow(Base):
    """Persisted form of ``app.core.tasks.TaskRecord`` (history kept as JSON)."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
