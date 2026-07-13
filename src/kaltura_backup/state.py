"""
Persistent state manager for the Kaltura Backup application.

The StateManager maintains the persistent backup state, allowing
interrupted backups to resume without repeating completed work.

Features
--------
- Thread-safe
- Atomic writes
- Crash recovery
- Automatic state creation
- Partner validation
- State format versioning
- Statistics persistence
- BackupEntry persistence

Only this module is allowed to read or write backup_state.json.
"""

from __future__ import annotations

import json
import os
#from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from .config import Configuration
from .exceptions import StateError
from .models import BackupEntry
from .models import BackupStatistics
from .models import BackupStatus
from .models import Statistic

STATE_FORMAT_VERSION = 1


class StateManager:
    """
    Manages backup_state.json.

    All modifications are protected by a recursive lock to support
    concurrent worker threads.
    """

    def __init__(
        self,
        configuration: Configuration,
    ) -> None:

        self._configuration = configuration

        self._lock = RLock()

        self._dirty = False

        self._entries: dict[str, BackupEntry] = {}

        self._statistics = BackupStatistics()

        self._state_file = (
            configuration.paths.state_file
        )

        self._temporary_file = Path(
            str(self._state_file) + ".tmp"
        )

        self._created_at = int(
            datetime.now(UTC).timestamp()
        )

        self._updated_at = self._created_at
        self._last_save = self._created_at
    # -------------------------------------------------------------

    @property
    def statistics(self) -> BackupStatistics:
        """
        Return runtime statistics.
        """

        return self._statistics

    # -------------------------------------------------------------

    @property
    def entries(self) -> dict[str, BackupEntry]:
        """
        Return all known entries.
        """

        return self._entries

    # -------------------------------------------------------------

    def exists(
        self,
        entry_id: str,
    ) -> bool:
        """
        Return True if the entry exists.
        """

        return entry_id in self._entries

    # -------------------------------------------------------------

    def get(
        self,
        entry_id: str,
    ) -> BackupEntry | None:
        """
        Retrieve an entry.
        """

        return self._entries.get(entry_id)

    # -------------------------------------------------------------

    def add(
        self,
        entry: BackupEntry,
    ) -> None:
        """
        Add a new entry.
        """

        with self._lock:

            self._entries[
                entry.entry_id
            ] = entry

            self._dirty = True

    # -------------------------------------------------------------

    def remove(
        self,
        entry_id: str,
    ) -> None:
        """
        Remove an entry.
        """

        with self._lock:

            self._entries.pop(
                entry_id,
                None,
            )

            self._dirty = True

    # -------------------------------------------------------------

    def update(
        self,
        entry: BackupEntry,
    ) -> None:
        """
        Replace an existing entry.
        """

        with self._lock:

            self._entries[
                entry.entry_id
            ] = entry

            self._dirty = True

    # -------------------------------------------------------------

    def mark_clean(self) -> None:
        """
        Reset dirty flag.
        """

        self._dirty = False
        self._last_save = self._updated_at

    # -------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        """
        Return True when state has changed.
        """

        return self._dirty

    # -------------------------------------------------------------

    def load(self) -> None:
        """
        Load state from disk.

        If no state exists a new one is created.
        """

        with self._lock:

            self._recover()

            if not self._state_file.exists():
                self._dirty = True
                return

            try:

                with self._state_file.open(
                    "r",
                    encoding="utf-8",
                ) as fp:

                    data = json.load(fp)

            except Exception as exc:

                raise StateError(
                    f"Unable to read state file: {exc}"
                ) from exc

            self._load_from_dict(data)

            self._dirty = False

    # -------------------------------------------------------------

    def save(self) -> None:
        """
        Save state atomically.

        Does nothing when nothing has changed.
        """

        with self._lock:

            if not self._dirty:
                return

            self._updated_at = int(
                datetime.now(
                    UTC
                ).timestamp()
            )

            data = self._to_dict()

            self._write_atomic(data)

            self._dirty = False

    # -------------------------------------------------------------
    # Internal methods
    # -------------------------------------------------------------
    def _to_dict(self) -> dict[str, Any]: # ident removed
        """
        Convert the complete state into a JSON-serializable dictionary.
        """

        return {
            "format_version": STATE_FORMAT_VERSION,
            "partner_id": (
                self._configuration.connection.partner_id
            ),
            "created_at": self._created_at,
            "updated_at": self._updated_at,
            "statistics": self._statistics.to_dict(),
            "entries": {
                entry_id: entry.to_dict()
                for entry_id, entry in self._entries.items()
            },
        }

    # -------------------------------------------------------------

    def _load_from_dict(
        self,
        data: dict[str, Any],
    ) -> None:
        """
        Restore the complete state from a dictionary.
        """

        version = data.get("format_version")

        if version != STATE_FORMAT_VERSION:
            raise StateError(
                f"Unsupported state format version: {version}"
            )

        partner = data.get("partner_id")

        if (
            partner
            != self._configuration.connection.partner_id
        ):
            raise StateError(
                "State file belongs to another partner."
            )

        self._created_at = data.get(
            "created_at",
            self._created_at,
        )

        self._updated_at = data.get(
            "updated_at",
            self._updated_at,
        )

        statistics = data.get("statistics", {})

        self._statistics = BackupStatistics.from_dict(
            statistics
        )

        if self._statistics.finished:

            self._statistics.finished = (
                datetime.fromtimestamp(
                    int(
                        self._statistics.finished
                    ),
                    UTC,
                )
                if self._statistics.finished.isdigit()
                else datetime.fromisoformat(
                    self._statistics.finished
                )
                )

        self._entries.clear()

        for (
            entry_id,
            entry_data,
        ) in data.get(
            "entries",
            {},
        ).items():

            self._entries[
                entry_id
            ] = BackupEntry.from_dict(
                entry_data
            )

    # -------------------------------------------------------------

    def _write_atomic(
        self,
        data: dict[str, Any],
    ) -> None:
        """
        Write the state atomically.

        The state is first written to a temporary file.
        After flushing it to disk the temporary file replaces
        the existing state file.
        """

        try:

            with self._temporary_file.open(
                "w",
                encoding="utf-8",
            ) as fp:

                json.dump(
                    data,
                    fp,
                    indent=2,
                    sort_keys=True,
                )

                fp.flush()

                os.fsync(
                    fp.fileno()
                )

            os.replace(
                self._temporary_file,
                self._state_file,
            )

        except Exception as exc:

            raise StateError(
                f"Unable to write state file: {exc}"
            ) from exc

    # -------------------------------------------------------------

    def _recover(self) -> None:
        """
        Recover from an interrupted save operation.

        If a temporary file exists it replaces the main
        state file.
        """

        if not self._temporary_file.exists():
            return

        if not self._state_file.exists():

            os.replace(
                self._temporary_file,
                self._state_file,
            )

            return

        if (
            self._temporary_file.stat().st_mtime
            > self._state_file.stat().st_mtime
        ):

            os.replace(
                self._temporary_file,
                self._state_file,
            )

        else:

            self._temporary_file.unlink(
                missing_ok=True
            )

    # -------------------------------------------------------------

    def clear(self) -> None:
        """
        Remove all entries and reset statistics.
        """

        with self._lock:

            self._entries.clear()

            self._statistics = BackupStatistics()

            self._dirty = True

    # -------------------------------------------------------------

    def completed_entries(self) -> list[BackupEntry]:
        """
        Return completed entries.
        """

        return [
            entry
            for entry in self._entries.values()
            if entry.status is BackupStatus.COMPLETED # replaces entry.status.name == "COMPLETED"
        ]

    # -------------------------------------------------------------

    def failed_entries(self) -> list[BackupEntry]:
        """
        Return failed entries.
        """

        return [
            entry
            for entry in self._entries.values()
            if entry.status.name == "FAILED"
        ]

    # -------------------------------------------------------------

    def pending_entries(self) -> list[BackupEntry]:
       """
       Return entries still requiring processing.
       """

       return [
           entry
           for entry in self._entries.values()
           if entry.status not in (
               BackupStatus.COMPLETED,
               BackupStatus.SKIPPED,
           )
       ]

    # -------------------------------------------------------------

    def flush(self) -> None:
        """
        Force a state save regardless of the dirty flag.
        """

        with self._lock:

            self._dirty = True

            self.save()

    # -------------------------------------------------------------

    def increment_statistic(
        self,
        statistic: Statistic | str,
        amount: int = 1,
    ) -> None:
        """
        Increment a statistic by name.
        """

        with self._lock:
            statistic_name = (
                statistic.value
                if isinstance(statistic, Statistic)
                else statistic
            )

            if not hasattr(
                self._statistics,
                statistic_name,
            ):
                raise StateError(
                    f"Unknown statistic '{statistic_name}'"
                )

            value = getattr(
                self._statistics,
                statistic_name,
            )

            if not isinstance(
                value,
                int,
            ):
                raise StateError(
                    f"Statistic '{statistic_name}' is not numeric."
                )

            setattr(
                self._statistics,
                statistic_name,
                value + amount,
            )

            self._dirty = True
    def should_autosave(
        self,
        interval_seconds: int = 30,
    ) -> bool:
        """
        Return True when an autosave should be performed.
        """

        if not self._dirty:
            return False

        now = int(
            datetime.now(
                UTC
            ).timestamp()
        )

        return (
            now - self._last_save
        ) >= interval_seconds