"""Knowledge tables with database-enforced tenant isolation and vector search."""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_knowledge"
down_revision = "0008_tenant_languages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _identity() -> list[sa.Column[UUID] | sa.Column[datetime]]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "documents",
        *_identity(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("active_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.CheckConstraint("status IN ('draft', 'approved', 'archived')", name="document_status"),
        sa.CheckConstraint("active_version > 0", name="active_version_positive"),
    )
    op.create_table(
        "document_versions",
        *_identity(),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("storage_key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("error_code", sa.String(40)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"]
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_document_versions_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "document_id",
            "version",
            name="uq_document_versions_tenant_document_version",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed')", name="version_status"
        ),
    )
    op.create_table(
        "document_chunks",
        *_identity(),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("section", sa.String(300)),
        sa.Column("subsection", sa.String(300)),
        sa.Column("page_number", sa.Integer()),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embedding", VECTOR(1536), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("source_metadata", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"]
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
        ),
        sa.UniqueConstraint("tenant_id", "document_version_id", "ordinal"),
        sa.CheckConstraint("token_count > 0", name="chunk_token_count"),
    )
    op.create_index("ix_documents_tenant", "documents", ["tenant_id"])
    op.create_index("ix_document_versions_queue", "document_versions", ["status", "updated_at"])
    op.create_index("ix_document_chunks_tenant", "document_chunks", ["tenant_id"])
    op.create_index(
        "ix_document_chunks_embedding",
        "document_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_table(
        "knowledge_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("document_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("error_code", sa.String(40)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'delivered', 'dead')", name="knowledge_job_status"
        ),
    )
    op.create_index("ix_knowledge_jobs_claim", "knowledge_jobs", ["status", "available_at"])
    predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    for table in ("documents", "document_versions", "document_chunks", "knowledge_jobs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )


def downgrade() -> None:
    for table in ("knowledge_jobs", "document_chunks", "document_versions", "documents"):
        op.drop_table(table)
    # Vector may be shared by other schemas.
