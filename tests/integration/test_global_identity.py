"""Shared credentials must never grant membership or reset through an invitation."""

import asyncio
import re
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from app.auth.human_models import AuthChallenge, HumanAccount, HumanIdentity
from app.auth.human_schemas import AcceptInvitation, EmailIdentity, InvitationToken, ResetPassword
from app.auth.identity import identity_scope
from app.core.database.rls import set_tenant_scope
from app.core.database.unit_of_work import UnitOfWork
from app.core.errors import AuthenticationError, ConflictError
from app.users.models import User
from app.users.service import UserService
from tests.integration.test_human_auth import PASSWORD, Harness
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def memberships(harness: Harness, email: str):
    platform = await harness.platform()
    first = await harness.onboard(platform, email)
    second = await harness.onboard(platform, email)
    for tenant in (first, second):
        await harness.auth.invite_initial_admin(platform, tenant.tenant_id, str(uuid4()))
    first_token = await harness.token(first.tenant_id, first.admin_id)
    second_token = await harness.token(second.tenant_id, second.admin_id)
    return first, second, first_token, second_token


async def test_existing_account_acceptance_preserves_password_and_tenant_rbac(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    email = f"{uuid4()}@example.test"
    first, second, token_a, token_b = await memberships(harness, email)
    assert (
        await harness.auth.invitation_details(InvitationToken(token=SecretStr(token_a)))
    ).password_required
    with pytest.raises(ConflictError):
        await harness.auth.accept(AcceptInvitation(token=SecretStr(token_a)), uuid4())
    await harness.accept(token_a)
    with pytest.raises(AuthenticationError):
        await harness.login(second.tenant_id, email)
    details = await harness.auth.invitation_details(InvitationToken(token=SecretStr(token_b)))
    assert not details.password_required and not details.account_requires_recovery
    with pytest.raises(ConflictError):
        await harness.accept(token_b, "an attacker replacement password 99")
    await harness.auth.accept(AcceptInvitation(token=SecretStr(token_b)), uuid4())
    a = await harness.login(first.tenant_id, email)
    b = await harness.login(second.tenant_id, email)
    assert a.actor_id != b.actor_id and a.role_id != b.role_id
    assert a.tenant_id == first.tenant_id and b.tenant_id == second.tenant_id
    async with tenancy.admin.begin() as session:
        identities = (
            await session.scalars(select(HumanIdentity).where(HumanIdentity.email == email))
        ).all()
        assert len(identities) == 1
        accounts = (
            await session.scalars(
                select(HumanAccount).where(HumanAccount.identity_id == identities[0].id)
            )
        ).all()
        assert len(accounts) == 2
    with pytest.raises(AuthenticationError):
        await harness.auth.invitation_details(InvitationToken(token=SecretStr(token_b)))


async def test_password_reset_revokes_every_membership_and_old_reset_codes(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    email = f"{uuid4()}@example.test"
    first, second, token_a, token_b = await memberships(harness, email)
    await harness.accept(token_a)
    await harness.auth.accept(AcceptInvitation(token=SecretStr(token_b)), uuid4())
    contexts = [await harness.login(tenant.tenant_id, email) for tenant in (first, second)]
    codes: list[str] = []
    for tenant in (first, second):
        await harness.auth.request_reset(
            EmailIdentity(tenant_id=tenant.tenant_id, email=email), uuid4()
        )
        body = (await harness.mail(tenant.tenant_id, tenant.admin_id))["body"]
        match = re.search(r"\b(\d{8})\b", body)
        assert match is not None
        codes.append(match.group(1))
    replacement = "replacement global passphrase 42"
    await harness.auth.reset(
        ResetPassword(
            tenant_id=first.tenant_id,
            email=email,
            otp=SecretStr(codes[0]),
            password=SecretStr(replacement),
        ),
        uuid4(),
    )
    for context in contexts:
        with pytest.raises(AuthenticationError):
            await UserService(UnitOfWork(harness.sessions)).me(context)
    with pytest.raises(AuthenticationError):
        await harness.auth.reset(
            ResetPassword(
                tenant_id=second.tenant_id,
                email=email,
                otp=SecretStr(codes[1]),
                password=SecretStr("another unwanted passphrase 99"),
            ),
            uuid4(),
        )
    for tenant in (first, second):
        with pytest.raises(AuthenticationError):
            await harness.login(tenant.tenant_id, email, PASSWORD)
        assert (
            await harness.login(tenant.tenant_id, email, replacement)
        ).tenant_id == tenant.tenant_id


async def test_global_identity_rls_requires_explicit_identity_scope(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    email = f"{uuid4()}@example.test"
    first, _, token, _ = await memberships(harness, email)
    await harness.accept(token)
    async with harness.sessions.begin() as session:
        await set_tenant_scope(session, first.tenant_id)
        assert (await session.scalars(select(HumanIdentity))).all() == []
        await identity_scope(session, "unrelated@example.test")
        assert (await session.scalars(select(HumanIdentity))).all() == []
        await identity_scope(session, email)
        assert [identity.email for identity in await session.scalars(select(HumanIdentity))] == [
            email
        ]
    async with harness.sessions.begin() as session:
        assert (await session.scalars(select(HumanIdentity))).all() == []


async def test_simultaneous_new_memberships_never_overwrite_a_password(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    email = f"{uuid4()}@example.test"
    _, _, token_a, token_b = await memberships(harness, email)
    outcomes = await asyncio.gather(
        harness.accept(token_a), harness.accept(token_b), return_exceptions=True
    )
    assert sum(isinstance(outcome, ConflictError) for outcome in outcomes) == 1
    remaining = token_a if isinstance(outcomes[0], ConflictError) else token_b
    await harness.auth.accept(AcceptInvitation(token=SecretStr(remaining)), uuid4())
    async with tenancy.admin.begin() as session:
        assert (
            len(
                (
                    await session.scalars(select(HumanIdentity).where(HumanIdentity.email == email))
                ).all()
            )
            == 1
        )


async def test_conflicting_legacy_identity_requires_reset_and_not_invitation_recovery(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    email = f"{uuid4()}@example.test"
    first, second, token_a, token_b = await memberships(harness, email)
    await harness.accept(token_a)
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(HumanIdentity).where(HumanIdentity.email == email).values(password_hash=None)
        )
        await session.execute(
            update(User).where(User.id == first.admin_id).values(email=email.upper())
        )
    details = await harness.auth.invitation_details(InvitationToken(token=SecretStr(token_b)))
    assert details.account_requires_recovery and not details.password_required
    replacement = "recovered global credential 42"
    with pytest.raises(ConflictError):
        await harness.accept(token_b, replacement)
    with pytest.raises(AuthenticationError):
        await harness.login(first.tenant_id, email)
    await harness.auth.request_reset(EmailIdentity(tenant_id=first.tenant_id, email=email), uuid4())
    body = (await harness.mail(first.tenant_id, first.admin_id))["body"]
    match = re.search(r"\b(\d{8})\b", body)
    assert match is not None
    await harness.auth.reset(
        ResetPassword(
            tenant_id=first.tenant_id,
            email=email,
            otp=SecretStr(match.group(1)),
            password=SecretStr(replacement),
        ),
        uuid4(),
    )
    assert not (
        await harness.auth.invitation_details(InvitationToken(token=SecretStr(token_b)))
    ).account_requires_recovery
    await harness.auth.accept(AcceptInvitation(token=SecretStr(token_b)), uuid4())
    for tenant in (first, second):
        assert (
            await harness.login(tenant.tenant_id, email, replacement)
        ).tenant_id == tenant.tenant_id


async def test_ambiguous_legacy_memberships_cannot_login_or_reset(tenancy: TenancyFixture) -> None:
    harness = Harness(tenancy)
    email = f"{uuid4()}@example.test"
    first, _, token, _ = await memberships(harness, email)
    await harness.accept(token)
    command = EmailIdentity(tenant_id=first.tenant_id, email=email)
    await harness.auth.request_reset(command, uuid4())
    body = (await harness.mail(first.tenant_id, first.admin_id))["body"]
    match = re.search(r"\b(\d{8})\b", body)
    assert match is not None
    async with tenancy.admin.begin() as session:
        original = await session.get(User, first.admin_id)
        identity = await session.scalar(select(HumanIdentity).where(HumanIdentity.email == email))
        assert original is not None and identity is not None
        duplicate = User(
            tenant_id=first.tenant_id,
            email=email.upper(),
            display_name="Ambiguous legacy member",
            role=original.role,
            role_id=original.role_id,
        )
        session.add(duplicate)
        await session.flush()
        session.add(
            HumanAccount(tenant_id=first.tenant_id, user_id=duplicate.id, identity_id=identity.id)
        )
    with pytest.raises(AuthenticationError):
        await harness.login(first.tenant_id, email)
    await harness.auth.request_reset(command, uuid4())
    async with tenancy.admin.begin() as session:
        challenges = (
            await session.scalars(
                select(AuthChallenge).where(
                    AuthChallenge.tenant_id == first.tenant_id, AuthChallenge.purpose == "reset"
                )
            )
        ).all()
        assert len(challenges) == 1 and challenges[0].consumed_at is None
    with pytest.raises(AuthenticationError):
        await harness.auth.reset(
            ResetPassword(
                tenant_id=first.tenant_id,
                email=email,
                otp=SecretStr(match.group(1)),
                password=SecretStr("unwanted ambiguous recovery 42"),
            ),
            uuid4(),
        )
