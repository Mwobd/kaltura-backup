"""
Central logging framework for the Kaltura Backup application.

This module configures both console and rotating file logging and provides
a thread-safe logger for the entire application.

All application modules should obtain their logger via
    get_logger()

and never call logging.basicConfig() themselves.

Python:
    >= 3.11
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from threading import Lock

from .config import Configuration


# ============================================================================
# Event identifiers
# ============================================================================


class EventId(StrEnum):
    """
    Stable event identifiers used throughout the application.
    """

    APPLICATION_START = "KBU-0001"
    APPLICATION_STOP = "KBU-0002"

    CONFIGURATION_LOADED = "KBU-0010"

    SESSION_CREATED = "KBU-1001"
    SESSION_RENEWED = "KBU-1002"
    SESSION_EXPIRED = "KBU-1003"

    ENTRY_DISCOVERED = "KBU-2001"
    ENTRY_SKIPPED = "KBU-2002"
    ENTRY_STARTED = "KBU-2003"
    ENTRY_COMPLETED = "KBU-2004"

    DOWNLOAD_STARTED = "KBU-3001"
    DOWNLOAD_COMPLETED = "KBU-3002"
    RETRY_SCHEDULED = "KBU-3003"

    CAPTION_DOWNLOADED = "KBU-4001"
    THUMBNAIL_DOWNLOADED = "KBU-4002"
    ATTACHMENT_DOWNLOADED = "KBU-4003"

    MANIFEST_WRITTEN = "KBU-5001"

    WARNING = "KBU-8000"

    CONNECTING = "KBU-1004"
    CONNECTED = "KBU-1005"
    DISCONNECTED = "KBU-1006"
    SESSION_ACQUIRED = "KBU-1007"
    SESSION_RELEASED = "KBU-1008"
    SESSION_RENEW = "KBU-1009"
    API_ERROR = "KBU-1010"

    ERROR = "KBU-9000"
    FATAL = "KBU-9001"


# ============================================================================
# Log formatter
# ============================================================================


class BackupFormatter(logging.Formatter):
    """
    Standard formatter used by all handlers.
    """

    DEFAULT_FORMAT = (
        "%(asctime)s "
        "%(levelname)-8s "
        "%(threadName)s "
        "%(message)s"
    )

    DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

    def __init__(self) -> None:

        super().__init__(
            fmt=self.DEFAULT_FORMAT,
            datefmt=self.DEFAULT_DATE_FORMAT,
        )


# ============================================================================
# Logger configuration
# ============================================================================


@dataclass(slots=True)
class LoggerSettings:
    """
    Internal logger configuration.
    """

    level: int
    log_directory: Path
    keep_days: int


# ============================================================================
# Backup logger
# ============================================================================


class BackupLogger:
    """
    Thread-safe wrapper around Python logging.
    """

    def __init__(
        self,
        configuration: Configuration,
    ) -> None:

        self._configuration = configuration

        self._lock = Lock()

        self._logger = logging.getLogger("kaltura_backup")

        self._logger.setLevel(
            getattr(
                logging,
                configuration.logging.level,
            )
        )

        self._logger.propagate = False

        self._configure()

    # ------------------------------------------------------------------

    @property
    def logger(self) -> logging.Logger:
        """
        Return the configured logger.
        """

        return self._logger

    def set_level(self, level: int) -> None:
        """Update the logger and all configured handlers at runtime."""
        with self._lock:
            self._logger.setLevel(level)
            for handler in self._logger.handlers:
                handler.setLevel(level)

    # ------------------------------------------------------------------

    def _configure(self) -> None:
        """
        Configure logging handlers.
        """

        if self._logger.handlers:
            return

        formatter = BackupFormatter()

        self._logger.addHandler(
            self._create_console_handler(
                formatter,
            )
        )

        self._logger.addHandler(
            self._create_file_handler(
                formatter,
            )
        )

    # ------------------------------------------------------------------

    def _create_console_handler(
        self,
        formatter: logging.Formatter,
    ) -> logging.Handler:
        """
        Create console handler.
        """

        handler = logging.StreamHandler()

        handler.setLevel(self._logger.level)

        handler.setFormatter(formatter)

        return handler

    # ------------------------------------------------------------------

    def _create_file_handler(
        self,
        formatter: logging.Formatter,
    ) -> logging.Handler:
        """
        Create rotating file handler.
        """

        log_directory = self._configuration.paths.log_dir

        log_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        logfile = log_directory / "kaltura_backup.log"

        handler = TimedRotatingFileHandler(
            filename=logfile,
            when="midnight",
            interval=1,
            backupCount=self._configuration.logging.keep_logs,
            encoding="utf-8",
        )

        handler.setLevel(
            self._logger.level,
        )

        handler.setFormatter(
            formatter,
        )

        return handler

    # ------------------------------------------------------------------

    def _build_message(
        self,
        event: EventId,
        message: str,
    ) -> str:
        """
        Prefix every message with its event identifier.
        """

        return f"{event} {message}"

    # ------------------------------------------------------------------
    # Logging methods continue in Part 2
    # ------------------------------------------------------------------

    def debug(
        self,
        event: EventId,
        message: str,
    ) -> None:
        """
        Log a DEBUG message.
        """
        with self._lock:
            self._logger.debug(
                self._build_message(event, message)
            )

    # ------------------------------------------------------------------

    def info(
        self,
        event: EventId,
        message: str,
    ) -> None:
        """
        Log an INFO message.
        """
        with self._lock:
            self._logger.info(
                self._build_message(event, message)
            )

    # ------------------------------------------------------------------

    def warning(
        self,
        event: EventId,
        message: str,
    ) -> None:
        """
        Log a WARNING message.
        """
        with self._lock:
            self._logger.warning(
                self._build_message(event, message)
            )

    # ------------------------------------------------------------------

    def error(
        self,
        event: EventId,
        message: str,
    ) -> None:
        """
        Log an ERROR message.
        """
        with self._lock:
            self._logger.error(
                self._build_message(event, message)
            )

    # ------------------------------------------------------------------

    def exception(
        self,
        event: EventId,
        message: str,
        exception: Exception,
    ) -> None:
        """
        Log an exception including the traceback.
        """
        with self._lock:
            self._logger.exception(
                self._build_message(
                    event,
                    f"{message}: {exception}",
                )
            )

    # ------------------------------------------------------------------

    def retry(
        self,
        attempt: int,
        maximum: int,
        delay: int,
        message: str,
    ) -> None:
        """
        Log a retry attempt.
        """

        self.warning(
            EventId.RETRY_SCHEDULED,
            (
                f"{message} "
                f"(attempt {attempt}/{maximum}, "
                f"retry in {delay}s)"
            ),
        )

    # ------------------------------------------------------------------

    def fatal(
        self,
        message: str,
    ) -> None:
        """
        Log a fatal application error.
        """

        self.error(
            EventId.FATAL,
            message,
        )


# ============================================================================
# Singleton
# ============================================================================


_logger_instance: BackupLogger | None = None
_logger_lock = Lock()


# ============================================================================
# Singleton access
# ============================================================================


def initialize_logger(
    configuration: Configuration,
) -> BackupLogger:
    """
    Initialize the application logger.

    This function should be called exactly once during application
    startup.

    Parameters
    ----------
    configuration:
        Application configuration.

    Returns
    -------
    BackupLogger
    """

    global _logger_instance

    with _logger_lock:

        if _logger_instance is None:
            _logger_instance = BackupLogger(
                configuration,
            )

    return _logger_instance


# --------------------------------------------------------------------------


def get_logger() -> BackupLogger:
    """
    Return the application logger.

    Returns
    -------
    BackupLogger

    Raises
    ------
    RuntimeError
        If the logger has not yet been initialized.
    """

    if _logger_instance is None:
        raise RuntimeError(
            "Logger has not been initialized."
        )

    return _logger_instance


# --------------------------------------------------------------------------


def shutdown_logger() -> None:
    """
    Flush and close all logging handlers.
    """

    global _logger_instance

    if _logger_instance is None:
        return

    logger = _logger_instance.logger

    for handler in logger.handlers[:]:
        handler.flush()
        handler.close()
        logger.removeHandler(handler)

    logging.shutdown()

    _logger_instance = None