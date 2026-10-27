"""The platform's speaking speed in each speech provider's own terms.

"Normal" is a calm receptionist's pace, a little under the voices' own: at their own
pace (1.0) callers found the receptionist too fast to follow (27 Oct).
"""

from app.core.settings import SpeakingSpeed

# ElevenLabs voice_settings.speed: 1.0 is the voice's own pace; it accepts 0.7 to 1.2.
ELEVENLABS_SPEED: dict[SpeakingSpeed, float] = {
    "slow": 0.8,
    "normal": 0.9,
    "fast": 1.0,
    "very_fast": 1.1,
}
# Azure SSML <prosody rate>, relative to the voice's own pace; "" leaves it as it is.
AZURE_RATE: dict[SpeakingSpeed, str] = {
    "slow": "-20%",
    "normal": "-10%",
    "fast": "",
    "very_fast": "+10%",
}
