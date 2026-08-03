"""
Custom exception hierarchy for the Kaltura Backup application.

The application uses a small hierarchy of strongly typed exceptions to
distinguish between retryable and non-retryable failures.

Retryable exceptions indicate that the operation may succeed if attempted
again after a delay.

Non-retryable exceptions indicate a permanent failure that should be logged
and skipped.

Author:
    <your name>

Python:
    >= 3.11
"""

from __future__ import annotations


class BackupError(Exception):
    """
    Base class for all application exceptions.
    """


# ============================================================================
# Configuration
# ============================================================================


class ConfigurationError(BackupError):
    """
    Invalid or incomplete application configuration.
    """


# ============================================================================
# Authentication / Session
# ============================================================================


class AuthenticationError(BackupError):
    """
    Failed to authenticate with Kaltura.
    """


class SessionError(BackupError):
    """
    Generic Kaltura session error.
    """


class SessionExpiredError(SessionError):
    """
    The current KS has expired and must be renewed.
    """


class SessionPoolExhaustedError(SessionError):
    """
    No session is available from the session pool.
    """


# ============================================================================
# Retryable errors
# ============================================================================


class RetryableError(BackupError):
    """
    Base class for errors that may succeed after retrying.
    """


class NetworkError(RetryableError):
    """
    Network communication failed.
    """


class TimeoutError(RetryableError):
    """
    Network timeout.
    """


class ApiError(RetryableError):
    """
    Temporary Kaltura API failure.
    """


class DownloadError(RetryableError):
    """
    Download of a media object failed.
    """


# ============================================================================
# Permanent failures
# ============================================================================


class PermanentError(BackupError):
    """
    Base class for permanent failures.
    """


class ClientError(PermanentError):
    """
    Kaltura client configuration or integration error.
    """


class EntryNotFoundError(PermanentError):
    """
    Requested Kaltura entry does not exist.
    """


class MetadataError(PermanentError):
    """
    Metadata retrieval or parsing failed.
    """


class ManifestError(PermanentError):
    """
    Manifest could not be written or parsed.
    """


class StateError(PermanentError):
    """
    backup_state.json could not be read or written.
    """


class ValidationError(PermanentError):
    """
    Invalid application data.
    """


class ExportError(PermanentError):
    """
    Export operation failed.
    """


# ============================================================================
# Retry management
# ============================================================================


class RetryExceededError(BackupError):
    """
    Maximum retry count reached.
    """


# ============================================================================
# Cancellation
# ============================================================================


class BackupCancelled(BackupError):
    """
    Backup interrupted by the user.
    """


# ============================================================================
# Utility functions
# ============================================================================


def is_retryable(exception: BaseException) -> bool:
    """
    Determine whether an exception is retryable.

    Parameters
    ----------
    exception:
        Exception instance.

    Returns
    -------
    bool
        True if the operation should be retried.
    """

    return isinstance(exception, RetryableError)


def is_permanent(exception: BaseException) -> bool:
    """
    Determine whether an exception represents a permanent failure.

    Parameters
    ----------
    exception:
        Exception instance.

    Returns
    -------
    bool
        True if the failure is permanent.
    """

    return isinstance(exception, PermanentError)