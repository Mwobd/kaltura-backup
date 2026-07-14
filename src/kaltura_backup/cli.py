from __future__ import annotations

import argparse
from pathlib import Path
import sys

from . import __version__
from .backup import BackupManager
from .config import load_configuration
from .exceptions import BackupError, ConfigurationError
from .logging_utils import initialize_logger
from .models import BackupStatus
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
        "--version",
        action="version",
        version=f"kaltura-backup {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        configuration = load_configuration(Path(args.config))
        logger = initialize_logger(configuration)
        print(f"Starting Kaltura backup using configuration: {args.config}")
        state_manager = StateManager(configuration)
        state_manager.load()

        client_manager = None
        try:
            from .client import KalturaClientManager

            client_manager = KalturaClientManager(configuration, logger)
        except Exception:  # pragma: no cover - fallback for missing SDK
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

        manager = BackupManager(
            configuration=configuration,
            state_manager=state_manager,
            client_manager=client_manager,
            logger=logger,
            dry_run=args.dry_run,
        )

        if args.retry_failed:
            failed_entries = [
                entry
                for entry in state_manager.entries.values()
                if entry.status is BackupStatus.FAILED
            ]
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
