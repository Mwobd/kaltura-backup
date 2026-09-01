"""
Configuration management for the Kaltura Backup application.

This module is responsible for:

- Reading the application configuration from config.ini.
- Validating required sections and values.
- Converting values to strongly typed dataclasses.
- Creating required application directories.
- Returning a single immutable Configuration object that is used
  throughout the application.

Only this module should access config.ini directly.
All other modules receive a Configuration instance.

Author:
    <your name>

Python:
    >= 3.11
"""

from __future__ import annotations

from configparser import ConfigParser, NoOptionError
from dataclasses import dataclass
from pathlib import Path
from .exceptions import ConfigurationError

DEFAULT_CONFIG_TEMPLATE = """[Connection]
PartnerId = 123
AdminSecret = your-admin-secret
ServiceUrl = https://www.kaltura.com
PlayManifestUrl = https://api.kaltura.com/p/
Expiry = 86400
Privileges = 

[Paths]
BackupDir = backup
CsvDir = csv
LogDir = logs
ReportDir = reports
StateFile = backup_state.json

[Download]
Workers = 1
RetryCount = 2
RetryDelaySeconds = 0
Timeout = 30
SkipOlderThanHours = 0
ResumeDownloads = false
VerifyChecksum = false

[Export]
SaveMetadata = true
SaveApiResponses = true
SaveCaptions = false
SaveThumbnails = false
SaveAttachments = false

[metadata_profiles]

[Logging]
Level = INFO
KeepLogs = 7
"""

# ============================================================================
# Configuration dataclasses
# ============================================================================


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """
    Kaltura connection configuration.
    """

    partner_id: int
    admin_secret: str
    service_url: str
    play_manifest_url: str = "https://api.kaltura.com/p/"
    expiry: int = 86400
    privileges: str = ""


@dataclass(frozen=True, slots=True)
class PathConfig:
    """
    Application paths.
    """

    backup_dir: Path
    csv_dir: Path
    log_dir: Path
    report_dir: Path
    state_file: Path


@dataclass(frozen=True, slots=True)
class DownloadConfig:
    """
    Download behaviour.
    """

    workers: int
    retry_count: int
    retry_delay_seconds: int
    timeout: int
    skip_older_than_hours: int
    resume_downloads: bool
    verify_checksum: bool


@dataclass(frozen=True, slots=True)
class ExportConfig:
    """
    Export options.
    """

    save_metadata: bool
    save_api_responses: bool
    save_captions: bool
    save_thumbnails: bool
    save_attachments: bool


@dataclass(frozen=True, slots=True)
class MetadataConfig:
    """
    Custom metadata profile selection.
    """

    profile_fields: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """
    Logging configuration.
    """

    level: str
    keep_logs: int


@dataclass(frozen=True, slots=True)
class Configuration:
    """
    Complete application configuration.
    """

    connection: ConnectionConfig
    paths: PathConfig
    download: DownloadConfig
    export: ExportConfig
    metadata: MetadataConfig
    logging: LoggingConfig


# ============================================================================
# Exceptions
# ============================================================================




# ============================================================================
# Configuration Loader
# ============================================================================


class ConfigLoader:
    """
    Reads, validates and converts config.ini into a strongly typed
    Configuration object.
    """

    REQUIRED_SECTIONS = (
        "Connection",
        "Paths",
        "Download",
        "Export",
        "metadata_profiles",
        "Logging",
    )

    def __init__(self, config_file: str | Path):

        self._config_file = Path(config_file)
        self._parser = ConfigParser()

    # ---------------------------------------------------------------------

    def load(self) -> Configuration:
        """
        Read, validate and return the application configuration.

        Raises
        ------
        ConfigurationError
            If the configuration file is missing or invalid.
        """

        if not self._config_file.exists():
            raise ConfigurationError(
                f"Configuration file not found: {self._config_file}"
            )

        self._parser.read(
            self._config_file,
            encoding="utf-8",
        )

        self._validate_sections()

        configuration = Configuration(
            connection=self._load_connection(),
            paths=self._load_paths(),
            download=self._load_download(),
            export=self._load_export(),
            metadata=self._load_metadata(),
            logging=self._load_logging(),
        )

        self._create_directories(configuration)

        return configuration

    # ---------------------------------------------------------------------

    def _validate_sections(self) -> None:
        """
        Ensure every required section exists.
        """

        for section in self.REQUIRED_SECTIONS:

            if not self._parser.has_section(section):
                raise ConfigurationError(
                    f"Missing configuration section [{section}]"
                )

    # ---------------------------------------------------------------------

    def _require(
        self,
        section: str,
        option: str,
    ) -> str:
        """
        Return a required configuration value.

        Raises
        ------
        ConfigurationError
            If the option is missing or empty.
        """

        try:
            value = self._parser.get(
                section,
                option,
            ).strip()

        except NoOptionError as exc:
            raise ConfigurationError(
                f"Missing configuration value [{section}] {option}"
            ) from exc

        if not value:
            raise ConfigurationError(
                f"Configuration value [{section}] {option} cannot be empty."
            )

        return value

    # ---------------------------------------------------------------------

    def _positive_int(
        self,
        section: str,
        option: str,
        minimum: int = 1,
        maximum: int | None = None,
    ) -> int:
        """
        Read and validate a positive integer.
        """

        value = self._parser.getint(
            section,
            option,
        )

        if value < minimum:
            raise ConfigurationError(
                f"[{section}] {option} must be >= {minimum}"
            )

        if maximum is not None and value > maximum:
            raise ConfigurationError(
                f"[{section}] {option} must be <= {maximum}"
            )

        return value

    # ---------------------------------------------------------------------
    # Section loaders
    # ---------------------------------------------------------------------
    def _load_connection(self) -> ConnectionConfig:
        """
        Load the [Connection] section.
        """

        return ConnectionConfig(
            partner_id=self._positive_int(
                "Connection",
                "PartnerId",
            ),
            admin_secret=self._require(
                "Connection",
                "AdminSecret",
            ),
            service_url=self._require(
                "Connection",
                "ServiceUrl",
            ),
            play_manifest_url=self._parser.get(
                "Connection",
                "PlayManifestUrl",
                fallback="https://api.kaltura.com/p/",
            ),
            expiry=self._parser.getint(
                "Connection",
                "Expiry",
                fallback=86400,
            ),
            privileges=self._parser.get(
                "Connection",
                "Privileges",
                fallback="",
            ),
        )

    # ---------------------------------------------------------------------

    def _load_paths(self) -> PathConfig:
        """
        Load the [Paths] section.
        """

#        section = self._parser["Paths"]

        return PathConfig(
            backup_dir=Path(
                self._require("Paths", "BackupDir")
            ).resolve(),
            csv_dir=Path(
                self._require("Paths", "CsvDir")
            ).resolve(),
            log_dir=Path(
                self._require("Paths", "LogDir")
            ).resolve(),
            report_dir=Path(
                self._require("Paths", "ReportDir")
            ).resolve(),
            state_file=Path(
                self._require("Paths", "StateFile")
            ).resolve(),
        )

    # ---------------------------------------------------------------------

    def _load_download(self) -> DownloadConfig:
        """
        Load the [Download] section.
        """

        section = "Download"

        return DownloadConfig(
            workers=self._positive_int(
                section,
                "Workers",
                minimum=1,
                maximum=4,
            ),
            retry_count=self._positive_int(
                section,
                "RetryCount",
                minimum=0,
            ),
            retry_delay_seconds=self._positive_int(
                section,
                "RetryDelaySeconds",
                minimum=0,
            ),
            timeout=self._positive_int(
                section,
                "Timeout",
            ),
            skip_older_than_hours=self._positive_int(
                section,
                "SkipOlderThanHours",
                minimum=0,
            ),
            resume_downloads=self._parser.getboolean(
                section,
                "ResumeDownloads",
            ),
            verify_checksum=self._parser.getboolean(
                section,
                "VerifyChecksum",
            ),
        )

    # ---------------------------------------------------------------------

    def _load_export(self) -> ExportConfig:
        """
        Load the [Export] section.
        """

        section = "Export"

        return ExportConfig(
            save_metadata=self._parser.getboolean(
                section,
                "SaveMetadata",
            ),
            save_api_responses=self._parser.getboolean(
                section,
                "SaveApiResponses",
            ),
            save_captions=self._parser.getboolean(
                section,
                "SaveCaptions",
            ),
            save_thumbnails=self._parser.getboolean(
                section,
                "SaveThumbnails",
            ),
            save_attachments=self._parser.getboolean(
                section,
                "SaveAttachments",
            ),
        )

    # ---------------------------------------------------------------------

    def _load_metadata(self) -> MetadataConfig:
        """
        Load the [metadata_profiles] section.
        """

        section = "metadata_profiles"

        profile_fields: dict[str, tuple[str, ...]] = {}

        for profile_id, raw_fields in self._parser.items(section):
            profile_fields[profile_id.strip()] = tuple(
                field.strip()
                for field in raw_fields.split(",")
                if field.strip()
            )

        return MetadataConfig(
            profile_fields=profile_fields,
        )

    # ---------------------------------------------------------------------

    def _load_logging(self) -> LoggingConfig:
        """
        Load the [Logging] section.
        """

        section = "Logging"

        level = self._require(
            section,
            "Level",
        ).upper()

        valid_levels = {
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
        }

        if level not in valid_levels:
            raise ConfigurationError(
                f"Invalid logging level '{level}'. "
                f"Expected one of: {', '.join(sorted(valid_levels))}"
            )

        return LoggingConfig(
            level=level,
            keep_logs=self._positive_int(
                section,
                "KeepLogs",
            ),
        )

    # ---------------------------------------------------------------------

    def _create_directories(
        self,
        configuration: Configuration,
    ) -> None:
        """
        Create the required application directories.

        Existing directories are left untouched.
        """

        directories = (
            configuration.paths.backup_dir,
            configuration.paths.csv_dir,
            configuration.paths.log_dir,
            configuration.paths.report_dir,
        )

        for directory in directories:
            directory.mkdir(
                parents=True,
                exist_ok=True,
            )


# ============================================================================
# Public helper
# ============================================================================


def load_configuration(
    config_file: str | Path = "config.ini",
) -> Configuration:
    """
    Load the application configuration.

    Parameters
    ----------
    config_file:
        Path to the configuration file.

    Returns
    -------
    Configuration
        Immutable application configuration.
    """

    config_path = Path(config_file)

    if not config_path.exists():
        if config_path.name == "config.ini":
            ensure_default_config(config_path)
        else:
            raise ConfigurationError(
                f"Configuration file not found: {config_path}"
            )

    return ConfigLoader(config_file).load()


def ensure_default_config(
    config_file: str | Path = "config.ini",
) -> bool:
    """
    Create a default configuration file if one does not already exist.

    Returns
    -------
    bool
        True when a new file was created, False otherwise.
    """

    config_path = Path(config_file)

    if config_path.exists():
        return False

    config_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return True