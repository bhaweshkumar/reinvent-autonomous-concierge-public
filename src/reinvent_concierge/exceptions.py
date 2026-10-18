"""Custom exceptions for re:Invent Autonomous Concierge."""


class ReInventConciergeError(Exception):
    """Base exception for all errors."""

    pass


# Authentication and Credentials Errors
class CredentialsMissingError(ReInventConciergeError):
    """Raised when AWS Events API token is missing."""

    pass


class TokenStorageError(ReInventConciergeError):
    """Raised when credential storage fails."""

    pass


class SignInError(ReInventConciergeError):
    """The Builder ID sign-in (authorization code + PKCE) did not complete."""


# Rate Limiting Errors
class RateLimitError(ReInventConciergeError):
    """Raised on HTTP 429; ``retry_after`` comes from the Retry-After header."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded. Retry after {retry_after} seconds")


# AWS Events API Errors
class AWSEventsAPIError(ReInventConciergeError):
    """Generic error from AWS Events API (4xx)."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"AWS Events API error {status_code}: {body}")


class AWSEventsServerError(ReInventConciergeError):
    """Server error from AWS Events API (5xx)."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"AWS Events server error {status_code}: {body}")


class AuthenticationRequiredError(AWSEventsAPIError):
    """HTTP 401 that persisted after one token refresh; the attendee must sign in again."""


class WriteAccessInactiveError(AWSEventsAPIError):
    """Write blocked: WRITE_ACCESS_ACTIVE is False (dry-run) or the API answered 409."""


class ReservationConflictError(AWSEventsAPIError):
    """HTTP 409 while WRITE_ACCESS_ACTIVE is True: the operation is closed server-side."""


class ScheduleReadBackError(ReInventConciergeError):
    """A write succeeded but its result could not be found via GetSchedule."""


# Transit Engine Errors
class UnknownVenueError(ReInventConciergeError):
    """Raised when venue name is unrecognized."""

    pass


class ScheduleConflictException(ReInventConciergeError):
    """Raised when schedule has unresolvable conflicts."""

    pass


# Database Errors
class DatabaseError(ReInventConciergeError):
    """Base exception for database operations."""

    pass


class SyncError(DatabaseError):
    """Raised when catalog sync fails."""
