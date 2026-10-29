"""The ElevenLabs voices a clinic can choose from, with their sample recordings.

Listing voices is free; the samples are recordings ElevenLabs hosts, so letting staff
listen to them makes no speech and costs nothing.
"""

import logging
from typing import Any, cast
from uuid import UUID

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, SecretStr

from app.core.settings import Settings
from app.integrations.resolver import TenantIntegrationResolver

logger = logging.getLogger(__name__)

MAX_VOICES = 100


class VoiceOption(BaseModel):
    id: str
    name: str
    description: str = ""
    preview_url: str | None = None


async def voice_key(app: FastAPI, settings: Settings, tenant_id: UUID) -> SecretStr | None:
    """The ElevenLabs key this clinic's calls use, if they use ElevenLabs at all."""
    resolver = getattr(app.state, "integration_resolver", None)
    if isinstance(resolver, TenantIntegrationResolver):
        connection = await resolver.active(tenant_id, "tts")
        if connection is not None:
            if connection.provider == "elevenlabs":
                return connection.secret("api_key")
            if connection.provider in {"language_router", "sarvam_router"}:
                return connection.secrets.get("elevenlabs_api_key")
            return None
    if settings.tts_provider == "elevenlabs":
        return settings.elevenlabs_api_key
    if settings.tts_provider in {"language_router", "sarvam_router"}:
        return settings.routed_tts_secrets.get("elevenlabs_api_key")
    return None


async def list_voices(
    key: SecretStr,
    *,
    base_url: str = "https://api.elevenlabs.io",
    client: httpx.AsyncClient | None = None,
) -> list[VoiceOption]:
    if not key.get_secret_value():
        return []
    owned = client is None
    http = client or httpx.AsyncClient(timeout=5)
    try:
        response = await http.get(
            f"{base_url}/v1/voices", headers={"xi-api-key": key.get_secret_value()}
        )
        if response.status_code != 200:
            logger.warning("voice_list_unavailable", extra={"status": response.status_code})
            return []
        body: object = response.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("voice_list_unavailable")
        return []
    finally:
        if owned:
            await http.aclose()
    raw = cast(dict[str, Any], body).get("voices") if isinstance(body, dict) else None
    voices: list[VoiceOption] = []
    for item in cast(list[object], raw) if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        voice = cast(dict[str, Any], item)
        labels = voice.get("labels")
        description = (
            ", ".join(str(value) for value in cast(dict[str, Any], labels).values() if value)
            if isinstance(labels, dict)
            else ""
        )
        identifier, name = voice.get("voice_id"), voice.get("name")
        if isinstance(identifier, str) and isinstance(name, str):
            preview = voice.get("preview_url")
            voices.append(
                VoiceOption(
                    id=identifier,
                    name=name[:100],
                    description=description[:200],
                    preview_url=preview if isinstance(preview, str) else None,
                )
            )
    return voices[:MAX_VOICES]
