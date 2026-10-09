"""Email-only routing preserves mailbox proof, accepted memberships and global revocation."""

import base64
import json
import re
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.auth.global_recovery import GlobalRecoveryService, RecoveryReceipt
from app.auth.human_models import AuthChallenge
from app.auth.human_schemas import AcceptInvitation
from app.auth.workspace_schemas import (
    GlobalEmail,
    GlobalPasswordReset,
    IdentityCredential,
    IdentityLogin,
)
from app.auth.workspaces import WorkspaceIdentityService
from app.core.errors import AuthenticationError
from app.main import create_app
from tests.integration.test_global_identity import memberships
from tests.integration.test_human_auth import PASSWORD, Harness
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy

REPLACEMENT = "replacement verified mailbox password 567"


async def test_email_recovery_revokes_every_workspace_and_admission(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    email = f"{uuid4()}@example.test"
    first, second, token_a, token_b = await memberships(harness, email)
    await harness.accept(token_a)
    await harness.auth.accept(AcceptInvitation(token=SecretStr(token_b)), uuid4())
    contexts = [await harness.login(row.tenant_id, email) for row in (first, second)]
    chooser = WorkspaceIdentityService(harness.sessions, harness.settings)
    admission = await chooser.login(
        IdentityLogin(email=email, password=SecretStr(PASSWORD)), uuid4()
    )
    recovery = GlobalRecoveryService(harness.sessions, harness.settings)
    view = await recovery.request(GlobalEmail(email=email.upper()), uuid4())
    target = await recovery.accepted_workspace(email)
    owner = first if target == first.tenant_id else second
    match = re.search(r"\b(\d{8})\b", (await harness.mail(owner.tenant_id, owner.admin_id))["body"])
    assert match is not None
    command = GlobalPasswordReset(
        email=email,
        recovery_token=SecretStr(view.recovery_token),
        otp=SecretStr(match.group(1)),
        password=SecretStr(REPLACEMENT),
    )
    await recovery.reset(command, uuid4())
    with pytest.raises(AuthenticationError):
        await recovery.reset(command, uuid4())
    for context in contexts:
        assert context.human_session_id is not None
        # Current identity revision invalidates already-authorized request contexts too.
        from app.auth.human import validate_human_context
        from app.core.database.rls import set_tenant_scope

        async with harness.sessions.begin() as session:
            await set_tenant_scope(session, context.tenant_id)
            with pytest.raises(AuthenticationError):
                await validate_human_context(session, context)
    with pytest.raises(AuthenticationError):
        await chooser.workspaces(
            IdentityCredential(email=email, token=SecretStr(admission.access_token))
        )
    for row in (first, second):
        await harness.login(row.tenant_id, email, REPLACEMENT)


async def test_unknown_and_pending_membership_cannot_authorize_recovery(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    email = f"{uuid4()}@example.test"
    first, second, token_a, _ = await memberships(harness, email)
    recovery = GlobalRecoveryService(harness.sessions, harness.settings)
    pending = await recovery.request(GlobalEmail(email=email), uuid4())
    unknown = await recovery.request(GlobalEmail(email=f"{uuid4()}@example.test"), uuid4())
    assert pending.model_dump().keys() == unknown.model_dump().keys()
    assert len(pending.recovery_token) == len(unknown.recovery_token)
    async with tenancy.admin.begin() as session:
        assert not (
            await session.scalars(
                select(AuthChallenge).where(
                    AuthChallenge.tenant_id.in_([first.tenant_id, second.tenant_id]),
                    AuthChallenge.purpose == "reset",
                )
            )
        ).all()
    # Even a legitimate setup link is not a password recovery receipt.
    for token in (pending.recovery_token, token_a):
        with pytest.raises(AuthenticationError):
            await recovery.reset(
                GlobalPasswordReset(
                    email=email,
                    recovery_token=SecretStr(token),
                    otp=SecretStr("00000000"),
                    password=SecretStr(REPLACEMENT),
                ),
                uuid4(),
            )
    assert await recovery.accepted_workspace(email) is None


async def test_receipt_binding_expiry_resend_and_committed_attempt_limit(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    email = f"{uuid4()}@example.test"
    first, _, token_a, _ = await memberships(harness, email)
    await harness.accept(token_a)
    recovery = GlobalRecoveryService(harness.sessions, harness.settings)
    old = await recovery.request(GlobalEmail(email=email), uuid4())
    current = await recovery.request(GlobalEmail(email=email), uuid4())
    match = re.search(r"\b(\d{8})\b", (await harness.mail(first.tenant_id, first.admin_id))["body"])
    assert match is not None
    code = match.group(1)
    command = GlobalPasswordReset(
        email=email,
        recovery_token=SecretStr(current.recovery_token),
        otp=SecretStr(code),
        password=SecretStr(REPLACEMENT),
    )
    with pytest.raises(AuthenticationError):
        await recovery.reset(
            command.model_copy(update={"recovery_token": SecretStr(old.recovery_token)}), uuid4()
        )
    with pytest.raises(AuthenticationError):
        await recovery.reset(command.model_copy(update={"email": "another@example.test"}), uuid4())
    with pytest.raises(AuthenticationError):
        await recovery.reset(
            command.model_copy(update={"recovery_token": SecretStr("x" * 120)}), uuid4()
        )
    receipt = RecoveryReceipt(
        tenant_id=first.tenant_id,
        challenge_id=uuid4(),
        expires=int(datetime.now(UTC).timestamp()) - 1,
    )
    key, ciphertext = recovery.cipher().encrypt(
        receipt.model_dump_json().encode(), recovery.binding(email)
    )
    expired = base64.urlsafe_b64encode(
        json.dumps([key, base64.b64encode(ciphertext).decode()]).encode()
    ).decode()
    with pytest.raises(AuthenticationError):
        await recovery.reset(
            command.model_copy(update={"recovery_token": SecretStr(expired)}), uuid4()
        )
    wrong = "00000000" if code != "00000000" else "11111111"
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            await recovery.reset(command.model_copy(update={"otp": SecretStr(wrong)}), uuid4())
    with pytest.raises(AuthenticationError):
        await recovery.reset(command, uuid4())
    async with tenancy.admin.begin() as session:
        challenge = await session.scalar(
            select(AuthChallenge).where(
                AuthChallenge.user_id == first.admin_id,
                AuthChallenge.purpose == "reset",
                AuthChallenge.consumed_at.is_(None),
            )
        )
        assert challenge is not None and challenge.attempts == 5
    await harness.login(first.tenant_id, email)


async def test_recovery_http_generic_shape_no_store_and_separate_attempt_budget(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    email = f"{uuid4()}@example.test"
    first, _, token_a, _ = await memberships(harness, email)
    await harness.accept(token_a)
    app = create_app(harness.settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, client=(f"203.0.113.{uuid4().int % 250 + 1}", 12345)),
            base_url="http://test",
        ) as client,
    ):
        known = await client.post("/api/v2/identity/password/request", json={"email": email})
        unknown = await client.post(
            "/api/v2/identity/password/request", json={"email": f"{uuid4()}@example.test"}
        )
        assert known.status_code == unknown.status_code == 202
        assert known.json().keys() == unknown.json().keys()
        assert known.headers["cache-control"] == "no-store"
        match = re.search(
            r"\b(\d{8})\b", (await harness.mail(first.tenant_id, first.admin_id))["body"]
        )
        assert match is not None
        wrong = "00000000" if match.group(1) != "00000000" else "11111111"
        body = {
            "email": email,
            "recovery_token": known.json()["recovery_token"],
            "otp": wrong,
            "password": REPLACEMENT,
        }
        for _ in range(5):
            assert (
                await client.post("/api/v2/identity/password/reset", json=body)
            ).status_code == 401
        assert (await client.post("/api/v2/identity/password/reset", json=body)).status_code == 429
