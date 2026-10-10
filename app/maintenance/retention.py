"""Per-tenant post-call retention; dry run unless --apply is supplied."""

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database.rls import set_tenant_scope
from app.core.database.session import Database
from app.core.settings import Settings
from app.events.store import verify_worker_role
from app.postcall.models import CallTranscript, FollowUpEvent, PostCallReport
from app.settings.models import TenantSettings

# Transcripts removed per clinic in one pass; the next pass takes the rest.
SWEEP_BATCH = 500


def _pending_followup(tenant_id: UUID):
    return (
        select(FollowUpEvent.id)
        .where(
            FollowUpEvent.tenant_id == tenant_id,
            FollowUpEvent.call_id == PostCallReport.call_id,
            FollowUpEvent.status == "pending",
        )
        .exists()
    )


async def _delete_transcripts(
    session: AsyncSession, tenant_id: UUID, cutoff: datetime, limit: int
) -> int:
    ids = (
        await session.scalars(
            select(CallTranscript.id)
            .where(CallTranscript.tenant_id == tenant_id, CallTranscript.created_at < cutoff)
            .order_by(CallTranscript.created_at, CallTranscript.id)
            .limit(limit)
        )
    ).all()
    if ids:
        await session.execute(
            delete(CallTranscript).where(
                CallTranscript.tenant_id == tenant_id, CallTranscript.id.in_(ids)
            )
        )
    return len(ids)


async def _delete_reports(
    session: AsyncSession, tenant_id: UUID, cutoff: datetime, limit: int
) -> int:
    ids = (
        await session.scalars(
            select(PostCallReport.id)
            .where(
                PostCallReport.tenant_id == tenant_id,
                PostCallReport.call_ended_at < cutoff,
                ~_pending_followup(tenant_id),
            )
            .order_by(PostCallReport.call_ended_at, PostCallReport.id)
            .limit(limit)
        )
    ).all()
    if ids:
        await session.execute(
            delete(PostCallReport).where(
                PostCallReport.tenant_id == tenant_id, PostCallReport.id.in_(ids)
            )
        )
    return len(ids)


async def sweep_transcripts(sessions: async_sessionmaker[AsyncSession]) -> bool:
    """Delete call transcripts older than each clinic chose to keep them.

    Runs in the worker. Returns True when a clinic still has more to remove, so the
    worker comes straight back instead of waiting for the next hour.
    """
    async with sessions.begin() as session:
        await verify_worker_role(session)
        clinics = (
            await session.execute(
                select(
                    TenantSettings.tenant_id,
                    TenantSettings.document["privacy"]["transcript_retention_days"].as_integer(),
                )
            )
        ).all()
    now = datetime.now(UTC)
    more = False
    for tenant_id, days in clinics:
        if not days or days < 1:
            continue
        async with sessions.begin() as session:
            await set_tenant_scope(session, tenant_id)
            removed = await _delete_transcripts(
                session, tenant_id, now - timedelta(days=days), SWEEP_BATCH
            )
        more = more or removed == SWEEP_BATCH
    return more


async def run(
    settings: Settings,
    tenant_id: UUID,
    *,
    transcript_days: int = 30,
    report_days: int = 365,
    max_rows_per_table: int = 1000,
    apply: bool = False,
) -> tuple[int, int]:
    if not 1 <= transcript_days <= 3650 or not 30 <= report_days <= 3650:
        raise ValueError("Retention periods are out of range")
    if not 1 <= max_rows_per_table <= 10000:
        raise ValueError("Retention batch size is out of range")
    if settings.worker_database_url is None:
        raise ValueError("Retention requires WORKER_DATABASE_URL")
    database = Database(settings.model_copy(update={"database_url": settings.worker_database_url}))
    now = datetime.now(UTC)
    transcript_cutoff = now - timedelta(days=transcript_days)
    report_cutoff = now - timedelta(days=report_days)
    try:
        async with database.session_factory.begin() as session:
            await verify_worker_role(session)
            await set_tenant_scope(session, tenant_id)
            if not apply:
                transcript_count = await session.scalar(
                    select(func.count(CallTranscript.id)).where(
                        CallTranscript.tenant_id == tenant_id,
                        CallTranscript.created_at < transcript_cutoff,
                    )
                )
                report_count = await session.scalar(
                    select(func.count(PostCallReport.id)).where(
                        PostCallReport.tenant_id == tenant_id,
                        PostCallReport.call_ended_at < report_cutoff,
                        ~_pending_followup(tenant_id),
                    )
                )
                return int(transcript_count or 0), int(report_count or 0)
        counts = [0, 0]
        for index, (cutoff, delete_batch) in enumerate(
            (
                (transcript_cutoff, _delete_transcripts),
                (report_cutoff, _delete_reports),
            )
        ):
            while counts[index] < max_rows_per_table:
                async with database.session_factory.begin() as session:
                    await set_tenant_scope(session, tenant_id)
                    removed = await delete_batch(
                        session, tenant_id, cutoff, min(500, max_rows_per_table - counts[index])
                    )
                counts[index] += removed
                if removed == 0:
                    break
        return counts[0], counts[1]
    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--transcript-days", type=int, default=30)
    parser.add_argument("--report-days", type=int, default=365)
    parser.add_argument("--max-rows-per-table", type=int, default=1000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    counts = asyncio.run(
        run(
            Settings.from_environment(),
            args.tenant_id,
            transcript_days=args.transcript_days,
            report_days=args.report_days,
            max_rows_per_table=args.max_rows_per_table,
            apply=args.apply,
        )
    )
    label = "deleted" if args.apply else "eligible"
    print(f"{label}: transcripts={counts[0]} reports={counts[1]}")


if __name__ == "__main__":
    main()
