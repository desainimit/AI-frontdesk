"""Baseline schema.

Revision ID: 0001_baseline
Revises:
"""

from collections.abc import Iterator, Sequence
from pathlib import Path

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = Path(__file__).with_suffix(".sql")
TABLES = (
    "api_credentials",
    "appointments",
    "audit_events",
    "auth_challenges",
    "auth_mail",
    "availability",
    "business_directory_mutations",
    "business_holidays",
    "business_hours_policies",
    "business_opening_windows",
    "business_resources",
    "business_service_resources",
    "business_service_staff",
    "business_services",
    "business_staff",
    "business_staff_holidays",
    "business_staff_windows",
    "call_transcripts",
    "calls",
    "configuration_drafts",
    "configuration_events",
    "configuration_resources",
    "document_chunks",
    "document_versions",
    "documents",
    "follow_up_events",
    "human_accounts",
    "human_identities",
    "human_sessions",
    "idempotency_records",
    "identity_memberships",
    "identity_sessions",
    "integration_credential_versions",
    "knowledge_jobs",
    "locations",
    "outbox_events",
    "patients",
    "phone_numbers",
    "platform_audit_events",
    "platform_authenticators",
    "platform_sessions",
    "platform_users",
    "post_call_reports",
    "providers",
    "published_configurations",
    "role_permissions",
    "support_sessions",
    "tenant_integrations",
    "tenant_onboardings",
    "tenant_roles",
    "tenants",
    "users",
    "workflow_intents",
)
FUNCTIONS = ("index_accepted_membership", "reject_audit_mutation")


def statements(script: str) -> Iterator[str]:
    """Split a SQL script on top-level semicolons, keeping quoted text and $$ bodies."""
    start = index = 0
    quote: str | None = None
    while index < len(script):
        if quote is not None:
            if script.startswith(quote, index):
                index += len(quote)
                quote = None
            else:
                index += 1
            continue
        char = script[index]
        if char == "-" and script.startswith("--", index):
            index = script.find("\n", index)
            index = len(script) if index == -1 else index
        elif char == "'":
            quote, index = "'", index + 1  # '' inside a string closes and reopens it.
        elif char == "$":
            end = script.find("$", index + 1)
            tag = script[index : end + 1]
            if end != -1 and (tag == "$$" or tag[1:-1].isidentifier()):
                quote, index = tag, end + 1
            else:
                index += 1
        elif char == ";":
            statement = script[start:index].strip()
            if statement and not all(line.startswith("--") for line in statement.splitlines()):
                yield statement
            start = index = index + 1
        else:
            index += 1
    if script[start:].strip():
        raise ValueError("The schema script ends without a semicolon")


def upgrade() -> None:
    for statement in statements(SCHEMA.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    for function in FUNCTIONS:
        op.execute(f'DROP FUNCTION IF EXISTS "{function}"() CASCADE')
