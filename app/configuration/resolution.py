"""Explicit field ownership; no unrestricted deep merging or live default injection."""

from copy import deepcopy
from typing import cast
from uuid import UUID

from pydantic import BaseModel

from app.configuration.schemas import EffectiveConfigurationView, OperationalConfiguration
from app.errors import ConflictError

SCOPE_FIELDS = {
    "location": ["business.timezone", "business.address", "hours"],
    "agent": [
        "agent.greeting",
        "agent.tone",
        "agent.instructions",
        "agent.unknown_answer",
        "agent.allowed_tools",
        "agent.max_steps",
        "agent.max_tool_calls",
        "languages",
    ],
    "channel": ["agent.greeting", "voice.max_duration_minutes", "voice.transfer_number", "chat"],
}


def resolve_configuration(
    document: OperationalConfiguration,
    version: int,
    *,
    location_id: UUID | None = None,
    agent_id: UUID | None = None,
    channel_id: UUID | None = None,
) -> EffectiveConfigurationView:
    resolved = document.model_copy(deep=True)
    origins: dict[str, str] = {}
    for section in type(document).model_fields:
        if section not in {"scopes", "schema_version"}:
            value = cast(BaseModel, getattr(document, section))
            for key in type(value).model_fields:
                origins[f"{section}.{key}"] = "tenant"

    channel = document.scopes.channels.get(channel_id) if channel_id else None
    if channel is not None:
        if agent_id and channel.agent_id and agent_id != channel.agent_id:
            raise ConflictError()
        agent_id = channel.agent_id or agent_id
    agent = document.scopes.agents.get(agent_id) if agent_id else None
    bound_location = channel.location_id if channel is not None else None
    bound_location = bound_location or (agent.location_id if agent is not None else None)
    if location_id and bound_location and location_id != bound_location:
        raise ConflictError()
    location_id = bound_location or location_id
    location = document.scopes.locations.get(location_id) if location_id else None

    def source(field: str, scope: str, identity: UUID | None) -> None:
        for key in origins:
            if key == field or key.startswith(field + "."):
                origins[key] = f"{scope}:{identity}"

    if location is not None:
        if location.timezone is not None:
            resolved.business.timezone = location.timezone
            source("business.timezone", "location", location_id)
        if location.address is not None:
            resolved.business.address = location.address
            source("business.address", "location", location_id)
        if location.hours is not None:
            resolved.hours = location.hours.model_copy(deep=True)
            source("hours", "location", location_id)
    if agent is not None:
        for field in SCOPE_FIELDS["agent"]:
            section, _, key = field.partition(".")
            value = getattr(agent, key if key else section)
            if value is not None:
                if key:
                    setattr(getattr(resolved, section), key, deepcopy(value))
                else:
                    setattr(resolved, section, value.model_copy(deep=True))
                source(field, "agent", agent_id)
    if channel is not None:
        if channel.greeting is not None:
            resolved.agent.greeting = channel.greeting
            source("agent.greeting", "channel", channel_id)
        if channel.max_duration_minutes is not None:
            resolved.voice.max_duration_minutes = channel.max_duration_minutes
            source("voice.max_duration_minutes", "channel", channel_id)
        if channel.transfer_number is not None:
            resolved.voice.transfer_number = channel.transfer_number or None
            source("voice.transfer_number", "channel", channel_id)
        if channel.chat is not None:
            resolved.chat = channel.chat.model_copy(deep=True)
            source("chat", "channel", channel_id)
    # Revalidate cross-field rules after applying strictly eligible overrides.
    return EffectiveConfigurationView(
        version=version,
        document=OperationalConfiguration.model_validate(resolved.model_dump()),
        field_origins=origins,
        location_id=location_id,
        agent_id=agent_id,
        channel_id=channel_id,
    )
