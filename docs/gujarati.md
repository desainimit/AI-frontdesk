# Gujarati conversations

See the [2026-10-18 implementation review](gujarati-implementation-review.md) for
configuration findings, readiness fixes, current verification and remaining live-call checks.

Gujarati is an opt-in conversation language, stored as `gu`. Receptionist language
settings also accept `guj`, `gu-IN`, and `gu_IN` and store them as `gu`. Existing
clinics retain their enabled languages. Gujarati can be enabled alongside English,
Hindi, Spanish, French, German, and Portuguese without changing the main language
or disabling automatic switching. Staff dashboard translation is separate.

## Configure a clinic

### Keep Deepgram and ElevenLabs, with automatic Gujarati routing

In **Platform > Settings > Providers and email**, select these combined options:

| Card | Provider | Enter |
| --- | --- | --- |
| Understanding callers | Automatic: Deepgram + Azure Speech | Deepgram API key, Azure Speech key and region |
| The receptionist's voice | Automatic: ElevenLabs + Azure Speech | ElevenLabs API key, voice ID and model, Azure Speech key and region |

Use **Save and check** on both cards. Both accounts in each combined connection
must verify. Workspace-owned connections offer the same options and override the
platform connection for their purpose. Credentials are encrypted together; when
rotating credentials in a combined connection, submit both keys again.

Then enable Gujarati alongside English and other languages under the receptionist
settings, keep your preferred main language, turn **Switch to the caller's
language** on, and save.

Azure identifies the language of **every completed utterance** from audio. By default,
Deepgram Nova 3 then transcribes Gujarati with `language=gu`; other enabled languages
use `language=multi`. Set **Gujarati recognition** to **Azure Speech** to reuse
Azure's Gujarati transcript instead. With the default Deepgram choice, all spoken
audio goes to **both services**, adding recognition charges and latency. This
deliberately rechecks every turn so
the previous turn's language cannot hide a switch into or out of Gujarati.
Detection failures fail the recognition turn rather than guessing the language.
This is endpointed recognition, without server-side interim streaming.

Gujarati replies use Azure's Dhwani voice; the other reply languages use the
configured ElevenLabs voice/model. A voice change is audible. The existing stable
conversation-language rules still apply, including protection against switching
on short ambiguous confirmations. Fixed-language mode bypasses detection and
sends audio to only the configured language's recognition service. Fixed Gujarati
can therefore use Deepgram directly without an Azure detection request.

These are optional combined connections. Existing single-provider connections
remain available. Live latency, recognition quality and provider entitlements
must still be checked with real calls.

### Use a single recognition or voice provider

1. Keep your preferred main language, such as English. Under Settings ? AI
   receptionist, enable Gujarati alongside the other languages and leave
   "Switch to the caller's language" on. The main language controls the initial
   conversation language; it does not restrict which enabled languages callers use.
2. For automatic voice switching that includes Gujarati, connect and verify **Azure
   Speech recognition** in Platform ? Settings or the clinic's provider settings.
   Each completed utterance is sent to Azure Fast Transcription with the clinic's
   enabled locale candidates, including `gu-IN`. The language is identified from
   audio, independently of the greeting or previous turn. This uses bounded
   endpointed audio, not server-side interim streaming. Select a region/account
   that supports Fast Transcription and evaluate response delay with real calls.
3. For speech output, use either:
   - Azure Speech: Gujarati replies use `gu-IN-DhwaniNeural`.
   - ElevenLabs: select **Eleven v3**. Existing Turbo 2.5 and Multilingual v2
     models do not support Gujarati. Your saved provider account is preserved;
     the application does not silently switch credentials or models.
4. Save. With calls enabled, the backend checks effective provider capabilities.
   It does not require Gujarati as the main language or automatic switching off.
   Unsupported provider combinations return a field error without changing the
   saved settings or version. If using Gujarati for text only, calls may be disabled.
   Runtime checks the speech pair again before a call starts.

The clinic's verified connection takes precedence over the platform account for
that purpose. Candidate languages, transcripts, and conversation language remain
call-local; nothing changes shared provider settings or another clinic's language.

Example receptionist policy, matching an English-first multilingual clinic:

```json
{
  "default_language": "en",
  "enabled_languages": ["en", "es", "fr", "de", "pt", "hi", "gu"],
  "auto_detect": true
}
```

## Deepgram compatibility

Deepgram remains available for its existing recognition modes. Nova-3 with explicit
`language=gu` supports Gujarati-only recognition. Its documented `multi` mode and
language-detection list do not include Gujarati. Therefore automatic Gujarati
recognition alongside the other languages requires Azure Speech in this implementation.
Changing the main language, removing validation, or guessing from a wrongly decoded
transcript cannot provide missing audio recognition. No parallel guessed transcripts
or unrequested provider fallback are used.

Azure detects the dominant language of each bounded utterance. Switching between
utterances is supported; accurate Gujarati?Hindi mixing within one sentence remains
a separate provider-quality evaluation, not an acceptance claim.

## Conversation behavior

- Gujarati script is used for AI replies. Unicode vowel marks and Gujarati digits
  survive safety normalization. Dates and numbers that remain ambiguous must be
  clarified; a prompt is not proof of real recognition accuracy.
- Gujarati-digit access codes are converted to ASCII before strict eight-digit
  validation. Codes and common identifiers using Gujarati digits are redacted from
  transcripts, and access-code-only turns do not reach the semantic classifier.
- Server-owned confirmations, failures, handoff messages, and safety messages have
  Gujarati translations. Appointment confirmations use Gujarati calendar wording
  in the provider's timezone, including the year and time of day.
- Only explicit affirmative phrases can approve the current appointment proposal.
  Refusals and corrections cannot authorize a change, and a later yes cannot approve
  an invalidated proposal. Existing identity, tenant isolation, and tool permissions
  remain authoritative.
- Deterministic safety rules include Gujarati risk phrases, historical/informational
  context, symptom denials, and requests for staff. This vocabulary is not exhaustive
  and must be evaluated with native speakers before broad rollout.
- Greetings, clinic instructions, unknown-answer wording, and notification templates
  are clinic-authored content. Configure them in Gujarati as appropriate. Scheduled
  notification templates and their date formatting are not automatically translated.

## Validation and rollout

Offline coverage is in `tests/test_gujarati.py`, with PostgreSQL coverage in
`tests/integration/test_settings.py` and `tests/integration/test_assistant.py`.
Speech provider HTTP is mocked. Database tests use separate restricted roles and
must run against an isolated disposable database, never the development database.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_gujarati.py -q
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m pyright
```

From the sibling frontend:

```powershell
& '..\AI frontdesk\.venv\Scripts\python.exe' scripts/check-openapi.py
npm run test
npm run typecheck
npm run lint
npm run build
```

After updating a local Docker installation, rebuild its application services:

```powershell
docker compose up -d --build api worker
```

Before enabling a clinic for callers, make real signed Twilio calls with Gujarati
speakers while retaining English as the main language and automatic switching on. Verify first-utterance recognition, names, Gujarati/ASCII numbers, dates,
appointment corrections, refusals, interruptions, safety escalation, provider
failures, and pronunciation. Measure end-to-end response delay and cost. Neither
mocked speech checks nor a scripted-model booking prove live speech or model quality.

Provider references checked on 2026-10-18:

- [Deepgram models and language support](https://developers.deepgram.com/docs/models-languages-overview)
- [Azure Speech language and voice support](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support)

## Verification boundary

On 2026-10-18, the backend passed 340 tests outside the integration group, Ruff,
and Pyright. Another 33 settings, telephony, provider, and assistant integration
tests passed on a newly initialized isolated PostgreSQL database; its test roles
and database were removed afterwards. The frontend passed 55 tests, TypeScript,
ESLint, and its production build. Two warnings in existing backend test fixtures
remain (pytest collection and an unawaited mock).

Automated tests cover English as main language with all seven languages enabled,
Gujarati detection from provider audio-language evidence, stable switching back to
English, call-local recognition candidates, Azure multipart requests, compatible
ElevenLabs v3 speech, save/reload behavior, and Gujarati appointment consent.
Provider HTTP is mocked and the AI model is scripted in database booking tests.
Live speech-provider calls, native-speaker translation/pronunciation review,
live-model Gujarati quality, and real Gujarati Twilio calls remain unverified.
Running Docker services are not rebuilt by these checks.
