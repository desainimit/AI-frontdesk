"""Normalized hours must follow draft writes, inheritance, rollback and forced RLS."""

from datetime import date, time
from uuid import uuid4

from sqlalchemy import select

from app.configuration.hours import BusinessHoliday, BusinessOpeningWindow, HoursPolicy
from app.configuration.schemas import (
    HoursSettings,
    LocationOverride,
    OpeningWindow,
    SaveConfiguration,
)
from app.configuration.service import ConfigurationService
from app.core.database.rls import set_tenant_scope
from app.locations.models import Location
from tests.integration.test_saas import authenticate
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_hours_persistence_publication_rollback_and_inheritance(
    tenancy: TenancyFixture,
) -> None:
    context = await authenticate(tenancy)
    configuration = ConfigurationService(tenancy.database.session_factory)
    draft = await configuration.get(context)
    location = uuid4()
    async with tenancy.admin.begin() as session:
        session.add(
            Location(
                id=location, tenant_id=context.tenant_id, name="Office", timezone="Asia/Kolkata"
            )
        )
    document = draft.document.model_copy(deep=True)
    document.hours = HoursSettings(
        windows=[OpeningWindow(weekday=0, opens=time(8), closes=time(18))],
        holidays=[date(2026, 12, 25)],
    )
    document.scopes.locations[location] = LocationOverride(
        hours=HoursSettings(windows=[OpeningWindow(weekday=1, opens=time(10), closes=time(14))])
    )
    saved = await configuration.save(
        context, SaveConfiguration(revision=draft.revision, document=document)
    )
    first = await configuration.publish(context, saved.revision)
    async with tenancy.database.session_factory.begin() as session:
        await set_tenant_scope(session, context.tenant_id)
        assert len((await session.scalars(select(HoursPolicy))).all()) == 2
        assert len((await session.scalars(select(BusinessOpeningWindow))).all()) == 2
        assert [row.day for row in await session.scalars(select(BusinessHoliday))] == [
            date(2026, 12, 25)
        ]
        await set_tenant_scope(session, tenancy.b.tenant_id)
        assert (await session.scalars(select(BusinessOpeningWindow))).all() == []
    document.scopes.locations[location].hours = None
    document.hours.windows = []
    saved = await configuration.save(
        context, SaveConfiguration(revision=saved.revision, document=document)
    )
    assert (await configuration.effective(context, location_id=location)).document.hours.windows[
        0
    ].weekday == 1
    async with tenancy.database.session_factory.begin() as session:
        await set_tenant_scope(session, context.tenant_id)
        assert len((await session.scalars(select(HoursPolicy))).all()) == 1
        assert (await session.scalars(select(BusinessOpeningWindow))).all() == []
    restored = await configuration.rollback(context, first.version, saved.revision)
    async with tenancy.database.session_factory.begin() as session:
        await set_tenant_scope(session, context.tenant_id)
        assert len((await session.scalars(select(BusinessOpeningWindow))).all()) == 2
    assert restored.document.hours.windows[0].opens == time(8)
