from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import ProspectStatus
from app.database import Base
from app.models._common import TimestampMixin

if TYPE_CHECKING:
    from app.models.message import OutreachMessage


class Prospect(TimestampMixin, Base):
    __tablename__ = "prospects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    job_title: Mapped[str | None] = mapped_column(String(120))
    company: Mapped[str | None] = mapped_column(String(120))
    source_url: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProspectStatus] = mapped_column(
        Enum(ProspectStatus, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e]),
        default=ProspectStatus.IMPORTED,
        nullable=False,
        index=True,
    )
    # Normalised identity used for duplicate detection (unique).
    dedupe_key: Mapped[str] = mapped_column(String(600), unique=True, nullable=False)
    research_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    messages: Mapped[list[OutreachMessage]] = relationship(
        back_populates="prospect", cascade="all, delete-orphan", order_by="OutreachMessage.id"
    )
