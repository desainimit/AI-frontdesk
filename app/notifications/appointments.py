"""The messages a patient gets when an appointment is booked, moved or cancelled.

Queued in the same transaction as the change, so a booking that fails sends nothing
and a booking that succeeds always has its messages. The worker only delivers; it
never needs to read patient records.
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from app.appointments.models import Appointment, reference
from app.core.database.unit_of_work import Transaction
from app.notifications.templates import SMS_LIMIT, render, when

Change = Literal["booked", "rescheduled", "cancelled"]
_KIND: dict[Change, str] = {
    "booked": "confirmation",
    "rescheduled": "rescheduled",
    "cancelled": "cancellation",
}
_SUBJECT: dict[str, str] = {
    "confirmation": "Your appointment at {clinic}",
    "rescheduled": "Your appointment at {clinic} has moved",
    "cancellation": "Your appointment at {clinic} was cancelled",
    "reminder": "Reminder: your appointment at {clinic}",
}


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
    provider = (await tx.providers.display_names({entity.provider_id})).get(entity.provider_id, "")
    service = ""
    if entity.service_id is not None:
        service = (await tx.schedule.service_names({entity.service_id})).get(entity.service_id, "")
    values = {
        "patient_name": patient.display_name,
        "clinic_name": settings.clinic.name,
        "provider_name": provider,
        "service_name": service,
        "starts_at": when(entity.starts_at, timezone),
        "appointment_number": reference(entity.number),
    }
    sender = await tx.messages.sender_number()
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
            body = f"{body} Appointment number: {values['appointment_number']}."
        for channel, recipient in channels:
            await tx.messages.enqueue(
                channel=channel,
                kind=kind,
                recipient=recipient,
                sender=sender if channel == "sms" else None,
                subject=_SUBJECT[kind].format(clinic=settings.clinic.name)
                if channel == "email"
                else None,
                body=body[:SMS_LIMIT] if channel == "sms" else body,
                available_at=available_at,
                appointment_id=entity.id,
                # The version changes with every move or cancellation.
                dedupe_key=f"appointment:{entity.id}:{entity.version}:{kind}:{channel}",
            )

    if wording.confirmations_enabled:
        text = wording.cancellation_text if change == "cancelled" else wording.confirmation_text
        await queue(_KIND[change], text)
    if wording.reminders_enabled and change != "cancelled":
        remind_at = entity.starts_at - timedelta(hours=wording.reminder_hours_before)
        # Booked inside the reminder window: the confirmation already says it all.
        if remind_at > datetime.now(UTC):
            await queue("reminder", wording.reminder_text, remind_at)
