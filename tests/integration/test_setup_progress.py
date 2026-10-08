"""Setup progress is evidence-based, revision-checked and tenant isolated."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.audit.models import AuditEvent
from app.configuration.schemas import SaveConfigurationSection
from app.configuration.service import ConfigurationService
from app.errors import AuthorizationError
from app.main import create_app
from app.onboarding.schemas import SetupCommand
from app.onboarding.service import SetupBlockedError, SetupService
from app.rbac.service import RevisionConflictError
from tests.integration.test_configuration_sections import editor
from tests.integration.test_saas import authenticate
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_reviews_survive_restart_and_relevant_changes_invalidate_them(
    tenancy: TenancyFixture,
) -> None:
    owner = await authenticate(tenancy)
    service = SetupService(tenancy.database.session_factory)
    initial = await service.get_setup(owner)
    assert initial.revision == 0 and len(initial.steps) == 9
    agent = next(step for step in initial.steps if step.key == "agent")
    assert agent.status == "needs_review" and agent.fingerprint is not None
    command = SetupCommand(
        revision=initial.revision,
        configuration_revision=initial.configuration_revision,
        step="agent",
        action="review",
        expected_fingerprint=agent.fingerprint,
    )
    reviewed = await service.save_setup(owner, command)
    assert reviewed.revision == 1 and reviewed.current_step == "agent"
    assert next(step for step in reviewed.steps if step.key == "agent").status == "reviewed"
    assert await SetupService(tenancy.database.session_factory).get_setup(owner) == reviewed
    with pytest.raises(RevisionConflictError):
        await service.save_setup(owner, command)
    other = await service.get_setup(await authenticate(tenancy, second=True))
    assert other.revision == 0 and other.current_step == "business"
    configuration = ConfigurationService(tenancy.database.session_factory)
    section = await configuration.get_section(owner, "agent")
    assert isinstance(section.value, dict)
    await configuration.save_section(
        owner,
        "agent",
        SaveConfigurationSection(
            revision=section.revision,
            value={**section.value, "greeting": "A newly configured greeting"},
        ),
    )
    changed = await service.get_setup(owner)
    assert next(step for step in changed.steps if step.key == "agent").status == "needs_review"
    with pytest.raises(RevisionConflictError):
        await service.save_setup(
            owner,
            command.model_copy(
                update={
                    "revision": changed.revision,
                    "configuration_revision": changed.configuration_revision,
                }
            ),
        )
    async with tenancy.admin.begin() as session:
        events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.tenant_id == owner.tenant_id,
                    AuditEvent.action == "onboarding_step_reviewed",
                )
            )
        ).all()
        assert (
            len(events) == 1
            and events[0].actor_id == owner.actor_id
            and events[0].resource_id is not None
        )


async def test_restricted_steps_hide_evidence_and_blocked_steps_cannot_be_completed(
    tenancy: TenancyFixture,
) -> None:
    owner = await authenticate(tenancy)
    actor = await editor(tenancy)
    service = SetupService(tenancy.database.session_factory)
    limited = await service.get_setup(actor)
    for key in (
        "administrator",
        "team",
        "knowledge",
        "channels",
        "billing",
        "tests",
        "publication",
    ):
        step = next(step for step in limited.steps if step.key == key)
        assert step.status == "restricted" and step.fingerprint is None and not step.checks
    agent = next(step for step in limited.steps if step.key == "agent")
    assert agent.fingerprint is not None
    with pytest.raises(AuthorizationError):
        await service.save_setup(
            actor,
            SetupCommand(
                revision=0,
                configuration_revision=limited.configuration_revision,
                step="agent",
                action="review",
                expected_fingerprint=agent.fingerprint,
            ),
        )
    view = await service.get_setup(owner)
    for key in ("administrator", "knowledge", "billing", "publication"):
        step = next(step for step in view.steps if step.key == key)
        assert step.status == "blocked" and step.fingerprint is not None
        with pytest.raises(SetupBlockedError):
            await service.save_setup(
                owner,
                SetupCommand(
                    revision=view.revision,
                    configuration_revision=view.configuration_revision,
                    step=step.key,
                    action="review",
                    expected_fingerprint=step.fingerprint,
                ),
            )
    assert (await service.get_setup(owner)).revision == 0


async def test_http_rejects_fabricated_completion_unknown_stages_and_stale_evidence(
    tenancy: TenancyFixture,
) -> None:
    app = create_app(tenancy.settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        headers = {"Authorization": f"Bearer {tenancy.a.token.get_secret_value()}"}
        view = (await client.get("/api/v2/onboarding", headers=headers)).json()
        agent = next(step for step in view["steps"] if step["key"] == "agent")
        command = {
            "revision": view["revision"],
            "configuration_revision": view["configuration_revision"],
            "step": "agent",
            "action": "review",
            "expected_fingerprint": agent["fingerprint"],
        }
        assert (
            await client.put(
                "/api/v2/onboarding", headers=headers, json={**command, "complete": True}
            )
        ).status_code == 422
        assert (
            await client.put(
                "/api/v2/onboarding", headers=headers, json={**command, "step": str(uuid4())}
            )
        ).status_code == 422
        assert (
            await client.put(
                "/api/v2/onboarding",
                headers=headers,
                json={**command, "expected_fingerprint": "0" * 64},
            )
        ).status_code == 412
        assert (
            await client.put("/api/v2/onboarding", headers=headers, json=command)
        ).status_code == 200
        assert (
            await client.put("/api/v2/onboarding", headers=headers, json=command)
        ).status_code == 412
