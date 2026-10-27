"""The platform's speaking speed in each speech provider's own terms.

"Normal" is a calm receptionist's pace, well under the voices' own: at their own pace
(1.0), and then at 0.9, callers found the receptionist too fast to follow (27 Oct).
"""

from app.core.settings import SpeakingSpeed

# ElevenLabs voice_settings.speed: 1.0 is the voice's own pace; it accepts 0.7 to 1.2.
ELEVENLABS_SPEED: dict[SpeakingSpeed, float] = {
    "slow": 0.7,
    "normal": 0.8,
    "fast": 0.9,
    "very_fast": 1.0,
}
# Azure SSML <prosody rate>, relative to the voice's own pace; "" leaves it as it is.
AZURE_RATE: dict[SpeakingSpeed, str] = {
    "slow": "-30%",
    "normal": "-20%",
    "fast": "-10%",
    "very_fast": "",
}
