# The receptionist: calls, text messages, tickets

One engine answers on every channel. The channel decides what to do with what the
engine produces: speak it, send it as a text, or show it in a test chat.

## A turn

`app/ai/engine.py` takes what the person just said and yields events as they happen:

| Event | Meaning |
| --- | --- |
| `Say` | The next sentence of the reply, complete enough to speak or send. |
| `Working`, `Worked` | A lookup started and finished (and whether it worked). |
| `Handoff` | A person has to take over: `requested`, `emergency` or `unavailable`. |
| `Done` | How the turn ended, and what it cost: tokens, time to the first words, total time. |

The order inside a turn never changes:

1. The fixed safety rules. They need no model; an emergency gets the fixed script.
2. A number that belongs to exactly one patient identifies that patient.
3. What the server answers itself: a code from reception, or yes/no to a question it asked.
4. The model, streaming its reply and calling tools, with its own reading of the risk
   running beside it (on the quicker model) instead of holding the reply up.

The model only proposes a booking, a move or a cancellation. The server words the
question, waits for a plain yes and makes the change itself. A test chat proposes
nothing and writes nothing.

## The model

`app/ai/llm` holds the provider interface and two adapters: OpenRouter and Gemini direct.
Both stream text and call tools natively. `ModelGateway` stands in front of them:

| What happens | What the gateway does |
| --- | --- |
| Rate limit, outage or timeout before anything was said | Up to two more tries with a growing pause, then one try on the lighter model. |
| A failure after part of the reply was delivered | Never replays it; the person is asked to say it again. |
| The key is refused or the account has no credit | No retry; the problem is remembered for the turn's record. |
| Repeated failures | The circuit for that model account opens and turns fail at once for a while. |
| One clinic running many turns at once | At most eight at a time per clinic in one process. |

When a turn cannot be finished the person is told so, the conversation is handed to
staff and a ticket is opened. Nobody is left with silence.

Every reply is recorded in `ai_turns`: channel, model, tokens, milliseconds to the first
words, what was looked up and how it ended. No words are stored there.
`GET /calls/{id}/turns` reads one call's replies; `GET /analytics/assistant` sums by day.

## Who the person is

Decided by the server, never by what the model was told:

- A number on exactly one patient's record is that patient.
- A number the clinic does not know can be registered as a new patient, once the
  person has given their name.
- On a call, reception can also issue a one-time code.

By text message a matching number is enough for everything the patient may do with
their own appointments: the reply only ever reaches that phone. On a call the number
shown can be faked, so there it is enough to book, and `privacy.known_number_can_manage`
(off unless the clinic turns it on) decides whether it is also enough to see, move and
cancel existing appointments. Otherwise the caller needs a code from reception, or the
receptionist takes a message.

## On the phone

The reply is spoken sentence by sentence while the rest is still being written. Silence
of more than a second and a half before the first words is filled with "one moment".
When the caller speaks over the reply it stops at once, the model is cancelled, and only
what the caller actually heard is kept in the transcript.

`GET /calls/{id}/live` streams the transcript of a call in progress.

## Text messages

`POST /webhooks/v1/twilio/sms` receives a text to one of a clinic's numbers. The number's
text address is set when the number is assigned, together with its call address.

- A message is stored once, however often the phone service delivers it.
- Two or three texts sent in a row are answered as one.
- The reply goes out through `outbound_messages`, like every other message to a patient.
- STOP and START set the patient's opt-out; nothing is sent in reply.
- A clinic that is not live keeps the messages for staff but the receptionist stays quiet.

Turns run in the API process, which may read patient records; the worker may not. A reply
lost to a restart is found again by a loop that looks for threads still waiting.

Staff see every thread under Messages. Writing in one takes it over from the
receptionist; handing it back starts the receptionist afresh.

| Route | Purpose |
| --- | --- |
| `GET /conversations`, `GET /conversations/{id}` | The threads, and one thread with its messages. Opening it marks it read. |
| `POST /conversations/{id}/messages` | Write to the patient as staff (takes the thread over). |
| `POST /conversations/{id}/take-over`, `/hand-back` | Who answers next. |

## Tickets

A ticket is a patient's request that a staff member has to deal with. They are opened by:

- staff, by hand,
- the receptionist, when it takes a message or cannot finish (`create_ticket`),
- the call handler, when a caller asks for a person, in an emergency, or when a call
  could not be answered at all,
- the worker, for a call whose report says a person should look at it, and for an
  appointment that was changed in a provider's own calendar.

The same cause opens one ticket, however often it is reported. Numbers count up per clinic.

When a ticket is opened by anyone but staff, an email goes to each address in
`notifications.recipients`. It says that a ticket is waiting, how urgent it is and
where it came from, never what the patient said.

| Route | Purpose |
| --- | --- |
| `GET /tickets`, `GET /tickets/counts` | The inbox (`view`: open, mine, unassigned, overdue, resolved, all) and its counts. |
| `POST /tickets`, `GET` / `PATCH /tickets/{id}` | Open one by hand; read it with its timeline; change status, priority, assignee. |
| `POST /tickets/{id}/notes` | A note for colleagues only. |
| `POST /tickets/{id}/replies` | A message to the patient, by text or else by email. |

Tickets replace the follow-up queue; reading them needs "see calls", working on them
needs "work on tickets and reply to messages".

## Try your receptionist

`POST /assistant/test` takes one message and answers with a stream of events (`start`,
`say`, `working`, `worked`, `handoff`, `done`). It uses the clinic's saved settings and
real lookups, and changes nothing: a booking it would propose is described, not made.

## Not built yet

- The summary of a finished call is still assembled from what was said, not written by
  the model.
- Confirmations and reminders are not shown inside the patient's thread; a reply to one
  still arrives in that thread and is answered.
- Gemini speech-to-speech (Phase 6).
