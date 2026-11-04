"""Create AI-owned metadata tables; no backend inquiry/chat tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_pipeline_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("inquiry_id", sa.String(32), nullable=False),
        sa.Column("inquiry_revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("run_status", sa.String(20), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("result_payload", postgresql.JSONB(), nullable=True),
        sa.Column("callback_status", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "idempotency_key"),
    )
    op.create_index("ix_ai_pipeline_runs_tenant_id", "ai_pipeline_runs", ["tenant_id"])
    op.create_table(
        "ai_rag_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(200), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("indexing_status", sa.String(20), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("tenant_id", "source_type", "source_id", "version"),
    )
    op.create_index("ix_ai_rag_sources_tenant_id", "ai_rag_sources", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("ai_rag_sources")
    op.drop_table("ai_pipeline_runs")
