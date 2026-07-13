"""
Kaltura client abstraction.

This module encapsulates all interaction with the Kaltura Python SDK.

Responsibilities
----------------
- Session management
- Session pooling
- Automatic KS renewal
- Retry handling
- Translation of SDK exceptions
- Download helper methods

The remainder of the application never communicates directly with the
Kaltura SDK.
"""

from __future__ import annotations

import time

#from collections import deque
from functools import wraps
from threading import Condition
from typing import Any
from typing import Callable
from typing import TypeVar

try:
    from KalturaClient import KalturaClient
    from KalturaClient import KalturaConfiguration
    from KalturaClient import KalturaSessionType
except ImportError:  # pragma: no cover - exercised when SDK is absent
    class KalturaConfiguration:
        def __init__(self, partner_id: int) -> None:
            self.partner_id = partner_id
            self.serviceUrl = ""

    class KalturaSessionType:
        ADMIN = "ADMIN"

    class KalturaClient:
        def __init__(self, configuration: KalturaConfiguration) -> None:
            self.configuration = configuration
            self.session = type(
                "SessionProxy",
                (),
                {"start": lambda self, *_args, **_kwargs: ""},
            )()

        def setKs(self, ks: str) -> None:
            self.ks = ks

from .config import Configuration
from .exceptions import (
    ApiError,
    AuthenticationError,
    NetworkError,
    RetryExceededError,
    RetryableError,
    SessionExpiredError,
    SessionPoolExhaustedError,
    TimeoutError,
)

from .logging_utils import (
    EventId,
    BackupLogger,
)

MAX_SESSIONS = 4

RETRY_COUNT = 3

RETRY_DELAY = 5

T = TypeVar("T")


# ============================================================================
# Session object
# ============================================================================


class KalturaSession:
    """
    Wrapper around a KalturaClient instance and its KS.
    """
   
    def __init__(
        self,
        client: KalturaClient,
        ks: str,
    ) -> None:

        self.client = client

        self.ks = ks

        self.created = time.time()

        self.last_used = self.created
        self.in_use = False
        self.use_count = 0
        self.last_error = ""

    def touch(self) -> None:
        """
        Update last-used timestamp.
        """

        self.last_used = time.time()
        self.use_count += 1

# ============================================================================
# Retry decorator
# ============================================================================


def retryable(
    func: Callable[..., T],
) -> Callable[..., T]:
    """
    Retry Kaltura operations on retryable failures.
    """

    @wraps(func)
    def wrapper(
        self,
        *args,
        **kwargs,
    ) -> T:

        last_exception: Exception | None = None

        for attempt in range(
            1,
            RETRY_COUNT + 1,
        ):

            try:

                return func(
                    self,
                    *args,
                    **kwargs,
                )

            except RetryableError as exc:

                last_exception = exc

                self._logger.retry(
                    attempt=attempt,
                    maximum=RETRY_COUNT,
                    delay=RETRY_DELAY,
                    message=str(exc),
                )

                if attempt == RETRY_COUNT:
                    break

                time.sleep(
                    RETRY_DELAY,
                )

        raise RetryExceededError(
            str(last_exception)
        ) from last_exception

    return wrapper


# ============================================================================
# Client manager
# ============================================================================


class KalturaClientManager:
    """
    Thread-safe Kaltura session pool.

    A maximum of four authenticated sessions are created and reused
    by the worker threads.
    """

    def __init__(
        self,
        configuration: Configuration,
        logger: BackupLogger,
    ) -> None:

        self._configuration = configuration

        self._logger = logger

        self._pool: list[KalturaSession] = [] # self._pool: deque[KalturaSession] = deque()

        self._condition = Condition()

        self._connected = False

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        Create the configured number of authenticated Kaltura sessions.
        """

        if self._connected:
            return

        self._logger.info(
            EventId.CONNECTING,
            "Creating Kaltura session pool.",
        )

        for _ in range(MAX_SESSIONS):

            session = self._create_session()

            self._pool.append(session)

        self._connected = True

        self._logger.info(
            EventId.CONNECTED,
            f"Created {len(self._pool)} authenticated sessions.",
        )

    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """
        Dispose all pooled sessions.
        """

        with self._condition:

            self._pool.clear()

            self._connected = False

            self._condition.notify_all()

        self._logger.info(
            EventId.DISCONNECTED,
            "Kaltura session pool closed.",
        )

    # ------------------------------------------------------------------

    def _create_session(self) -> KalturaSession:
        """
        Create one authenticated Kaltura session.
        """

        try:

            cfg = KalturaConfiguration(
                self._configuration.connection.partner_id
            )

            cfg.serviceUrl = (
                self._configuration.connection.service_url
            )

            client = KalturaClient(cfg)

            ks = client.session.start(
                self._configuration.connection.admin_secret,
                None,
                KalturaSessionType.ADMIN,
                self._configuration.connection.partner_id,
                self._configuration.connection.expiry,
                self._configuration.connection.privileges,
            )

            client.setKs(ks)

            return KalturaSession(
                client,
                ks,
            )

        except Exception as exc:

            raise AuthenticationError(
                str(exc)
            ) from exc

    # ------------------------------------------------------------------

    def acquire(self) -> KalturaSession:
        """
        Acquire an available session from the pool.

        Blocks until a session becomes available.
        """

        with self._condition:

            while True:

                for session in self._pool:

                    if not session.in_use:

                        session.in_use = True

                        session.touch()

                        self._logger.debug(
                            EventId.SESSION_ACQUIRED,
                            (
                                "Session acquired "
                                f"(uses={session.use_count})"
                            ),
                        )

                        return session

                self._condition.wait()

    # ------------------------------------------------------------------

    def release(
        self,
        session: KalturaSession,
    ) -> None:
        """
        Return a session to the pool.
        """

        with self._condition:

            session.in_use = False

            session.touch()

            self._condition.notify()

        self._logger.debug(
            EventId.SESSION_RELEASED,
            "Session released.",
        )

    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """
        Return True if the client manager is connected.
        """

        return self._connected

    # ------------------------------------------------------------------

    @property
    def active_sessions(self) -> int:
        """
        Number of sessions currently leased.
        """

        return sum(
            1
            for session in self._pool
            if session.in_use
        )

    # ------------------------------------------------------------------

    @property
    def available_sessions(self) -> int:
        """
        Number of available sessions.
        """

        return (
            len(self._pool)
            - self.active_sessions
        )
    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _renew_session(
        self,
        session: KalturaSession,
    ) -> None:
        """
        Renew an expired Kaltura session.
        """

        self._logger.info(
            EventId.SESSION_RENEW,
            "Renewing Kaltura session.",
        )

        new_session = self._create_session()

        session.client = new_session.client
        session.ks = new_session.ks
        session.created = new_session.created
        session.last_used = new_session.last_used
        session.last_error = ""

    # ------------------------------------------------------------------

    class _SessionContext:
        """
        Context manager for pooled sessions.
        """

        def __init__(
            self,
            manager: "KalturaClientManager",
        ) -> None:

            self._manager = manager
            self._session: KalturaSession | None = None

        def __enter__(self) -> KalturaSession:

            self._session = self._manager.acquire()
            return self._session

        def __exit__(
            self,
            exc_type,
            exc,
            tb,
        ) -> bool:

            if self._session is not None:
                self._manager.release(self._session)

            return False

    # ------------------------------------------------------------------

    def session(self) -> "_SessionContext":
        """
        Return a context manager that automatically acquires and
        releases a pooled session.
        """

        return self._SessionContext(self)

    # ------------------------------------------------------------------
    # Entry operations
    # ------------------------------------------------------------------

    @retryable
    def get_entry(
        self,
        entry_id: str,
    ) -> Any:
        """
        Retrieve a single Kaltura entry.
        """

        with self.session() as session:

            try:

                return session.client.baseEntry.get(
                    entry_id
                )

            except Exception as exc:

                self._translate_exception(exc)

    # ------------------------------------------------------------------

    @retryable
    def list_entries(
        self,
        filter_object: Any,
        pager: Any,
    ) -> Any:
        """
        Retrieve a page of entries.
        """

        with self.session() as session:

            try:

                return session.client.baseEntry.list(
                    filter_object,
                    pager,
                )

            except Exception as exc:

                self._translate_exception(exc)

    # ------------------------------------------------------------------
    # Exception translation
    # ------------------------------------------------------------------

    def _translate_exception(
        self,
        exception: Exception,
    ) -> None:
        """
        Translate SDK exceptions into project exceptions.
        """

        message = str(exception).lower()

        self._logger.exception(
            EventId.API_ERROR,
            "Kaltura API exception",
            exception,
        )

        if "invalid_ks" in message:
            raise SessionExpiredError(
                str(exception)
            ) from exception

        if "timeout" in message:
            raise TimeoutError(
                str(exception)
            ) from exception

        if (
            "connection" in message
            or "network" in message
        ):
            raise NetworkError(
                str(exception)
            ) from exception

        if (
            "temporarily"
            in message
            or "try again"
            in message
        ):
            raise RetryableError(
                str(exception)
            ) from exception

        raise ApiError(
            str(exception)
        ) from exception    