# Gujarati implementation review — 2026-10-18

## Result

The earlier implementation was not complete enough to call production-ready.
Its distinction between Deepgram's explicit Gujarati recognition and automatic
multilingual recognition is supported by the provider documentation. Removing
the validation would have let users save a configuration that fails during calls.
The implementation and the configuration experience nevertheless had defects.

Read-only inspection of the development database found these saved, verified
platform connections at the start of this review:

- Recognition: Deepgram, `nova-3`, `language=multi`.
- Voice: ElevenLabs, `eleven_turbo_v2_5`.
- No workspace recognition or voice overrides.

Adding a new provider option did not change these saved connections. Neither
connection met the requirements for automatic Gujarati conversations. The user
has chosen to connect Azure Speech through the product. No credentials or saved
provider choices were silently changed during this review.

## Findings corrected

| Priority | Finding | Correction and evidence |
| --- | --- | --- |
| High | Combined platform connections stored credentials in routed profiles, but workspace readiness looked for the unrelated single Azure key. A verified combined configuration could be reported as missing. | Readiness now checks both keys of the selected combined profile. Unit tests cover missing keys and unrelated legacy keys; PostgreSQL tests save both profiles and check readiness. |
| High | Separate Azure recognition and voice connections wrote to the same key and region settings. Loading the second connection could make the first use another account's credentials or region. | Purpose-specific keys and regions now remain independent. Regression coverage checks both row orders and removal of one connection. Legacy direct Settings construction remains supported. |
| High | Workspace activation checked connection availability but did not recheck language capabilities after a provider change. | Readiness now validates the saved language policy against current connection metadata, without decrypting secrets or opening nested database sessions. The database regression downgrades a saved compatible setup and verifies a blocking language check. Call-start validation remains in place. |
| Medium | Save validation stopped at the first speech problem and attached a generic combined-provider instruction to `languages`, making the main-language selector look wrong. | Validation reports recognition and voice problems separately, identifies platform versus workspace ownership, and assigns them to speech connections. Tests cover both failures and a compatible workspace override. |
| Medium | Frontend guidance implied that selecting Gujarati enabled switching, and unnecessarily recommended Azure for all voice configurations. | Guidance now distinguishes language preferences from provider setup, names the actual settings location, and includes single-provider ElevenLabs v3 as a voice option. |

## Reviewed behavior and remaining limits

- Recognition routing is call-local. Automatic mode identifies each completed
  utterance with Azure and then sends it to Deepgram: `gu` for Gujarati, `multi`
  for the other enabled languages. Fixed-language mode bypasses detection.
- This is two sequential recognition requests per utterance with the default
  Deepgram option. It adds cost and delay and is not continuous interim streaming.
  There is no measured live latency result in this review.
- A caller switching between utterances is covered by mocked provider tests.
  Gujarati/Hindi mixing within one sentence, dialects, noisy telephone audio,
  names and spoken numbers still need real audio evaluation. Dominant-language
  detection is not proof of reliable word-by-word code switching.
- Combined voice routing intentionally uses Azure for Gujarati and ElevenLabs
  for other languages. A single ElevenLabs v3 connection is an alternative if
  using the same voice provider for every language is preferred.
- Account verification checks credentials. It does not prove Azure Fast
  Transcription availability for that account/region or measure speech quality.
- Recognition failure recovery remains limited: an STT error exits the utterance
  worker, then the pipeline consumer fails the call. There is no automatic retry
  or alternate-provider recovery in this route. The earlier description of a
  failed recognition turn should not be read as a promise that the call continues.
- Reviewed Gujarati locale aliases, language observations, prompts, server
  wording, appointment confirmation, digit normalization, transcript redaction
  and safety-rule changes. Automated tests provide regression evidence; they do
  not establish exhaustive Gujarati translation or safety accuracy. Native
  speaker review and real-model evaluation remain required.
- Provider runtime checks still prevent incompatible calls after provider changes.
  Readiness is recomputed; changing a provider does not automatically deactivate
  an already-live workspace.

## Required configuration

In **Platform settings → Providers and email**:

1. **Understanding callers:** choose **Automatic: Deepgram + Azure Speech**.
   Keep **Gujarati recognition = Deepgram Nova 3**. Supply the Deepgram key and
   Azure Speech key/region, then **Save and check**.
2. **The receptionist's voice:** choose **Automatic: ElevenLabs + Azure Speech**.
   Supply the ElevenLabs key and voice ID plus Azure Speech key/region, then
   **Save and check**. Alternatively select a single ElevenLabs connection with
   the Eleven v3 model, or Azure Speech.
3. In receptionist settings, retain English as main language, enable Gujarati,
   leave automatic switching on, and save. A workspace-owned connection, when
   present, overrides its platform connection for that purpose.
4. Make a real Twilio call that switches English → Gujarati → Hindi → English.
   Check reply language, appointment corrections, interruptions and response delay.

## Verification

- Focused speech/Gujarati/provider regression tests: 125 passed.
- Broader backend suite outside the integration directory: 379 passed. One
  existing unawaited AsyncMock warning remains in the media-pipeline test.
- PostgreSQL speech, settings, platform-connection and integration tests: 23 passed
  using a fresh disposable database and restricted roles; resources were removed.
- Frontend: 55 tests passed; ESLint and production build passed.
- Backend Ruff and Pyright passed. Frontend OpenAPI contract matches 130 paths.
- Local Docker API and worker rebuilt and healthy. API readiness reports PostgreSQL
  and Redis healthy; an in-container import confirms the updated configuration code.
- Speech HTTP in these tests is mocked. No live Gujarati provider transcription,
  synthesis, native-speaker evaluation or Twilio call is claimed.

## Provider references

- [Deepgram model language matrix](https://developers.deepgram.com/docs/models-languages-overview): Gujarati appears as an explicit recognition language, but not in Nova 3's documented multilingual list.
- [Azure Fast Transcription](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/fast-transcription-create): multiple locale candidates enable language identification; identification selects a main language for an audio file.
- [ElevenLabs models](https://elevenlabs.io/docs/overview/models): the selected Turbo v2.5 model's language list does not include Gujarati; Eleven v3 supports it.
