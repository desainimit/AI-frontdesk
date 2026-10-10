# Creating clinics, patient messages and calendar sync

## Creating a clinic

`POST /api/v1/platform/workspaces` with an `Idempotency-Key` header creates a clinic that
works at once. One transaction saves:

- the workspace (in `setup`: it can be managed, calls start when it goes live),
- its settings, from the platform's defaults for new clinics,
- four roles and the default appointment types,
- the first location,
- the owner's pending membership and, when the email server is set up, their invitation.

The same request can also assign a phone number from the platform's phone account
(`phone_number`) and start taking calls (`go_live`). Those two, and the invitation, are
optional steps: when one cannot be done the clinic is still created and the response's
`warnings` says what is left. A retry with the same key answers with the same clinic and
repeats nothing; the same key with a different body answers 409.

| Route | Purpose |
| --- | --- |
| `GET /platform/workspaces/{id}` | The clinic, its owner with invitation status, and its phone numbers. |
| `POST /platform/workspaces/{id}/invitation` | Send the owner's invitation. A new key sends a fresh link and cancels the last. |
| `PUT /platform/workspaces/{id}/owner` | Correct the owner's name or email until they accept. |
| `GET` / `PUT` / `DELETE /platform/defaults` | What new clinics start with; `DELETE` goes back to the product's defaults. Write `{{clinic_name}}` where the clinic's name belongs. Existing clinics are not changed. |
| `GET /getting-started` | For the clinic itself: seven steps, each worked out from its own data. |

A clinic can go live before its owner has signed in: answering calls does not depend on
anyone being signed in.

## Messages to patients

Booking, moving or cancelling an appointment queues the patient's messages in the same
transaction as the change (`outbound_messages`):

- a text, when the clinic has a phone number and the patient has not opted out,
- an email, when the patient has an address,
- a reminder, which is the same kind of message due later (`reminder_hours_before`).

A change cancels whatever has not gone out yet for that appointment. A patient who opts
out of texts also loses the ones still waiting.

The worker sends: email through the platform's email server, texts through the clinic's
own phone service connection or else the platform's. A failure is retried with growing
pauses; a message that cannot be sent ends as `failed` with a plain reason, and one that
is more than six hours overdue is not sent at all.

| Route | Purpose |
| --- | --- |
| `GET /notifications/messages` | The clinic's delivery log, newest first. Recipients are partly hidden. |
| `POST /notifications/test` | Send the clinic's confirmation wording, with sample details, to one address. |
| `GET /platform/messages` | The log across every clinic, for platform administrators. |

The wording (`confirmation_text`, `reminder_text`, `cancellation_text`) is part of the
clinic's `notifications` settings and may use `{{patient_name}}`, `{{clinic_name}}`,
`{{starts_at}}`, `{{provider_name}}` and `{{service_name}}`.

Account emails (invitations, password reset) keep their own queue (`auth_mail`).

## Calendar sync

A provider's own Google or Outlook calendar is connected from their settings.

1. A platform administrator saves the Google and Microsoft apps under Platform, Settings
   (`google_calendar`, `microsoft_calendar`: client ID and client secret). Register
   `<frontend origin>/api/calendar/callback` as the redirect address of both apps.
   Scopes: Google `openid email https://www.googleapis.com/auth/calendar.events`;
   Microsoft `offline_access User.Read Calendars.ReadWrite`.
2. `POST /providers/{id}/calendar/connect` answers with the address to send the calendar's
   owner to. They sign in and agree; the browser returns to the frontend, which calls
   `POST /calendar/complete`. The sign-in uses PKCE and a single-use state bound to the
   clinic that started it. Only the refresh token is kept, encrypted.
3. `GET /calendar` lists each provider's connection; `DELETE /providers/{id}/calendar`
   disconnects (entries already in the calendar stay).

What the worker does:

- **Out.** Every booking, move and cancellation is written to the calendar. The entry
  says what kind of visit it is; the patient's name only when the clinic turned on
  `privacy.calendar_shows_patient_name`. A retried write is recognised as the same entry.
- **In.** Every five minutes the provider's events for the next 60 days are read. The
  time they take (`calendar_busy_blocks`) is removed from bookable slots. All-day entries
  and entries marked free do not block.
- **Conflicts.** If the entry written for an appointment was moved or deleted in the
  calendar, the appointment is flagged (`appointment_calendar_links.status = 'conflict'`)
  and shown on the provider's Calendar tab. The appointment itself is never changed
  silently.

A booking also asks the calendar once more just before it is saved, so something added
since the last sync is respected. If the calendar cannot be reached, the booking relies
on the last sync and is not refused. When consent is withdrawn the connection shows
"needs attention" until it is connected again.

Not built yet: push notifications from Google and Microsoft (the five-minute read and the
check at booking cover the same ground more slowly), and opening a ticket for a conflict
(tickets arrive in Phase 5).
