"""A publication review must compare current persisted values under authorization."""

from dataclasses import replace

import pytest

from app.configuration.schemas import SaveConfiguration
from app.configuration.service import ConfigurationService
from app.core.errors import AuthorizationError
from app.rbac.service import RevisionConflictError
from tests.integration.test_saas import authenticate
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_revision_bound_review_validation_and_tenant_isolation(
    tenancy: TenancyFixture,
) -> None:
    service = ConfigurationService(tenancy.database.session_factory)
    a, b = await authenticate(tenancy), await authenticate(tenancy, second=True)
    draft = await service.get(a)
    first = await service.diff(a, draft.revision)
    assert first.published_version is None and first.validation.valid
    assert all(change.kind == "added" for change in first.changes)
    assert (
        next(c.draft for c in first.changes if c.field == "business.name")
        == draft.document.business.name
    )
    await service.publish(a, draft.revision)
    assert (await service.diff(a, draft.revision)).changes == []
    edited = draft.document.model_copy(deep=True)
    edited.agent.greeting = "नमस्ते — welcome to this workspace"
    edited.languages.enabled_languages = ["en", "hi"]
    saved = await service.save(a, SaveConfiguration(revision=draft.revision, document=edited))
    reviewed = await service.diff(a, saved.revision)
    assert reviewed.published_version == 1 and reviewed.validation.valid
    assert {change.field for change in reviewed.changes} == {
        "agent.greeting",
        "languages.enabled_languages",
    }
    greeting = next(c for c in reviewed.changes if c.field == "agent.greeting")
    assert greeting.published == draft.document.agent.greeting
    assert greeting.draft == edited.agent.greeting and greeting.kind == "changed"
    assert (await service.effective(a)).document.agent.greeting == draft.document.agent.greeting
    other = await service.get(b)
    assert (await service.diff(b, other.revision)).published_version is None
    with pytest.raises(RevisionConflictError):
        await service.diff(a, draft.revision)
    with pytest.raises(AuthorizationError):
        await service.diff(replace(a, capabilities=frozenset()), saved.revision)
    edited.voice.enabled = True
    blocked = await service.save(a, SaveConfiguration(revision=saved.revision, document=edited))
    assert not (await service.diff(a, blocked.revision)).validation.valid
