class ApplicationError(Exception):
    """Only static public messages belong here; provider exception text is private."""

    status_code = 400
    code = "application_error"
    public_message = "The request could not be completed."


class NotFoundError(ApplicationError):
    status_code = 404
    code = "not_found"
    public_message = "The requested resource was not found."


class ConflictError(ApplicationError):
    status_code = 409
    code = "conflict"
    public_message = "The request conflicts with the current resource state."


class AuthenticationError(ApplicationError):
    status_code = 401
    code = "unauthenticated"
    public_message = "Valid credentials are required."


class AuthorizationError(ApplicationError):
    status_code = 403
    code = "forbidden"
    public_message = "You do not have permission to perform this action."
