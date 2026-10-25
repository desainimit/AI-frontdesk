"""Persist both speech accounts through platform and workspace connection contracts."""

from httpx import ASGITransport, AsyncClient
from pydantic import JsonValue

from app.core.settings import Settings
from app.main import create_app
from app.settings.schemas import SettingsSection
from app.settings.service import SettingsService
from app.tenants.readiness import TenantReadinessService
from app.voice.factory import platform_stt, platform_tts, stt_from_connection, tts_from_connection
from app.voice.routing import RoutedSTTProvider, RoutedTTSProvider
from app.voice.stt.deepgram import DeepgramSTTProvider
from tests.fakes import FakeDependency
from tests.integration.identity_harness import IdentityHarness
from tests.integration.test_integrations import FakeVerifier, Harness
from tests.integration.test_platform_settings import platform as platform
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy

LANGUAGES: dict[str, JsonValue] = {
    "default_language": "en",
    "enabled_languages": ["en", "hi", "gu"],
    "auto_detect": True,
}


async def test_platform_saves_both_accounts_and_accepts_english_first_gujarati_switching(
    tenancy: TenancyFixture,
    platform: Settings,
) -> None:
    admin = await IdentityHarness(tenancy).admin()
    app = create_app(platform, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        app.state.integration_verifier = FakeVerifier()
        for purpose, other in (("stt", "deepgram"), ("tts", "elevenlabs")):
            secrets = {"speech_key": "azure-key-" * 4, f"{other}_api_key": f"{other}-key-" * 4}
            saved = await client.put(
                f"/api/v1/platform/connections/{purpose}",
                headers=admin.headers,
                json={"provider": "language_router", "config": {}, "secrets": secrets},
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["status"] == "verified"
            if purpose == "stt":
                assert saved.json()["config"]["gujarati_recognition"] == "deepgram"
            assert all(secret not in saved.text for secret in secrets.values())
        assert isinstance(platform_stt(platform), RoutedSTTProvider)
        assert isinstance(platform_tts(platform), RoutedTTSProvider)
        before = (await client.get("/api/v1/settings", headers=tenancy.a.headers)).json()
        saved = await client.put(
            "/api/v1/settings/receptionist",
            headers=tenancy.a.headers,
            json={**before["settings"]["receptionist"], "languages": LANGUAGES},
        )
        assert saved.status_code == 200, saved.text
        reloaded = (await client.get("/api/v1/settings", headers=tenancy.a.headers)).json()
        assert reloaded["settings"]["receptionist"]["languages"] == LANGUAGES
        readiness = TenantReadinessService(tenancy.database.session_factory, platform)
        view = await readiness.readiness(tenancy.context(tenancy.a))
        checks = {check.key: check for check in view.checks}
        assert checks["speech_to_text"].status == "ready"
        assert checks["text_to_speech"].status == "ready"
        assert "speech_languages" not in checks
        # A provider change must invalidate readiness for the already saved policy.
        downgraded = await client.put(
            "/api/v1/platform/connections/stt",
            headers=admin.headers,
            json={
                "provider": "deepgram",
                "config": {"model": "nova-3", "language": "multi"},
                "secrets": {"api_key": "deepgram-key-" * 4},
            },
        )
        assert downgraded.status_code == 200, downgraded.text
        checks = {
            check.key: check
            for check in (await readiness.readiness(tenancy.context(tenancy.a))).checks
        }
        assert checks["speech_languages"].status == "blocking"
        # Removing the combined profile clears both keys from the runtime settings.
        assert (
            await client.delete("/api/v1/platform/connections/stt", headers=admin.headers)
        ).status_code == 204
        assert platform.routed_stt_secrets == {} and platform.routed_stt_config == {}
        assert platform.routed_tts_secrets


async def test_workspace_combined_connections_are_verified_and_isolated(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    owner = tenancy.context(tenancy.a)
    for purpose, other in (("stt", "deepgram"), ("tts", "elevenlabs")):
        await harness.connect(
            owner,
            purpose,
            "language_router",
            {"speech_key": "azure-key-" * 4, f"{other}_api_key": f"{other}-key-" * 4},
        )
    stt = await harness.resolver.require(tenancy.a.tenant_id, "stt")
    tts = await harness.resolver.require(tenancy.a.tenant_id, "tts")
    service = SettingsService(tenancy.database.session_factory, harness.settings)
    before = (await service.get(owner)).settings.receptionist.model_dump(mode="json")
    saved = await service.save(
        owner, SettingsSection.RECEPTIONIST, {**before, "languages": LANGUAGES}
    )
    policy = saved.settings.receptionist.languages
    recognition = stt_from_connection(harness.settings, stt, language_policy=policy)
    assert isinstance(recognition, RoutedSTTProvider)
    assert recognition.enabled_languages == ("en", "hi", "gu")
    assert isinstance(recognition.gujarati, DeepgramSTTProvider)
    assert recognition.gujarati.language == "gu"
    assert isinstance(tts_from_connection(harness.settings, tts), RoutedTTSProvider)
    for purpose in ("stt", "tts"):
        assert await harness.resolver.active(tenancy.b.tenant_id, purpose) is None
    assert (
        await service.get(tenancy.context(tenancy.b))
    ).settings.receptionist.languages.enabled_languages != list(recognition.enabled_languages)
