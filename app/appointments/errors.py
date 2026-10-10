from app.core.errors import ApplicationError, ConflictError


class SlotUnavailableError(ConflictError):
    code = "slot_unavailable"
    public_message = "The requested time is not available."


class StaleAppointmentError(ConflictError):
    code = "stale_appointment"
    public_message = "The appointment has changed. Retrieve it and try again."


class AppointmentStateError(ConflictError):
    code = "appointment_state_conflict"
    public_message = "This appointment can no longer be changed."


class TooLateToChangeError(ConflictError):
    code = "too_late_to_change"
    public_message = "It is too close to the appointment to change it. Please call the clinic."


class PastTimeError(ApplicationError):
    status_code = 422
    code = "past_time"
    public_message = "Choose a time in the future."
