from app.core.errors import ApplicationError, ConflictError


class SlotUnavailableError(ConflictError):
    code = "slot_unavailable"
    public_message = "The requested time is not available."


class ScheduleOverlapError(ConflictError):
    code = "schedule_overlap"
    public_message = "This availability window overlaps an existing window."


class StaleAppointmentError(ConflictError):
    code = "stale_appointment"
    public_message = "The appointment has changed. Retrieve it and try again."


class AppointmentStateError(ConflictError):
    code = "appointment_state_conflict"
    public_message = "This appointment can no longer be changed."


class PastTimeError(ApplicationError):
    status_code = 422
    code = "past_time"
    public_message = "Choose a time in the future."
