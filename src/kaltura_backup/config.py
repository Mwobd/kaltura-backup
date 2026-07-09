"""
Configuration management for the Kaltura Backup application.

Reads the INI configuration file, validates all required values and
returns a strongly typed Configuration object that is used throughout
the application.

Author: <your name>
"""

from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """Kaltura connection settings."""

    partner_id: int
    admin_secret: str
    service_url: str


@dataclass(frozen=True, slots=True)
class PathConfig:
    """Filesystem paths."""

    backup_dir: Path
    csv_dir: Path
    log_dir: Path
    report_dir: Path
    state_file: Path


@dataclass(frozen=True, slots=True)
class DownloadConfig:
    """Download behaviour."""

    workers: int
    retry_count: int
    retry_delay_seconds: int
    timeout: int
    skip_older_than_hours: int
    resume_downloads: bool
    verify_checksum: bool


@dataclass(frozen=True, slots=True)
class ExportConfig:
    """Export options."""

    save_metadata: bool
    save_api_responses: bool
    save_captions: bool
    save_thumbnails: bool
    save_attachments: bool


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Logging configuration."""

    level: str
    keep_logs: int


@dataclass(frozen=True, slots=True)
class Configuration:
    """Complete application configuration."""

    connection: ConnectionConfig
    paths: PathConfig
    download: DownloadConfig
    export: ExportConfig
    logging: LoggingConfig


# ---------------------------------------------------------------------------
# Configuration loader
# ---------------------------------------------------------------------------


class ConfigurationError(RuntimeError):
    """Raised when the configuration is invalid."""


class ConfigLoader:
    """Reads and validates config.ini."""

    REQUIRED_SECTIONS = (
        "Connection",
        "Paths",
        "Download",
        "Export",
        "Logging",
    )

    def __init__(self, config_file: Path):
        self._config_file = config_file
        self._parser = ConfigParser()

    def load(self) -> Configuration:
        """
        Load and validate the configuration.

        Returns
        -------
        Configuration
            Fully populated configuration object.
        """

        if not self._config_file.exists():
            raise ConfigurationError(
                f"Configuration file not found: {self._config_file}"
            )

        self._parser.read(self._config_file, encoding="utf-8")

        self._validate_sections()

        return Configuration(
            connection=self._load_connection(),
            paths=self._load_paths(),
            download=self._load_download(),
            export=self._load_export(),
            logging=self._load_logging(),
        )

    # ------------------------------------------------------------------

    def _validate_sections(self) -> None:

        for section in self.REQUIRED_SECTIONS:
            if not self._parser.has_section(section):
                raise ConfigurationError(
                    f"Missing configuration section [{section}]"
                )

    # ------------------------------------------------------------------

    def _load_connection(self) -> ConnectionConfig:

        section = self._parser["Connection"]

        return ConnectionConfig(
            partner_id=section.getint("PartnerId"),
            admin_secret=section.get("AdminSecret", "").strip(),
            service_url=section.get("ServiceUrl", "").strip(),
        )

    # ------------------------------------------------------------------

    def _load_paths(self) -> PathConfig:

        section = self._parser["Paths"]

        return PathConfig(
            backup_dir=Path(section.get("BackupDir")),
            csv_dir=Path(section.get("CsvDir")),
            log_dir=Path(section.get("LogDir")),
            report_dir=Path(section.get("ReportDir")),
            state_file=Path(section.get("StateFile")),
        )

    # ------------------------------------------------------------------

    def _load_download(self) -> DownloadConfig:

        section = self._parser["Download"]

        return DownloadConfig(
            workers=section.getint("Workers"),
            retry_count=section.getint("RetryCount"),
            retry_delay_seconds=section.getint("RetryDelaySeconds"),
            timeout=section.getint("Timeout"),
            skip_older_than_hours=section.getint("SkipOlderThanHours"),
            resume_downloads=section.getboolean("ResumeDownloads"),
            verify_checksum=section.getboolean("VerifyChecksum"),
        )

    # ------------------------------------------------------------------

    def _load_export(self) -> ExportConfig:

        section = self._parser["Export"]

        return ExportConfig(
            save_metadata=section.getboolean("SaveMetadata"),
            save_api_responses=section.getboolean("SaveApiResponses"),
            save_captions=section.getboolean("SaveCaptions"),
            save_thumbnails=section.getboolean("SaveThumbnails"),
            save_attachments=section.getboolean("SaveAttachments"),
        )

    # ------------------------------------------------------------------

    def _load_logging(self) -> LoggingConfig:

        section = self._parser["Logging"]

        return LoggingConfig(
            level=section.get("Level"),
            keep_logs=section.getint("KeepLogs"),
        )


def load_configuration(config_file: str | Path = "config.ini") -> Configuration:
    """
    Convenience function for loading the application configuration.

    Parameters
    ----------
    config_file:
        Path to config.ini

    Returns
    -------
    Configuration
    """

    loader = ConfigLoader(Path(config_file))
    return loader.load()