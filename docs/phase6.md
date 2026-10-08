# Phase 6: speech pipeline

Reviewed and corrected on 2026-09-29. The pipeline converts Twilio mono 8 kHz
mu-law audio to PCM16 for recognition and returns mu-law speech over the same socket.
Mu-law conversion is lossy; it is not a zero-loss audio codec.

## Recognition and fallback

Deepgram uses a persistent authenticated WebSocket with keepalives, interim results,
and utterance-final results while the caller is connected. Final segments are joined
until endpointing or an UtteranceEnd message completes the utterance.
See the [Deepgram streaming protocol](https://developers.deepgram.com/reference/speech-to-text/listen-streaming).

Azure uses short-utterance REST recognition. The adapter detects speech in 20 ms
frames, retains a short pre-roll, and submits after 600 ms of silence or a 10 second
utterance limit. It resamples input to mono 16 kHz PCM WAV as required by the
[Azure short-audio API](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-speech-to-text-short).
It produces final results during the call, but does not provide server interim results.
Queues are bounded and worker failures reach the pipeline instead of being discarded.

STT failover closes the failed stream and replays at most two seconds of recent audio
once. A lock coordinates the sender and result consumer during a switch. This bounded
replay can lose earlier words in a long unfinished utterance; it is not seamless recovery.
Provider instances and circuit breakers are shared across calls within each API process.
A half-open circuit allows one probe at a time. Azure health currently checks configuration;
Deepgram health opens and closes an authenticated recognition stream.

## Synthesis and interruption

ElevenLabs and Azure return raw mono PCM16 at 8 kHz. Arbitrarily sized HTTP chunks
are reassembled into 20 ms frames before conversion and paced delivery to Twilio.
Invalid trailing partial samples are rejected. Azure SSML text and attributes are escaped,
and fallback synthesis uses the fallback provider's configured voice.
TTS switches providers only before audio has been delivered, avoiding a repeated partial reply.

After synthesis finishes, a Twilio mark tracks queued playback. Caller interruption
remains active until the matching acknowledgment or the bounded acknowledgment deadline.
Interruption cancels response/synthesis work and sends Twilio clear. Stale marks are ignored.
The energy detector is heuristic; microphone noise and speakerphone echo need live tuning.

## Configuration

Use `.env.example` for the complete settings list. To exercise this pipeline, set
`TELEPHONY_ENABLED=true` and `TELEPHONY_MODE=pipeline`, then configure Twilio and voice credentials.

| Setting | Supported values / purpose |
| --- | --- |
| PRIMARY_STT_PROVIDER | deepgram, azure, mock |
| FALLBACK_STT_PROVIDER | deepgram, azure, mock, none |
| PRIMARY_TTS_PROVIDER | elevenlabs, azure, mock |
| FALLBACK_TTS_PROVIDER | elevenlabs, azure, mock, none |
| DEEPGRAM_API_KEY / DEEPGRAM_MODEL / DEEPGRAM_LANGUAGE | Recognition credentials, model, language |
| ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID / ELEVENLABS_MODEL_ID | Synthesis credentials and voice |
| ELEVENLABS_OUTPUT_FORMAT | pcm_8000 only |
| AZURE_SPEECH_KEY / AZURE_SPEECH_REGION | Azure credentials and region |
| AZURE_SPEECH_LANGUAGE / AZURE_SPEECH_VOICE | Recognition language and synthesis voice |
| AZURE_SPEECH_OUTPUT_FORMAT | raw-8khz-16bit-mono-pcm only |
| STT_TIMEOUT_SECONDS / TTS_TIMEOUT_SECONDS | Provider operation timeouts |
| VOICE_CIRCUIT_BREAKER_FAILURES / VOICE_CIRCUIT_BREAKER_RESET_SECONDS | Failure threshold and recovery interval |
| BARGE_IN_ENABLED | Enables caller interruption; false disables it |
| BARGE_IN_ENERGY_THRESHOLD / BARGE_IN_CONSECUTIVE_FRAMES | Interruption sensitivity |

Unknown provider names and incompatible output codecs fail configuration validation.
`httpx` and `websockets` are production dependencies, including in the Docker image.
With `AGENT_ENABLED=false`, final recognition results feed the Phase 6 transcript-repeat
response. Enable the [Phase 8 agent](phase8.md) for generated responses.

## Verification

```powershell
.\.venv\Scripts\pytest.exe -q tests/test_voice_phase6.py tests/test_voice_regressions.py
```

Tests cover a local Deepgram-compatible WebSocket, Azure WAV requests before hangup,
background failures, concurrent failover, replay counts, chunk boundaries, SSML escaping,
circuit probes, and playback acknowledgment/interruption. Provider HTTP responses are mocked.
The full suite also exercises signed Twilio streams, PostgreSQL tenant isolation, and
conversation persistence. See [validation results](validation.md).

No live Twilio, STT, TTS, or LLM call was made during this review. Production acceptance
still requires a real call with configured providers, language and latency checks, and noise tuning.
