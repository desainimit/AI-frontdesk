"""A platform administrator working inside a workspace.

They act as themselves, never as a workspace member: every request is checked against
the routes below and written to the platform activity log with the visit it belongs to.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import RequestContext
from app.auth.policies import Permission
from app.core.database.rls import set_tenant_scope, verify_runtime_role
from app.core.errors import AuthorizationError
from app.identity.sessions import validate_support_context
from app.platform.models import PlatformAuditEvent

# Route templates are intentional. Unknown and newly added routes are denied by default.
SUPPORT_ROUTES: dict[tuple[str, str], Permission | None] = {
    ("GET", "/api/v1/tenants/current"): Permission.TENANT_READ,
    ("GET", "/api/v1/settings"): Permission.SETTINGS_READ,
    ("GET", "/api/v1/settings/history"): Permission.SETTINGS_READ,
    ("PUT", "/api/v1/settings/{section}"): Permission.SETTINGS_UPDATE,
    ("GET", "/api/v1/capabilities"): None,
    ("GET", "/api/v1/getting-started"): Permission.TENANT_READ,
    ("GET", "/api/v1/notifications/messages"): Permission.SETTINGS_READ,
    ("POST", "/api/v1/notifications/test"): Permission.SETTINGS_UPDATE,
    ("GET", "/api/v1/permissions"): Permission.ROLE_READ,
    ("GET", "/api/v1/roles"): Permission.ROLE_READ,
    ("POST", "/api/v1/roles"): Permission.ROLE_CREATE,
    ("PUT", "/api/v1/roles/{identity}"): Permission.ROLE_UPDATE,
    ("DELETE", "/api/v1/roles/{identity}"): Permission.ROLE_DELETE,
    ("PUT", "/api/v1/users/{identity}/role"): Permission.ROLE_ASSIGN,
    ("GET", "/api/v1/users/assignable-roles"): Permission.USER_INVITE,
    ("GET", "/api/v1/users"): Permission.USER_READ,
    ("POST", "/api/v1/users/invitations"): Permission.USER_INVITE,
    ("POST", "/api/v1/users/{user_id}/invitations"): Permission.USER_INVITE,
    ("PATCH", "/api/v1/users/{user_id}"): Permission.USER_MANAGE,
    ("POST", "/api/v1/users/{user_id}/revoke-sessions"): Permission.USER_MANAGE,
    ("GET", "/api/v1/audit-events"): Permission.AUDIT_READ,
    ("GET", "/api/v1/locations"): Permission.DIRECTORY_READ,
    ("POST", "/api/v1/locations"): Permission.DIRECTORY_WRITE,
    ("GET", "/api/v1/providers"): Permission.DIRECTORY_READ,
    ("POST", "/api/v1/providers"): Permission.DIRECTORY_WRITE,
    ("PUT", "/api/v1/locations/{location_id}"): Permission.DIRECTORY_WRITE,
    ("PUT", "/api/v1/providers/{provider_id}"): Permission.DIRECTORY_WRITE,
    ("GET", "/api/v1/providers/{provider_id}/slots"): Permission.DIRECTORY_READ,
    ("GET", "/api/v1/providers/{provider_id}/schedule"): Permission.DIRECTORY_READ,
    ("PUT", "/api/v1/providers/{provider_id}/schedule"): Permission.DIRECTORY_WRITE,
    ("POST", "/api/v1/providers/{provider_id}/time-off"): Permission.DIRECTORY_WRITE,
    (
        "DELETE",
        "/api/v1/providers/{provider_id}/time-off/{time_off_id}",
    ): Permission.DIRECTORY_WRITE,
    ("GET", "/api/v1/calendar"): Permission.DIRECTORY_READ,
    ("POST", "/api/v1/providers/{provider_id}/calendar/connect"): Permission.DIRECTORY_WRITE,
    ("POST", "/api/v1/calendar/complete"): Permission.DIRECTORY_WRITE,
    ("DELETE", "/api/v1/providers/{provider_id}/calendar"): Permission.DIRECTORY_WRITE,
    ("GET", "/api/v1/services"): Permission.DIRECTORY_READ,
    ("POST", "/api/v1/services"): Permission.DIRECTORY_WRITE,
    ("PUT", "/api/v1/services/{service_id}"): Permission.DIRECTORY_WRITE,
    ("PUT", "/api/v1/patients/{patient_id}"): Permission.PATIENT_WRITE,
    ("GET", "/api/v1/patients"): Permission.PATIENT_READ,
    ("POST", "/api/v1/patients"): Permission.PATIENT_WRITE,
    ("GET", "/api/v1/patients/{patient_id}"): Permission.PATIENT_READ,
    ("PUT", "/api/v1/patients/{patient_id}/verification"): Permission.PATIENT_WRITE,
    ("GET", "/api/v1/appointments"): Permission.APPOINTMENT_READ,
    ("POST", "/api/v1/appointments"): Permission.APPOINTMENT_WRITE,
    ("GET", "/api/v1/appointments/{appointment_id}"): Permission.APPOINTMENT_READ,
    ("POST", "/api/v1/appointments/{appointment_id}/cancel"): Permission.APPOINTMENT_WRITE,
    ("POST", "/api/v1/appointments/{appointment_id}/reschedule"): Permission.APPOINTMENT_WRITE,
    ("GET", "/api/v1/knowledge/documents"): Permission.KNOWLEDGE_READ,
    ("POST", "/api/v1/knowledge/documents"): Permission.KNOWLEDGE_WRITE,
    ("GET", "/api/v1/knowledge/documents/{document_id}/versions"): Permission.KNOWLEDGE_READ,
    ("POST", "/api/v1/knowledge/documents/{document_id}/versions"): Permission.KNOWLEDGE_WRITE,
    ("POST", "/api/v1/knowledge/documents/{document_id}/approve"): Permission.KNOWLEDGE_WRITE,
    ("POST", "/api/v1/knowledge/documents/{document_id}/archive"): Permission.KNOWLEDGE_WRITE,
    ("POST", "/api/v1/knowledge/search"): Permission.KNOWLEDGE_READ,
    ("GET", "/api/v1/calls"): Permission.CALL_READ,
    ("GET", "/api/v1/calls/{call_id}"): Permission.CALL_READ,
    ("POST", "/api/v1/calls/{call_id}/patient-access"): Permission.PATIENT_WRITE,
    ("GET", "/api/v1/calls/{call_id}/post-call"): Permission.CALL_READ,
    ("GET", "/api/v1/calls/{call_id}/transcript"): Permission.CALL_READ,
    ("GET", "/api/v1/tickets"): Permission.CALL_READ,
    ("GET", "/api/v1/tickets/counts"): Permission.CALL_READ,
    ("POST", "/api/v1/tickets"): Permission.FOLLOW_UP_MANAGE,
    ("GET", "/api/v1/tickets/{ticket_id}"): Permission.CALL_READ,
    ("PATCH", "/api/v1/tickets/{ticket_id}"): Permission.FOLLOW_UP_MANAGE,
    ("POST", "/api/v1/tickets/{ticket_id}/notes"): Permission.FOLLOW_UP_MANAGE,
    ("POST", "/api/v1/tickets/{ticket_id}/replies"): Permission.FOLLOW_UP_MANAGE,
    ("GET", "/api/v1/conversations"): Permission.CALL_READ,
    ("GET", "/api/v1/conversations/unread"): Permission.CALL_READ,
    ("GET", "/api/v1/conversations/{conversation_id}"): Permission.CALL_READ,
    ("POST", "/api/v1/conversations/{conversation_id}/messages"): Permission.FOLLOW_UP_MANAGE,
    ("POST", "/api/v1/conversations/{conversation_id}/take-over"): Permission.FOLLOW_UP_MANAGE,
    ("POST", "/api/v1/conversations/{conversation_id}/hand-back"): Permission.FOLLOW_UP_MANAGE,
    ("POST", "/api/v1/assistant/test"): Permission.SETTINGS_READ,
    ("GET", "/api/v1/calls/{call_id}/turns"): Permission.CALL_READ,
    ("GET", "/api/v1/calls/{call_id}/live"): Permission.CALL_READ,
    ("GET", "/api/v1/analytics/assistant"): Permission.CALL_READ,
    ("GET", "/api/v1/analytics/calls"): Permission.CALL_READ,
    ("GET", "/api/v1/integration-providers"): Permission.INTEGRATION_READ,
    ("GET", "/api/v1/integrations"): Permission.INTEGRATION_READ,
    ("POST", "/api/v1/integrations"): Permission.INTEGRATION_MANAGE,
    ("GET", "/api/v1/integrations/{integration_id}"): Permission.INTEGRATION_READ,
    ("PATCH", "/api/v1/integrations/{integration_id}"): Permission.INTEGRATION_MANAGE,
    (
        "POST",
        "/api/v1/integrations/{integration_id}/credential-versions",
    ): Permission.INTEGRATION_MANAGE,
    ("POST", "/api/v1/integrations/{integration_id}/verify"): Permission.INTEGRATION_MANAGE,
    (
        "POST",
        "/api/v1/integrations/{integration_id}/activate-version",
    ): Permission.INTEGRATION_MANAGE,
    ("POST", "/api/v1/integrations/{integration_id}/disconnect"): Permission.INTEGRATION_MANAGE,
    ("GET", "/api/v1/integrations/{integration_id}/phone-numbers"): Permission.INTEGRATION_MANAGE,
    ("POST", "/api/v1/integrations/{integration_id}/phone-numbers"): Permission.INTEGRATION_MANAGE,
    ("GET", "/api/v1/tenants/current/readiness"): Permission.INTEGRATION_READ,
    ("POST", "/api/v1/tenants/current/activate"): Permission.TENANT_ACTIVATE,
    ("POST", "/api/v1/tenants/current/deactivate"): Permission.TENANT_ACTIVATE,
}


async def record_support_action(
    sessions: async_sessionmaker[AsyncSession], context: RequestContext, method: str, route: str
) -> None:
    """Log the request against the visit, then refuse it if the route is not allowed."""
    known_route = (method, route) in SUPPORT_ROUTES
    permission = SUPPORT_ROUTES.get((method, route))
    allowed = known_route and (permission is None or permission.value in context.capabilities)
    async with sessions.begin() as db:
        await verify_runtime_role(db)
        await set_tenant_scope(db, context.tenant_id)
        await validate_support_context(db, context)
        db.add(
            PlatformAuditEvent(
                user_id=context.platform_user_id,
                action="support_action_attempt" if allowed else "support_action_denied",
                request_id=context.request_id,
                tenant_id=context.tenant_id,
                support_session_id=context.support_session_id,
                http_method=method[:10],
                route=route[:200],
            )
        )
    if not allowed:
        raise AuthorizationError()
