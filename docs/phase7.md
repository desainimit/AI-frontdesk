# Phase 7: conversation state

Conversation state tracks call lifecycle, language, identity verification progress,
active flow, collected slots, confirmation, and safety decisions. It carries context
across short utterances such as ?Dr Shah?, ?Tomorrow?, and ?Afternoon? without storing
a complete transcript.

The Phase 7 review found and fixed these issues:

- Affirmative prefix matching could confirm ?yes, don't book?. Negation now wins;
  only explicit complete affirmative replies confirm. Ambiguous changes invalidate
  the old proposal so a later ?yes? cannot approve stale parameters.
- Confirmation parameters shared mutable references. Requests and returned parameters
  now use deep copies; expired confirmations enter the expired state.
- Identity attempts could exceed the limit, and success did not require a patient ID.
  Attempt limits are now enforced and stale verification data is cleared.
- A single high-confidence language observation could change the conversation language.
  Changes now require two observations; hi-Latn normalizes to Hinglish.
- Redis trusted payload identity and treated failures as missing state. Reads now
  validate both nested call IDs and the requested tenant/call key, fail closed on
  invalid state or Redis failures, and avoid logging raw provider errors.
- Only the first save honored the configured TTL. All voice state writes now use it.
  Call completion, failure, and disconnect are reflected in conversation state.
- Transcript history and response work were unbounded. History keeps 20 results and
  final turns use a bounded queue. New turns cancel obsolete responses; closing the
  pipeline cancels and awaits its tasks.
- Docker Compose did not pass Phase 6/7 settings to the API. These are now wired.

## Storage and ownership

One accepted media stream owns each call's mutable state. Database call claiming
prevents a second stream from taking over the call. Redis is an ephemeral snapshot,
not a durable workflow journal or a cross-worker conversation lock. Reconnecting
or transferring an active conversation across workers is not implemented.

Redis keys are `tenant:<tenant_id>:call:<call_id>:conv_state`; payloads are capped at
64 KiB and expire after the configured TTL. Production/staging require Redis.
The bounded memory store is for development and tests.

## Configuration

```dotenv
CONVERSATION_STATE_TTL_SECONDS=3600
CONVERSATION_DEFAULT_LANGUAGE=en
CONVERSATION_LANGUAGE_CONFIDENCE_THRESHOLD=0.6
CONVERSATION_CONFIRMATION_TIMEOUT_SECONDS=120
```

Identity and confirmation are state primitives, not an identity-verification
integration or authorization for booking. Phase 8 exposes only public directory
reads. Appointment tools and real verification belong to later work.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_conversation_phase7.py tests/test_conversation_regressions.py
```

Real Redis TTL/isolation checks are in `tests/integration/test_agents.py`.
