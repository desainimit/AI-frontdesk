"""Chooser admission never grants a tenant membership or bypasses current tenant authority."""

import re
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, update

from app.auth.human_schemas import AcceptInvitation, EmailIdentity, ResetPassword
from app.auth.identity import identity_scope
from app.auth.service import AuthenticationService
from app.auth.workspace_models import IdentityMembership, IdentitySession
from app.auth.workspace_schemas import IdentityCredential, IdentityLogin, WorkspaceSelection
from app.auth.workspaces import WorkspaceIdentityService
from app.core.database.rls import set_tenant_scope
from app.core.errors import AuthenticationError, AuthorizationError
from app.main import create_app
from app.tenants.models import Tenant
from tests.integration.test_global_identity import memberships
from tests.integration.test_human_auth import PASSWORD, Harness
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_chooser_accepted_memberships_only_and_tenant_tokens_remain_scoped(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    email = f"{uuid4()}@example.test"
    first, second, token_a, token_b = await memberships(harness, email)
    await harness.accept(token_a)
    identity = WorkspaceIdentityService(harness.sessions, harness.settings)
    login = await identity.login(
        IdentityLogin(email=email.upper(), password=SecretStr(PASSWORD)), uuid4()
    )
    credential = IdentityCredential(email=email, token=SecretStr(login.access_token))
    assert [row.tenant_id for row in (await identity.workspaces(credential)).items] == [
        first.tenant_id
    ]
    with pytest.raises(AuthenticationError):
        await AuthenticationService(harness.sessions).authenticate(login.access_token, uuid4())
    with pytest.raises(AuthorizationError):
        await identity.select(
            WorkspaceSelection(**credential.model_dump(), tenant_id=second.tenant_id), uuid4()
        )
    await harness.auth.accept(AcceptInvitation(token=SecretStr(token_b)), uuid4())
    assert {row.tenant_id for row in (await identity.workspaces(credential)).items} == {
        first.tenant_id,
        second.tenant_id,
    }
    chosen = await identity.select(
        WorkspaceSelection(**credential.model_dump(), tenant_id=second.tenant_id), uuid4()
    )
    context = await AuthenticationService(harness.sessions).authenticate(
        chosen.access_token, uuid4()
    )
    assert context.tenant_id == second.tenant_id and context.actor_id == second.admin_id
    async with harness.sessions.begin() as session:
        await set_tenant_scope(session, first.tenant_id)
        assert not (await session.scalars(select(IdentityMembership))).all()
        await identity_scope(session, email)
        assert len((await session.scalars(select(IdentityMembership))).all()) == 2
        await identity_scope(session, "unrelated@example.test")
        assert not (await session.scalars(select(IdentitySession))).all()
    await identity.logout(credential)
    with pytest.raises(AuthenticationError):
        await identity.workspaces(credential)


async def test_chooser_current_activation_and_password_revocation(tenancy: TenancyFixture) -> None:
    harness = Harness(tenancy)
    email = f"{uuid4()}@example.test"
    first, second, token_a, token_b = await memberships(harness, email)
    await harness.accept(token_a)
    await harness.auth.accept(AcceptInvitation(token=SecretStr(token_b)), uuid4())
    identity = WorkspaceIdentityService(harness.sessions, harness.settings)
    login = await identity.login(IdentityLogin(email=email, password=SecretStr(PASSWORD)), uuid4())
    credential = IdentityCredential(email=email, token=SecretStr(login.access_token))
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(Tenant).where(Tenant.id == second.tenant_id).values(is_active=False)
        )
    assert [row.tenant_id for row in (await identity.workspaces(credential)).items] == [
        first.tenant_id
    ]
    with pytest.raises(AuthorizationError):
        await identity.select(
            WorkspaceSelection(**credential.model_dump(), tenant_id=second.tenant_id), uuid4()
        )
    await harness.auth.request_reset(EmailIdentity(tenant_id=first.tenant_id, email=email), uuid4())
    match = re.search(r"\b(\d{8})\b", (await harness.mail(first.tenant_id, first.admin_id))["body"])
    assert match is not None
    await harness.auth.reset(
        ResetPassword(
            tenant_id=first.tenant_id,
            email=email,
            otp=SecretStr(match.group(1)),
            password=SecretStr("replacement global password 567"),
        ),
        uuid4(),
    )
    with pytest.raises(AuthenticationError):
        await identity.select(
            WorkspaceSelection(**credential.model_dump(), tenant_id=first.tenant_id), uuid4()
        )
    with pytest.raises(AuthenticationError):
        await identity.login(IdentityLogin(email=email, password=SecretStr(PASSWORD)), uuid4())


async def test_chooser_refresh_http_limit_is_separate_from_password_attempts(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    email = f"{uuid4()}@example.test"
    first, _, token, _ = await memberships(harness, email)
    await harness.accept(token)
    app = create_app(harness.settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, client=(f"203.0.113.{uuid4().int % 250 + 1}", 12345)),
            base_url="http://test",
        ) as client,
    ):
        login = await client.post(
            "/api/v2/identity/login", json={"email": email, "password": PASSWORD}
        )
        assert login.status_code == 200
        credential = {"email": email, "token": login.json()["access_token"]}
        for _ in range(7):
            response = await client.post("/api/v2/identity/workspaces", json=credential)
            assert response.status_code == 200
            assert response.json()["items"][0]["tenant_id"] == str(first.tenant_id)
        selected = await client.post(
            "/api/v2/identity/select", json={**credential, "tenant_id": str(first.tenant_id)}
        )
        assert selected.status_code == 200 and selected.json()["tenant_id"] == str(first.tenant_id)
        await client.post("/api/v2/identity/logout", json=credential)
        assert (
            await client.post("/api/v2/identity/workspaces", json=credential)
        ).status_code == 401
