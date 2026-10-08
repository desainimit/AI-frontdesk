# Phase 12 — Safety engine

Phase 12 checks every final caller utterance before appointment confirmation, agent prompts, or tools. It normalizes Unicode and speech punctuation, applies explicit emergency and human-request rules, examines a short symptom context, and optionally asks the configured OpenAI agent provider for a structured safety classification. A semantic result can add an alarm but cannot cancel a deterministic emergency result. A classifier timeout preserves the rule decision. One-time call access codes are excluded from semantic review.

A safety decision updates the persisted `SafetyState`, clears a pending appointment proposal, and blocks normal agent work for that turn. Emergency and transfer decisions remain active for the call. The emergency response is fixed and localized for English, Spanish, French, German, Portuguese, Hindi, and Hinglish. It tells the caller to contact **their local** emergency services; the application does not guess a global emergency number or provide medical instructions. The generic instruction to seek emergency help for serious symptoms follows [WHO guidance](https://extranet.who.int/prequal/sites/default/files/document_files/Day2_Session5_ClinicalManagement.pdf). Speech recognition errors and uncommon phrasing still require real-call evaluation and rule review.

## Configure human transfer

Run the migrations and restricted-role provisioning. This phase adds `tenants.human_transfer_number`, which is nullable and validated as an E.164 number. A tenant admin configures it through the authenticated API:

```http
PUT /api/v1/tenants/current/transfer
Authorization: Bearer <tenant-admin-token>
Content-Type: application/json

{"number":"+15551234567"}
```

Send `{"number":null}` to disable transfer. The destination is loaded from the verified call's tenant and is refused if it equals the incoming frontdesk number. Without a destination, callers asking for a person hear that live transfer is unavailable. The assistant never says a transfer succeeded unless Twilio accepted the call update. For a configured destination, the active call is redirected to Twilio `<Dial>` through the [Calls update API](https://www.twilio.com/docs/voice/api/call-resource#update-a-call); if the destination does not answer, Twilio plays a fallback message. The request uses the existing Twilio account credentials, a fixed API host, and a server-validated destination.

`SAFETY_SEMANTIC_ENABLED=true` (default) enables the secondary classifier when the live agent uses the OpenAI provider. `SAFETY_CLASSIFIER_TIMEOUT_SECONDS=2` bounds its latency. Deterministic rules remain active even with the mock provider or when the semantic classifier is disabled. Safety decisions and transfer outcomes emit audit events and low-cardinality `emergency_detection_total` / `human_transfer_total` metrics without storing raw transcripts in audit rows.

## Verify

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_safety_phase12.py -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\pyright.exe
```

The unit suite covers direct English/Hindi/Hinglish emergencies, basic Spanish/French/German/Portuguese phrases, negated and historical symptoms, scheduling false positives, contextual worsening, classifier failures, pending-confirmation cancellation, and a mocked Twilio call update. The disposable-PostgreSQL test in `tests/integration/test_safety.py` checks admin authorization, tenant isolation, transfer lookup, and auditing. It requires the same full migration prerequisites as Phase 11, including the pgvector server extension in the local PostgreSQL installation. Test actual STT/TTS and transfer routing with each tenant's approved destination before depending on it in live calls.
