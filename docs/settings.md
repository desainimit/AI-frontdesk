# Clinic settings, default data and platform accounts

## Clinic settings

Each clinic has one settings record (`tenant_settings`), made of sections. A section is
saved on its own and takes effect at once; there is no draft and nothing to publish.

| Section | What it holds |
| --- | --- |
| `clinic` | Name, description, contact details, address, time zone, website. The name is also the workspace name. |
| `hours` | Opening hours per weekday, holidays, and what happens to calls outside opening hours. |
| `receptionist` | Greeting, tone, instructions, what to say when it does not know, whether it may book and answer from knowledge, languages. |
| `calls` | Whether calls are answered, the longest call, and the number callers are transferred to. |
| `scheduling` | Booking rules for patients: notice, how far ahead, change and cancellation notice, usual appointment length. |
| `notifications` | Whether bookings are confirmed and reminded, when the reminder goes out, and the wording of confirmation, reminder and cancellation. |
| `privacy` | How long call transcripts are kept (the worker deletes older ones every hour), what a matching phone number is enough for, what staff check before giving a caller a code, and whether a calendar entry names the patient. Calls are not recorded. |
| `tickets` | Ticket categories (used from Phase 5). |

- `GET /api/v1/settings` returns every section with a `version` that goes up by one on each save.
- `PUT /api/v1/settings/{section}` saves one section. A refused value answers 422 with
  `error.fields`: which input and why, in plain words. Saving unchanged values does nothing.
- `GET /api/v1/settings/history` lists who changed what, field by field (`{items, next_cursor}`).

The receptionist reads the settings when a call starts; a call in progress keeps the
wording it started with. Reading needs `settings:read`, saving `settings:update`.

## Schedule

- **Appointment types** (`/api/v1/services`): a name and a length. The length is the slot size.
- **Providers** have a weekly schedule (`GET`/`PUT /api/v1/providers/{id}/schedule`) in
  the clock of their location, and days off (`POST`/`DELETE .../time-off`). A new
  provider starts with the clinic's opening hours.
- **Slots** (`GET /api/v1/providers/{id}/slots?starts_at=&ends_at=&service_id=`) are the
  weekly schedule, minus days off and clinic holidays, minus existing bookings.
  A booking outside working hours is refused. The clinic's notice rules apply to
  patients booking through the receptionist; staff may book any free time.

## What a new clinic starts with

Defined in `app/settings/defaults.py` and applied when the clinic is created:

- Opening hours Monday to Friday 09:00-18:00 and Saturday 09:00-13:00.
- A greeting with the clinic's name, receptionist instructions (no medical advice),
  booking and knowledge answers on, English.
- Calls answered, 20 minutes at most. One hour's notice to book, 60 days ahead, two
  hours' notice to change or cancel.
- Confirmation and reminder wording, reminders 24 hours before.
- Two appointment types: "New patient visit" (30 minutes) and "Follow-up visit" (15).
- Four roles: Owner, Admin, Front desk, Viewer (`app/rbac/catalog.py`). Roles can be
  edited and new ones added; a member's access is always their role's permissions.

## Lists

Every list answers `{"items": [...], "next_cursor": "..." | null}`. Send `next_cursor`
back as `cursor` to get the next page; `limit` is 1 to 100.

## Platform accounts

A platform administrator manages these under `/api/v1/platform`:

| Route | Purpose |
| --- | --- |
| `GET /connections` | Email, phone service, speech to text, text to speech and AI model, each with its status. |
| `PUT /connections/{purpose}` | Save the account and check it with the provider in the same step. Leaving `secrets` out keeps the saved secret. |
| `POST /connections/{purpose}/verify`, `DELETE /connections/{purpose}` | Check again, or remove. |
| `POST /connections/email/test` | Send a test email. |
| `GET`/`PUT /settings` | Product name, support email, allowed AI models, administrator sign-in length. |
| `GET`/`POST /phone-numbers`, `DELETE /phone-numbers/{number}` | Numbers on the platform phone account. Assigning one proves ownership and points its call webhooks at this application. |
| `GET /checklist` | What is still missing before clinics can be invited and take calls. |

Secrets are stored as AES-GCM ciphertext (`platform_connections`) under the deployment
keyring and never returned. Verified accounts are copied onto the settings of each
running API and worker process at start, after every change, and every 15 seconds, so
a change made on one process reaches the others without a restart.

The environment no longer carries provider keys: `TWILIO_*`, `DEEPGRAM_*`,
`ELEVENLABS_*`, `AZURE_*`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `LLM_*`, `SMTP_*`,
`STT_PROVIDER`, `TTS_PROVIDER`, `TENANT_LLM_MODELS` and `PLATFORM_SESSION_MINUTES` are
ignored if present.
