# Phase 10 — Global multilingual calls

The launch supports **English (`en`), Spanish (`es`), French (`fr`), German (`de`), Portuguese (`pt`), and Hindi (`hi`)**, with Hindi/English code switching. Language behavior is configured per tenant and stays stable during short replies. The appointment domain, access checks, and registered tools remain the existing backend; multilingual support does not add new shop-specific workflows.

## Apply the migration and enable

Migration `0008_tenant_languages` adds tenant language policy storage. Apply it and refresh runtime grants before starting the updated API:

```powershell
docker compose run --rm --build migrate
```

For host Python with your local connection URLs in `.env`, use:

```powershell
.\.venv\Scripts\python.exe -m app.database.initialize
```

The initialization job upgrades the schema, provisions restricted roles, and performs the existing super-admin bootstrap. It uses your installed PostgreSQL server; no PostgreSQL image is needed.

Set these values in `.env`, keeping the existing provider credentials:

```dotenv
MULTILINGUAL_ENABLED=true
PRIMARY_STT_PROVIDER=deepgram
DEEPGRAM_MODEL=nova-3
DEEPGRAM_LANGUAGE=multi
ELEVENLABS_MODEL_ID=eleven_turbo_v2_5
CONVERSATION_DEFAULT_LANGUAGE=en
```

Then rebuild/restart the API:

```powershell
docker compose up --build -d api
```

The flag defaults to `false`; tenant language policies apply to calls when it is enabled. Startup rejects incompatible STT and ElevenLabs model settings. Configure the existing agent as described in [Phase 9](phase9.md) for appointment conversations; without the agent, the pipeline speaks back the transcript.

## Tenant language settings

A tenant admin can call `PUT /api/v1/tenants/current/languages` with their tenant bearer token:

```json
{
  "default_language": "es",
  "enabled_languages": ["en", "es", "fr", "de", "pt", "hi"],
  "auto_detect": true
}
```

Use canonical language codes above, not regional codes. The default must be in the enabled list. Set `auto_detect` to `false` to keep the call in the tenant's default language. `GET /api/v1/tenants/current` returns the policy. Updates require `tenant:update` (tenant admin), are audited, and are protected by existing tenant isolation. Platform administrators use the existing platform administration flow; a platform token is not a tenant API token.

Policies are read at call start and snapshotted in conversation state, so changes affect new calls. Existing tenants with an empty policy use the global `CONVERSATION_DEFAULT_LANGUAGE` and all six enabled languages. The tenant API reports the same effective default used by new calls.

## Detection and speech

- Deepgram's trained speech model supplies word-level language labels and an ordered `alternative.languages` list. The adapter aggregates final segments up to the utterance boundary. Configured `multi` or a fixed recognition language is never presented as a detected language. See [Deepgram code switching](https://developers.deepgram.com/docs/multilingual-code-switching) and [supported models/languages](https://developers.deepgram.com/docs/models-languages-overview).
- When speech language evidence is absent, [Lingua](https://github.com/pemistahl/lingua-py) classifies text among the six launch languages using confidence and separation from the next candidate. There are no Hindi/English vocabulary sets for language detection. Unlabeled Roman Hindi and unsupported languages can still be misclassified by text-only detection; it is a fallback, not a universal language identifier.
- Short or repeated replies, names, and access codes do not change language. A substantive first observation can establish it immediately; later switches need two consecutive observations. Disabled or unsupported provider language labels leave the current language unchanged. Transcript accuracy confidence is separate from language confidence.
- The agent receives the stable reply language. Server-owned appointment proposals, confirmations, results, and errors are translated for every launch language. Consent uses exact localized replies such as yes, sí, oui, ja, sim, and हाँ; these fixed consent phrases authorize a specific pending operation and do not detect language. Revised or ambiguous answers discard the proposal, so a later yes cannot approve stale details.
- ElevenLabs receives the selected language code; Hinglish leaves it unset for multilingual pronunciation. Use a multilingual voice appropriate for your callers. Azure TTS selects a matching voice using the [Azure voice catalog](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=tts). Default locales are `en-US`, `es-ES`, `fr-FR`, `de-DE`, `pt-BR`, and `hi-IN`; region-specific tenant voice selection is not included.
- Azure STT fallback now follows each call's stable language, including changes during the call. It remains fixed-language recognition for each utterance: automatic acoustic code switching during a Deepgram outage is not equivalent to the primary provider. Before the first language observation, it uses the tenant default.

## Verify

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_multilingual_phase10.py tests/test_global_languages.py -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\pyright.exe
```

The disposable-database integration suite additionally tests tenant RBAC, audit records, call policy isolation, and actual booking transactions for all six confirmation languages. See [validation setup](validation.md) before enabling database integration tests.

Provider request tests use mocked HTTP/WebSocket responses. Real speech recognition, accents, pronunciation, and call latency still need live acceptance calls with your configured credentials in each target language. Test a mid-call switch, a short acknowledgment, a revised confirmation, and provider failover before launch.
