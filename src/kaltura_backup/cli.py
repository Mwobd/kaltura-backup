from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable
import sys

from . import __version__
from .backup import BackupManager
from .config import load_configuration
from .database import DatabaseManager
from .exceptions import BackupError, ConfigurationError
from .logging_utils import initialize_logger
from .state import StateManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Kaltura backup workflow",
        epilog="Use --config to point to a specific configuration file.",
    )
    parser.add_argument(
        "--config",
        default="config.ini",
        help="Path to the configuration file (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without writing backup artifacts",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry only entries that were previously marked as failed in the saved state",
    )
    parser.add_argument(
        "--db-sync",
        "--sync-db",
        "--database-sync",
        action="store_true",
        dest="database_sync",
        help="Sync Kaltura entries to the configured MySQL database once per day",
    )
    parser.add_argument(
        "--force-db-sync",
        "--force-sync-db",
        action="store_true",
        dest="force_database_sync",
        help="Force a database sync even when it has already run today",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"kaltura-backup {__version__}",
    )
    return parser


def main(
    argv: list[str] | None = None,
    client_manager_factory: Callable[[Any, Any], Any] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        configuration = load_configuration(Path(args.config))
        logger = initialize_logger(configuration)
        print(f"Starting Kaltura backup using configuration: {args.config}")
        state_manager = StateManager(configuration)
        state_manager.load()

        if client_manager_factory is not None:
            client_manager = client_manager_factory(configuration, logger)
        else:
            client_manager = None
            try:
                from .client import KalturaClientManager

                client_manager = KalturaClientManager(configuration, logger)
            except ImportError:  # pragma: no cover - fallback for missing SDK
                client_manager = type(
                    "ClientManagerStub",
                    (),
                    {
                        "connect": lambda self: None,
                        "disconnect": lambda self: None,
                        "list_entries": lambda self, *args, **kwargs: [],
                        "get_entry": lambda self, entry_id: {"id": entry_id, "name": entry_id},
                    },
                )()

        if args.database_sync:
            if configuration.database is None:
                print("Database sync requested but no [mysql] section is configured.", file=sys.stderr)
                return 2
            try:
                DatabaseManager(configuration).sync_if_due(client_manager, force=args.force_database_sync)
                print("Database sync completed.")
                return 0
            except Exception as exc:  # pragma: no cover - surfaced in CLI output
                print(f"Database sync error: {exc}", file=sys.stderr)
                return 1

        if configuration.database is not None:
            try:
                DatabaseManager(configuration).sync_if_due(client_manager, force=args.force_database_sync)
            except Exception as exc:
                print(f"Database sync skipped because of an error: {exc}", file=sys.stderr)

        manager = BackupManager(
            configuration=configuration,
            state_manager=state_manager,
            client_manager=client_manager,
            logger=logger,
            dry_run=args.dry_run,
        )

        if args.retry_failed:
            failed_entries = state_manager.failed_entries()
            if not failed_entries:
                print("No failed entries found in state to retry.")
                return 0
            manager.run(failed_entries)
        else:
            manager.run()
        return 0
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except BackupError as exc:
        print(f"Backup error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
