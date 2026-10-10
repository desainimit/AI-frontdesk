"""Sessions: issuing them, turning a bearer token into a request context, rechecking it.

The contexts built here are the only way the rest of the application learns who is
calling. Every transaction that acts on one rechecks the session behind it, so signing
out, a password change or a removed membership takes effect on the next statement.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission
from app.core.database.rls import set_tenant_scope, verify_runtime_role
from app.core.errors import ApplicationError, AuthenticationError, AuthorizationError
from app.core.settings import Settings
from app.identity.models import Account, AccountSession
from app.identity.tokens import SESSION, issue_token, parse_token, token_matches
from app.platform.policies import PlatformContext, PlatformRole
from app.tenants.models import Tenant
from app.users.models import User

WORKSPACE_SESSION = timedelta(hours=8)
# Long enough to open the authenticator app; the session opens nothing until the code is given.
PENDING_SESSION = timedelta(minutes=10)
SUPPORT_VISIT = timedelta(minutes=30)
ALL_PERMISSIONS = frozenset(item.value for item in Permission)


class WorkspaceRequiredError(ApplicationError):
    status_code = 409
    code = "workspace_required"
    public_message = "Choose a workspace to continue."


def session_lifetime(settings: Settings, account: Account) -> timedelta:
    if account.is_platform_admin:
        return timedelta(minutes=settings.platform_session_minutes)
    return WORKSPACE_SESSION


def issue_session(
    db: AsyncSession, account: Account, lifetime: timedelta, *, mfa_pending: bool = False
) -> tuple[AccountSession, str]:
    """Add a session for the account and return it with the token to hand out once."""
    identity, token, digest = issue_token(SESSION)
    row = AccountSession(
        id=identity,
        account_id=account.id,
        token_digest=digest,
        credential_revision=account.credential_revision,
        expires_at=datetime.now(UTC) + lifetime,
        mfa_pending=mfa_pending,
    )
    db.add(row)
    return row, token


async def account_scope(db: AsyncSession, account_id: UUID) -> None:
    """Let this transaction read the account's own memberships across workspaces."""
    await db.execute(
        text("SELECT set_config('app.account_id', :account_id, true)"),
        {"account_id": str(account_id)},
    )


async def load_session(
    db: AsyncSession, token: str, *, pending: bool = False, lock: bool = False
) -> tuple[AccountSession, Account]:
    """The live session and account for a bearer token, or an authentication error.

    `pending` selects the one case where a session still owing its two-step code is
    accepted: submitting that code.
    """
    identity = parse_token(token, SESSION)
    query = select(AccountSession).where(AccountSession.id == identity)
    row = await db.scalar(query.with_for_update() if lock else query)
    account = await db.get(Account, row.account_id) if row is not None else None
    if (
        not token_matches(token, row.token_digest if row is not None else None)
        or row is None
        or account is None
        or row.revoked_at is not None
        or row.expires_at <= datetime.now(UTC)
        or not account.is_active
        or account.credential_revision != row.credential_revision
        or row.mfa_pending != pending
    ):
        raise AuthenticationError()
    return row, account


def _live(now: datetime) -> tuple[ColumnElement[bool], ...]:
    """Conditions every usable session and its account satisfy."""
    return (
        AccountSession.revoked_at.is_(None),
        AccountSession.expires_at > now,
        AccountSession.mfa_pending.is_(False),
        Account.id == AccountSession.account_id,
        Account.is_active.is_(True),
        Account.credential_revision == AccountSession.credential_revision,
    )


async def workspace_context(
    db: AsyncSession, row: AccountSession, account: Account, request_id: UUID
) -> RequestContext:
    """Who is acting in the session's workspace: a member, or a visiting administrator."""
    from app.rbac.service import bind_user_permissions

    if row.tenant_id is None:
        raise WorkspaceRequiredError()
    await set_tenant_scope(db, row.tenant_id)
    tenant = await db.get(Tenant, row.tenant_id)
    if tenant is None or not tenant.is_active:
        raise WorkspaceRequiredError()
    if row.support_id is not None:
        expires = row.support_expires_at
        if not account.is_platform_admin or expires is None or expires <= datetime.now(UTC):
            raise WorkspaceRequiredError()
        return RequestContext(
            request_id=request_id,
            tenant_id=tenant.id,
            actor_id=None,
            actor_type=ActorType.PLATFORM_SUPPORT,
            platform_user_id=account.id,
            session_id=row.id,
            support_session_id=row.support_id,
            support_expires_at=expires,
            capabilities=ALL_PERMISSIONS,
        )
    user = await db.scalar(
        select(User).where(
            User.tenant_id == tenant.id,
            User.account_id == account.id,
            User.is_active.is_(True),
            User.accepted_at.is_not(None),
        )
    )
    if user is None:
        raise WorkspaceRequiredError()
    context = RequestContext(request_id, tenant.id, user.id, ActorType.USER, session_id=row.id)
    return await bind_user_permissions(db, context, user)


async def authenticate_workspace(
    sessions: async_sessionmaker[AsyncSession], token: str, request_id: UUID
) -> RequestContext:
    async with sessions.begin() as db:
        await verify_runtime_role(db)
        row, account = await load_session(db, token)
        return await workspace_context(db, row, account, request_id)


async def authenticate_platform(
    sessions: async_sessionmaker[AsyncSession], token: str, request_id: UUID
) -> PlatformContext:
    async with sessions.begin() as db:
        await verify_runtime_role(db)
        row, account = await load_session(db, token)
        if not account.is_platform_admin:
            raise AuthorizationError()
        return PlatformContext(account.id, row.id, request_id, PlatformRole.SUPER_ADMIN)


async def validate_user_session(db: AsyncSession, context: RequestContext, user: User) -> None:
    """Recheck, inside the operation, that the member's session still stands."""
    now = datetime.now(UTC)
    live = await db.scalar(
        select(AccountSession.id)
        .join(Account, Account.id == AccountSession.account_id)
        .where(
            AccountSession.id == context.session_id,
            AccountSession.account_id == user.account_id,
            AccountSession.tenant_id == context.tenant_id,
            AccountSession.support_id.is_(None),
            *_live(now),
        )
        .with_for_update(read=True, of=AccountSession)
    )
    if live is None:
        raise AuthenticationError()


async def validate_support_context(db: AsyncSession, context: RequestContext) -> AccountSession:
    """Recheck a platform administrator's visit: still signed in, same workspace, in time."""
    if context.actor_type != ActorType.PLATFORM_SUPPORT or context.support_session_id is None:
        raise AuthenticationError()
    now = datetime.now(UTC)
    row = await db.scalar(
        select(AccountSession)
        .join(Account, Account.id == AccountSession.account_id)
        .join(Tenant, Tenant.id == AccountSession.tenant_id)
        .where(
            AccountSession.id == context.session_id,
            AccountSession.account_id == context.platform_user_id,
            AccountSession.tenant_id == context.tenant_id,
            AccountSession.support_id == context.support_session_id,
            AccountSession.support_expires_at > now,
            Account.is_platform_admin.is_(True),
            Tenant.is_active.is_(True),
            *_live(now),
        )
        .with_for_update(read=True, of=AccountSession)
    )
    if row is None or context.capabilities != ALL_PERMISSIONS:
        raise AuthenticationError()
    return row


async def validate_platform_context(db: AsyncSession, context: PlatformContext) -> Account:
    """Recheck, inside the operation, that the platform administrator is still signed in."""
    await verify_runtime_role(db)
    account = await db.scalar(
        select(Account)
        .join(AccountSession, AccountSession.account_id == Account.id)
        .where(
            Account.id == context.user_id,
            Account.is_platform_admin.is_(True),
            AccountSession.id == context.session_id,
            *_live(datetime.now(UTC)),
        )
    )
    if account is None:
        raise AuthenticationError()
    return account


async def platform_scope(db: AsyncSession, context: PlatformContext) -> Account:
    """Recheck the administrator, then let this transaction manage every workspace."""
    account = await validate_platform_context(db, context)
    await db.execute(
        text("SELECT set_config('app.platform_user_id', :identity, true)"),
        {"identity": str(context.user_id)},
    )
    return account
