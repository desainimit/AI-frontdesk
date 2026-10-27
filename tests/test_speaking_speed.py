"""The platform's speaking speed reaches each speech provider. No real requests."""

import json

import httpx
import pytest
from pydantic import SecretStr

from app.core.settings import SpeakingSpeed
from app.platform.connections import ProductSettings, platform_values
from app.platform.models import PlatformSettings
from app.voice.tts.azure import AzureTTSProvider
from app.voice.tts.elevenlabs import ElevenLabsTTSProvider


def recorder() -> tuple[list[httpx.Request], httpx.AsyncClient]:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"\x00\x00")

    return requests, httpx.AsyncClient(transport=httpx.MockTransport(respond))


@pytest.mark.asyncio
@pytest.mark.parametrize(("speed", "value"), [("slow", 0.7), ("normal", 0.8), ("very_fast", 1.0)])
async def test_elevenlabs_is_asked_for_the_chosen_pace(speed: SpeakingSpeed, value: float) -> None:
    requests, client = recorder()
    async with client:
        provider = ElevenLabsTTSProvider(SecretStr("test"), client=client, speed=speed)
        _ = [chunk async for chunk in provider.synthesize("Hello", language="en")]
    assert json.loads(requests[0].content)["voice_settings"]["speed"] == value
    # Audio made at another pace is never replayed for this one.
    assert provider.voice_key.endswith(f":{value}")


@pytest.mark.asyncio
async def test_azure_slows_down_with_prosody_and_leaves_very_fast_at_its_own_pace() -> None:
    bodies: dict[str, str] = {}
    for speed in ("slow", "very_fast"):
        requests, client = recorder()
        async with client:
            provider = AzureTTSProvider(SecretStr("test"), client=client, speed=speed)
            _ = [chunk async for chunk in provider.synthesize("Hello & bye", language="en")]
        bodies[speed] = requests[0].content.decode()
    assert '<prosody rate="-30%">Hello &amp; bye</prosody>' in bodies["slow"]
    assert "prosody" not in bodies["very_fast"]


def test_the_administrators_choice_becomes_the_platform_setting() -> None:
    product = PlatformSettings(
        product_name="AI Frontdesk",
        support_email="",
        allowed_llm_models=["gemini-3.8-flash"],
        session_minutes=30,
        speaking_speed="slow",
    )
    assert platform_values([], product)["speaking_speed"] == "slow"
    assert ProductSettings.model_validate(product).speaking_speed == "slow"
    with pytest.raises(ValueError):
        ProductSettings.model_validate(
            {
                "product_name": "x",
                "allowed_llm_models": ["m-1"],
                "session_minutes": 30,
                "speaking_speed": "turbo",
            }
        )
