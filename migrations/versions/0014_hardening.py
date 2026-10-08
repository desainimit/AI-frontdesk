"""Index bounded tenant transcript retention scans."""

from collections.abc import Sequence

from alembic import op

revision = "0014_hardening"
down_revision = "0013_post_call"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_call_transcripts_tenant_created", "call_transcripts", ["tenant_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_call_transcripts_tenant_created", "call_transcripts")
