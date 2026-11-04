from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PipelineRun(Base):
    __tablename__ = "ai_pipeline_runs"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    inquiry_id: Mapped[str] = mapped_column(String(32))
    inquiry_revision: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    run_status: Mapped[str] = mapped_column(String(20))
    request_payload: Mapped[dict] = mapped_column(JSONB)
    result_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    callback_status: Mapped[str] = mapped_column(String(20), default="대기")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RagSource(Base):
    __tablename__ = "ai_rag_sources"
    __table_args__ = (UniqueConstraint("tenant_id", "source_type", "source_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(64))
    source_revision: Mapped[int] = mapped_column(Integer)
    indexing_status: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
