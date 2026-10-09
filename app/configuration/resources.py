"""Tenant-owned scope identities. Behavioral assignments remain published configuration."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.auth.context import RequestContext
from app.auth.policies import Permission
from app.configuration.models import ConfigurationResource
from app.configuration.schemas import Section
from app.configuration.sections import authority
from app.configuration.service import ConfigurationService
from app.core.errors import ConflictError, NotFoundError
from app.rbac.service import RevisionConflictError, audit


class CreateConfigurationResource(Section):
    id: UUID
    kind: Literal["agent", "channel"]
    channel_kind: Literal["voice", "chat"] | None = None
    name: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_kind(self) -> "CreateConfigurationResource":
        if (self.kind == "channel") != (self.channel_kind is not None):
            raise ValueError("Select a supported kind for channels only")
        return self


class UpdateConfigurationResource(Section):
    revision: int = Field(ge=1, strict=True)
    name: str = Field(min_length=1, max_length=200)
    is_active: bool


class ConfigurationResourceView(Section):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    kind: Literal["agent", "channel"]
    channel_kind: Literal["voice", "chat"] | None
    name: str
    revision: int
    is_active: bool


class ConfigurationResourceService(ConfigurationService):
    async def create(
        self, context: RequestContext, command: CreateConfigurationResource
    ) -> ConfigurationResourceView:
        digest = hashlib.sha256(
            json.dumps(command.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        try:
            async with self.sessions.begin() as session:
                await self._enter(
                    session,
                    context,
                    authority("agent" if command.kind == "agent" else "voice", "update", context),
                    lock=True,
                )
                existing = await session.get(ConfigurationResource, command.id)
                if existing is not None:
                    if existing.creation_hash != digest:
                        raise ConflictError()
                    return ConfigurationResourceView.model_validate(existing)
                count = await session.scalar(
                    select(func.count())
                    .select_from(ConfigurationResource)
                    .where(
                        ConfigurationResource.tenant_id == context.tenant_id,
                        ConfigurationResource.kind == command.kind,
                    )
                )
                if count is not None and count >= 200:
                    raise ConflictError()
                row = ConfigurationResource(
                    tenant_id=context.tenant_id, creation_hash=digest, **command.model_dump()
                )
                session.add(row)
                await session.flush()
                audit(
                    session,
                    context,
                    "configuration_resource_created",
                    "configuration_resource",
                    row.id,
                )
                return ConfigurationResourceView.model_validate(row)
        except IntegrityError:
            raise ConflictError() from None

    async def update(
        self, context: RequestContext, identity: UUID, command: UpdateConfigurationResource
    ) -> ConfigurationResourceView:
        try:
            async with self.sessions.begin() as session:
                await self._enter(session, context, Permission.TENANT_READ, lock=True)
                row = await session.get(ConfigurationResource, identity)
                if row is None:
                    raise NotFoundError()
                authority("agent" if row.kind == "agent" else "voice", "update", context)
                if row.revision != command.revision:
                    raise RevisionConflictError()
                row.name, row.is_active = command.name, command.is_active
                row.revision += 1
                audit(
                    session,
                    context,
                    "configuration_resource_updated",
                    "configuration_resource",
                    row.id,
                )
                await session.flush()
                return ConfigurationResourceView.model_validate(row)
        except IntegrityError:
            raise ConflictError() from None
