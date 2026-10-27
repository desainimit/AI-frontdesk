"""The platform's speaking speed in each speech provider's own terms."""

from app.core.settings import SpeakingSpeed

# ElevenLabs voice_settings.speed: 1.0 is the voice's natural pace; it accepts 0.7 to 1.2.
ELEVENLABS_SPEED: dict[SpeakingSpeed, float] = {
    "slow": 0.85,
    "normal": 1.0,
    "fast": 1.1,
    "very_fast": 1.2,
}
# Azure SSML <prosody rate>, relative to the voice's own pace; "" leaves it as it is.
AZURE_RATE: dict[SpeakingSpeed, str] = {
    "slow": "-15%",
    "normal": "",
    "fast": "+10%",
    "very_fast": "+20%",
}
