"""The messages a patient gets when an appointment is booked, moved or cancelled.

Queued in the same transaction as the change, so a booking that fails sends nothing
and a booking that succeeds always has its messages. The worker only delivers; it
never needs to read patient records.
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from app.appointments.models import Appointment, reference
from app.conversation.locales import LanguagePolicy
from app.core.database.unit_of_work import Transaction
from app.languages import family, known, normalize, pack
from app.notifications.templates import SMS_LIMIT, render, when

Change = Literal["booked", "rescheduled", "cancelled"]
_KIND: dict[Change, str] = {
    "booked": "confirmation",
    "rescheduled": "rescheduled",
    "cancelled": "cancellation",
}


def message_language(patient_language: str | None, policy: LanguagePolicy) -> str:
    """The language a patient's messages are written in: their own, when the clinic
    still offers it (Hinglish with Hindi), otherwise the clinic's main language."""
    if (
        patient_language
        and known(patient_language)
        and family(patient_language) in policy.enabled_languages
    ):
        return normalize(patient_language)
    return policy.default_language


async def queue_appointment_messages(tx: Transaction, entity: Appointment, change: Change) -> None:
    """Queue the confirmation (or change notice) now and the reminder for later."""
    settings = await tx.schedule.settings()
    wording = settings.notifications
    # Whatever was waiting for the old time or the cancelled visit must not go out.
    await tx.messages.cancel_for_appointment(entity.id)
    if not wording.confirmations_enabled and not wording.reminders_enabled:
        return
    patient = await tx.patients.get(entity.patient_id)
    if patient is None:
        return
    timezone = await tx.schedule.timezone(entity.provider_id) or settings.clinic.timezone
    policy = settings.receptionist.languages
    language = message_language(patient.language, policy)
    words = pack(language).clinic_texts
    provider = (await tx.providers.display_names({entity.provider_id})).get(entity.provider_id, "")
    service = ""
    if entity.service_id is not None:
        service = (await tx.schedule.service_names({entity.service_id})).get(entity.service_id, "")
    values = {
        "patient_name": patient.display_name,
        "clinic_name": settings.clinic.name,
        "provider_name": provider,
        "service_name": service,
        "starts_at": when(entity.starts_at, timezone, language),
        "appointment_number": reference(entity.number),
    }
    # From the provider's own line when it has one, so replies reach the right desk.
    line = await tx.providers.get(entity.provider_id)
    sender = await tx.messages.sender_number(
        entity.provider_id, line.location_id if line is not None else None
    )
    # A text needs a clinic number to come from and a patient who still wants texts.
    channels: list[tuple[str, str]] = []
    if sender is not None and not patient.sms_opt_out:
        channels.append(("sms", patient.phone))
    if patient.email:
        channels.append(("email", patient.email))

    async def queue(kind: str, text: str, available_at: datetime | None = None) -> None:
        body = render(text, values)
        if "appointment_number" not in text:
            # Every message carries the number, whatever wording the clinic saved.
            body = f"{body} " + words["number_suffix"].format(number=values["appointment_number"])
        for channel, recipient in channels:
            await tx.messages.enqueue(
                channel=channel,
                kind=kind,
                recipient=recipient,
                sender=sender if channel == "sms" else None,
                subject=words[f"subject_{kind}"].format(clinic=settings.clinic.name)
                if channel == "email"
                else None,
                body=body[:SMS_LIMIT] if channel == "sms" else body,
                available_at=available_at,
                appointment_id=entity.id,
                # The version changes with every move or cancellation.
                dedupe_key=f"appointment:{entity.id}:{entity.version}:{kind}:{channel}",
            )

    main = policy.default_language
    if wording.confirmations_enabled:
        kind = "cancellation_text" if change == "cancelled" else "confirmation_text"
        await queue(_KIND[change], wording.text(kind, language, main))
    if wording.reminders_enabled and change != "cancelled":
        remind_at = entity.starts_at - timedelta(hours=wording.reminder_hours_before)
        # Booked inside the reminder window: the confirmation already says it all.
        if remind_at > datetime.now(UTC):
            await queue("reminder", wording.text("reminder_text", language, main), remind_at)
