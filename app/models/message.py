from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import MessageStatus, Tone
from app.database import Base
from app.models._common import TimestampMixin

if TYPE_CHECKING:
    from app.models.prospect import Prospect


def _enum(e: type) -> Enum:
    return Enum(e, native_enum=False, length=32, values_callable=lambda x: [m.value for m in x])


class OutreachMessage(TimestampMixin, Base):
    __tablename__ = "outreach_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    tone: Mapped[Tone] = mapped_column(_enum(Tone), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    facts_used: Mapped[list[str]] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    generation_attempts: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[MessageStatus] = mapped_column(
        _enum(MessageStatus), default=MessageStatus.PENDING_APPROVAL, nullable=False
    )
    reviewer: Mapped[str | None] = mapped_column(String(120))
    review_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    prospect: Mapped[Prospect] = relationship(back_populates="messages")
