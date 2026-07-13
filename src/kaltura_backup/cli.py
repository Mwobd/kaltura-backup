from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .backup import BackupManager
from .config import load_configuration
from .exceptions import BackupError, ConfigurationError
from .logging_utils import initialize_logger
from .state import StateManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Kaltura backup workflow")
    parser.add_argument("--config", default="config.ini", help="Path to the configuration file")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        configuration = load_configuration(Path(args.config))
        logger = initialize_logger(configuration)
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
        )

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
