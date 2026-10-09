"""Tenant business edits stage a configuration revision; publication owns runtime changes."""

import hashlib
import json
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, literal, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import RequestContext
from app.auth.policies import Permission, has_permission
from app.business.models import BusinessResource, BusinessService, BusinessStaff, DirectoryMutation
from app.business.persistence import sync_directory
from app.business.schemas import (
    BusinessListItem,
    BusinessListPage,
    BusinessListQuery,
    BusinessRecordView,
    DirectorySnapshot,
    DirectoryView,
    DirectoryWrite,
    DirectoryWriteResult,
    ResourceDefinition,
    ServiceDefinition,
    StaffDefinition,
)
from app.configuration.schemas import OperationalConfiguration
from app.configuration.service import ConfigurationService
from app.core.errors import ConflictError, NotFoundError
from app.core.security.cursors import CursorCodec
from app.core.security.encryption import SecretCipher
from app.integrations.models import TenantIntegration
from app.locations.models import Location
from app.rbac.service import RevisionConflictError, audit
from app.users.models import User


async def validate_directory_references(
    session: AsyncSession, tenant_id: UUID, snapshot: DirectorySnapshot
) -> None:
    staff = {row.id: row for row in snapshot.staff}
    resources = {row.id: row for row in snapshot.resources}
    for record in (*snapshot.services, *snapshot.staff, *snapshot.resources):
        if record.location_id is not None:
            location = await session.get(Location, record.location_id)
            if location is None or location.tenant_id != tenant_id:
                raise ConflictError()
        if isinstance(record, StaffDefinition):
            if record.user_id is not None:
                user = await session.get(User, record.user_id)
                if (
                    user is None
                    or user.tenant_id != tenant_id
                    or not user.is_active
                    or user.accepted_at is None
                ):
                    raise ConflictError()
            if record.calendar_connection_id is not None:
                connection = await session.get(TenantIntegration, record.calendar_connection_id)
                if (
                    connection is None
                    or connection.tenant_id != tenant_id
                    or connection.purpose != "calendar"
                    or connection.status != "verified"
                ):
                    raise ConflictError()
        if isinstance(record, ServiceDefinition):
            for identity in record.staff_ids:
                member = staff.get(identity)
                if (
                    member is None
                    or (record.is_active and not member.is_active)
                    or (
                        record.location_id is not None
                        and member.location_id not in (None, record.location_id)
                    )
                ):
                    raise ConflictError()
            for identity in record.resource_ids:
                resource = resources.get(identity)
                if (
                    resource is None
                    or (record.is_active and not resource.is_active)
                    or (
                        record.location_id is not None
                        and resource.location_id != record.location_id
                    )
                ):
                    raise ConflictError()


class BusinessDirectoryService(ConfigurationService):
    async def receipt(self, context: RequestContext, identity: UUID) -> DirectoryWriteResult:
        async with self.sessions.begin() as session:
            # Serialize with commits; an absent receipt still permits only an idempotent retry.
            await self._enter(session, context, Permission.BUSINESS_WRITE, lock=True)
            row = await session.get(DirectoryMutation, identity)
            if row is None:
                raise NotFoundError()
            return DirectoryWriteResult.model_validate(row.result)

    async def record(self, context: RequestContext, identity: UUID) -> BusinessRecordView:
        view = await self.get_directory(context)
        record = next(
            (
                row
                for row in (*view.records.services, *view.records.staff, *view.records.resources)
                if row.id == identity
            ),
            None,
        )
        if record is None:
            raise NotFoundError()
        return BusinessRecordView(
            draft_revision=view.draft_revision,
            item=BusinessListItem(revision=view.revisions[identity], record=record),
        )

    async def list_records(
        self, context: RequestContext, query: BusinessListQuery, cipher: SecretCipher
    ) -> BusinessListPage:
        binding = json.dumps(
            {
                "tenant": str(context.tenant_id),
                "actor": str(context.actor_id),
                "platform_actor": str(context.platform_user_id),
                "support_session": str(context.support_session_id),
                "role": str(context.role_id),
                "authorization": context.authorization_revision,
                "query": query.model_dump(mode="json", exclude={"cursor"}),
            },
            sort_keys=True,
        )
        codec = CursorCodec(cipher, binding)
        async with self.sessions.begin() as session:
            tenant = await self._enter(session, context, Permission.BUSINESS_READ, lock=True)
            draft = await self._draft(session, tenant)
            model = {
                "service": BusinessService,
                "staff": BusinessStaff,
                "resource": BusinessResource,
            }[query.kind]
            snapshot = OperationalConfiguration.model_validate(draft.document).directory
            records = {
                record.id: record
                for record in (*snapshot.services, *snapshot.staff, *snapshot.resources)
            }
            statement = select(model).where(model.tenant_id == tenant.id, model.id.in_(records))
            if query.search:
                escaped = query.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                statement = statement.where(model.name.ilike(f"%{escaped}%", escape="\\"))
            if query.active is not None:
                statement = statement.where(model.is_active == query.active)
            if query.location_id is not None:
                statement = statement.where(model.location_id == query.location_id)
            if query.cursor:
                anchor = codec.decode(query.cursor)
                if anchor.revision != draft.revision:
                    raise RevisionConflictError()
                try:
                    identity = UUID(anchor.identity)
                except ValueError:
                    raise ConflictError() from None
                statement = statement.where(
                    tuple_(model.name, model.id) > tuple_(literal(anchor.name), literal(identity))
                )
            rows = (
                await session.scalars(
                    statement.order_by(model.name, model.id).limit(query.limit + 1)
                )
            ).all()
            page_rows = rows[: query.limit]
            items = [
                BusinessListItem(revision=row.revision, record=records[row.id])
                for row in page_rows
                if row.id in records
            ]
            cursor = (
                codec.encode(page_rows[-1].name, str(page_rows[-1].id), draft.revision)
                if len(rows) > query.limit
                else None
            )
            return BusinessListPage(
                draft_revision=draft.revision,
                published_version=draft.published_version,
                items=items,
                next_cursor=cursor,
            )

    async def get_directory(self, context: RequestContext) -> DirectoryView:
        async with self.sessions.begin() as session:
            tenant = await self._enter(session, context, Permission.BUSINESS_READ, lock=True)
            draft = await self._draft(session, tenant)
            revisions: dict[UUID, int] = {}
            for model in (BusinessService, BusinessStaff, BusinessResource):
                for row in await session.scalars(select(model).where(model.tenant_id == tenant.id)):
                    revisions[row.id] = row.revision
            return DirectoryView(
                draft_revision=draft.revision,
                published_version=draft.published_version,
                records=OperationalConfiguration.model_validate(draft.document).directory,
                revisions=revisions,
            )

    async def write(self, context: RequestContext, command: DirectoryWrite) -> DirectoryWriteResult:
        digest = hashlib.sha256(
            json.dumps(command.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        try:
            async with self.sessions.begin() as session:
                tenant = await self._enter(session, context, Permission.BUSINESS_WRITE, lock=True)
                proof = await session.get(DirectoryMutation, command.request_id)
                if proof is not None:
                    if proof.request_hash != digest:
                        raise ConflictError()
                    return DirectoryWriteResult.model_validate(proof.result)
                draft = await self._draft(session, tenant)
                if draft.revision != command.draft_revision:
                    raise RevisionConflictError()
                model = {
                    "service": BusinessService,
                    "staff": BusinessStaff,
                    "resource": BusinessResource,
                }[command.record.kind]
                existing = await session.get(model, command.record.id)
                if (existing.revision if existing else 0) != command.expected_revision:
                    raise RevisionConflictError()
                if existing is None:
                    count = await session.scalar(
                        select(func.count()).select_from(model).where(model.tenant_id == tenant.id)
                    )
                    if count is not None and count >= (
                        500 if command.record.kind == "service" else 200
                    ):
                        raise ConflictError()
                document = OperationalConfiguration.model_validate(draft.document)
                payload = document.directory.model_dump(mode="json")
                group = {"service": "services", "staff": "staff", "resource": "resources"}[
                    command.record.kind
                ]
                payload[group] = [
                    record for record in payload[group] if record["id"] != str(command.record.id)
                ]
                payload[group].append(command.record.model_dump(mode="json"))
                snapshot = DirectorySnapshot.model_validate(payload)
                await validate_directory_references(session, tenant.id, snapshot)
                revisions = await sync_directory(
                    session, tenant.id, snapshot, changed_id=command.record.id
                )
                document.directory = snapshot
                draft.document = document.model_dump(mode="json")
                draft.revision += 1
                result = DirectoryWriteResult(
                    request_id=command.request_id,
                    draft_revision=draft.revision,
                    entity_revision=revisions[command.record.id],
                    record=command.record,
                )
                session.add(
                    DirectoryMutation(
                        id=command.request_id,
                        tenant_id=tenant.id,
                        request_hash=digest,
                        result=result.model_dump(mode="json"),
                    )
                )
                audit(
                    session,
                    context,
                    "business_directory_staged",
                    command.record.kind,
                    command.record.id,
                )
                return result
        except (IntegrityError, ValidationError):
            raise ConflictError() from None

    async def directory_catalog(self, context: RequestContext) -> dict[str, object]:
        async with self.sessions.begin() as session:
            tenant = await self._enter(session, context, Permission.BUSINESS_READ, lock=True)
            draft = await self._draft(session, tenant)
            directory = OperationalConfiguration.model_validate(draft.document).directory
            locations = (
                await session.scalars(
                    select(Location).where(Location.tenant_id == tenant.id).order_by(Location.name)
                )
            ).all()
            return {
                "schemas": {
                    "service": ServiceDefinition.model_json_schema(),
                    "staff": StaffDefinition.model_json_schema(),
                    "resource": ResourceDefinition.model_json_schema(),
                },
                "locations": [{"id": str(row.id), "name": row.name} for row in locations],
                "calendar_connections": [],
                "bindings": {
                    "location_id": "locations",
                    "staff_ids": "staff",
                    "resource_ids": "resources",
                    "user_id": "users",
                    "calendar_connection_id": "calendar_connections",
                },
                "users": [
                    {"id": str(row.id), "name": row.display_name, "is_active": row.is_active}
                    for row in await session.scalars(
                        select(User)
                        .where(
                            User.tenant_id == tenant.id,
                            User.is_active.is_(True),
                            User.accepted_at.is_not(None),
                        )
                        .order_by(User.display_name)
                    )
                ]
                if has_permission(context, Permission.USER_READ)
                else [],
                "staff": [
                    {"id": str(row.id), "name": row.name, "is_active": row.is_active}
                    for row in await session.scalars(
                        select(BusinessStaff)
                        .where(
                            BusinessStaff.tenant_id == tenant.id,
                            BusinessStaff.id.in_([row.id for row in directory.staff]),
                        )
                        .order_by(BusinessStaff.name)
                    )
                ],
                "resources": [
                    {"id": str(row.id), "name": row.name, "is_active": row.is_active}
                    for row in await session.scalars(
                        select(BusinessResource)
                        .where(
                            BusinessResource.tenant_id == tenant.id,
                            BusinessResource.id.in_([row.id for row in directory.resources]),
                        )
                        .order_by(BusinessResource.name)
                    )
                ],
                "page_sizes": [25, 50, 100],
                "calendar_unavailable_reason": "Calendar adapters are not yet verified.",
                "limits": {"services": 500, "staff": 200, "resources": 200},
            }
