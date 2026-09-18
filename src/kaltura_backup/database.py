from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree

from .config import Configuration

try:  # pragma: no cover - optional dependency
    import mysql.connector  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    mysql = None  # type: ignore


class DatabaseManager:
    """Small MySQL-backed sync layer for Kaltura baseEntry metadata."""

    _COLUMN_ALIASES = {
        "id": "EntryId",
        "entryid": "EntryId",
        "entry_id": "EntryId",
        "name": "Name",
        "description": "Description",
        "partnerid": "PartnerId",
        "userid": "UserId",
        "creatorid": "CreatorId",
        "tags": "Tags",
        "admintags": "AdminTags",
        "status": "Status",
        "type": "Type",
        "createdat": "CreatedAt",
        "updatedat": "UpdatedAt",
        "downloadurl": "DownloadUrl",
        "thumbnailurl": "ThumbnailUrl",
        "dataurl": "DataUrl",
        "referenceid": "ReferenceId",
        "mediatype": "MediaType",
        "duration": "Duration",
        "width": "Width",
        "height": "Height",
    }

    _MANAGED_COLUMNS = {
        "EntryId",
        "CreatedAtHR",
        "UpdatedAtHR",
        "EntryUpdated",
        "IsDeleted",
        "IsDeletedDate",
        "RawXml",
    }

    def __init__(self, configuration: Configuration) -> None:
        self._configuration = configuration
        self._database = configuration.database
        if self._database is None:
            raise ValueError("Database configuration is not enabled.")

    def connect(self):
        if mysql is None:
            raise RuntimeError(
                "mysql-connector-python is required for database sync. "
                "Install it with: python -m pip install mysql-connector-python"
            )

        return mysql.connector.connect(
            host=self._database.host,
            port=self._database.port,
            user=self._database.user,
            password=self._database.password,
            database=self._database.database,
            autocommit=True,
        )

    def ensure_schema(self) -> None:
        connection = self.connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS kaltura_entries (
                    EntryId VARCHAR(255) NOT NULL PRIMARY KEY,
                    Name VARCHAR(255) NULL,
                    Description TEXT NULL,
                    PartnerId BIGINT NULL,
                    UserId VARCHAR(255) NULL,
                    CreatorId VARCHAR(255) NULL,
                    Tags TEXT NULL,
                    AdminTags TEXT NULL,
                    Status INT NULL,
                    Type INT NULL,
                    CreatedAt BIGINT NULL,
                    UpdatedAt BIGINT NULL,
                    CreatedAtHR VARCHAR(50) NULL,
                    UpdatedAtHR VARCHAR(50) NULL,
                    DownloadUrl TEXT NULL,
                    ThumbnailUrl TEXT NULL,
                    DataUrl TEXT NULL,
                    ReferenceId TEXT NULL,
                    MediaType INT NULL,
                    Duration INT NULL,
                    Width INT NULL,
                    Height INT NULL,
                    EntryUpdated VARCHAR(20) NULL,
                    IsDeleted TINYINT(1) NOT NULL DEFAULT 0,
                    IsDeletedDate VARCHAR(20) NULL,
                    RawXml LONGTEXT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS db_sync_state (
                    id INT NOT NULL PRIMARY KEY,
                    last_sync_date VARCHAR(20) NULL,
                    last_sync_at DATETIME NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute("SHOW COLUMNS FROM kaltura_entries")
            columns = {str(row[0]).lower() for row in cursor.fetchall()}
            if "id" in columns:
                cursor.execute("ALTER TABLE kaltura_entries DROP COLUMN ID")
        finally:
            cursor.close()
            connection.close()

    @staticmethod
    def _parse_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_timestamp(value: Any) -> str | None:
        if value in (None, ""):
            return None
        try:
            seconds = int(str(value).strip())
            return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _entry_updated_today() -> str:
        return datetime.now(UTC).strftime("%d-%m-%Y")

    @staticmethod
    def _normalize_tag(tag: str) -> str:
        if not tag:
            return ""
        return re.sub(r"\{.*?\}", "", tag)

    @classmethod
    def _column_name(cls, name: str) -> str:
        normalized = cls._normalize_tag(str(name)).strip()
        alias = cls._COLUMN_ALIASES.get(normalized.lower())
        if alias:
            return alias
        normalized = re.sub(r"[^A-Za-z0-9_]", "_", normalized)
        normalized = re.sub(r"_+", "_", normalized).strip("_") or "XmlField"
        if normalized[0].isdigit():
            normalized = f"Xml_{normalized}"
        return normalized[:64]

    @classmethod
    def _extract_xml_fields(cls, xml_payload: str | bytes | ElementTree.Element) -> dict[str, Any]:
        if isinstance(xml_payload, (bytes, bytearray)):
            xml_payload = xml_payload.decode("utf-8", errors="replace")

        if isinstance(xml_payload, ElementTree.Element):
            root = xml_payload
        else:
            root = ElementTree.fromstring(xml_payload)

        result: dict[str, Any] = {}
        for node in root.iter():
            if list(node):
                continue
            tag = cls._normalize_tag(node.tag)
            if tag in {"result", "relatedObjects"}:
                continue
            result[tag] = (node.text or "").strip()

        if not result and root.tag:
            result[root.tag] = (root.text or "").strip()

        return result

    def _ensure_entry_columns(self, cursor: Any, entry: dict[str, Any]) -> None:
        cursor.execute("SHOW COLUMNS FROM kaltura_entries")
        existing = {str(row[0]).lower() for row in cursor.fetchall()}
        for source_name in entry:
            column = self._column_name(source_name)
            if column == "EntryId" or column.lower() in existing:
                continue
            cursor.execute(
                f"ALTER TABLE kaltura_entries ADD COLUMN `{column}` TEXT NULL"
            )
            existing.add(column.lower())

    def upsert_entry(self, entry_payload: dict[str, Any] | str | bytes | ElementTree.Element) -> None:
        if isinstance(entry_payload, (str, bytes, bytearray, ElementTree.Element)):
            entry = self._extract_xml_fields(entry_payload)
        else:
            entry = dict(entry_payload)

        normalized_entry = {
            self._column_name(name): value for name, value in entry.items()
        }
        entry_id = str(normalized_entry.get("EntryId") or "").strip()
        if not entry_id:
            raise ValueError("Kaltura entry payload is missing an entry ID")

        created_at = self._parse_int(normalized_entry.get("CreatedAt"))
        updated_at = self._parse_int(normalized_entry.get("UpdatedAt"))
        created_at_hr = self._parse_timestamp(created_at) if created_at is not None else None
        updated_at_hr = self._parse_timestamp(updated_at) if updated_at is not None else None
        entry_updated = self._entry_updated_today()

        connection = self.connect()
        cursor = connection.cursor()
        try:
            self._ensure_entry_columns(cursor, entry)
            values = dict(normalized_entry)
            values.update(
                {
                    "EntryId": entry_id,
                    "CreatedAt": created_at,
                    "UpdatedAt": updated_at,
                    "CreatedAtHR": created_at_hr,
                    "UpdatedAtHR": updated_at_hr,
                    "EntryUpdated": entry_updated,
                    "IsDeleted": 0,
                    "IsDeletedDate": None,
                    "RawXml": self._raw_payload(entry_payload),
                }
            )
            columns = [column for column in values if column != "EntryId"]
            insert_columns = ["EntryId", *columns]
            assignments = ", ".join(
                f"`{column}` = VALUES(`{column}`)"
                for column in columns
                if column not in {"IsDeleted", "IsDeletedDate"}
            )
            assignments = f"{assignments}, IsDeleted = 0, IsDeletedDate = NULL"
            cursor.execute(
                f"INSERT INTO kaltura_entries ({', '.join(f'`{column}`' for column in insert_columns)}) "
                f"VALUES ({', '.join(['%s'] * len(insert_columns))}) "
                f"ON DUPLICATE KEY UPDATE {assignments}",
                tuple([entry_id, *[values[column] for column in columns]]),
            )
        finally:
            cursor.close()
            connection.close()

    @staticmethod
    def _raw_payload(payload: Any) -> str:
        if isinstance(payload, ElementTree.Element):
            return ElementTree.tostring(payload, encoding="unicode")
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return str(payload)

    def mark_deleted(self, entry_id: str, deleted_date: str | None = None) -> None:
        timestamp = deleted_date or datetime.now(UTC).strftime("%d-%m-%Y")
        connection = self.connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "UPDATE kaltura_entries SET IsDeleted = 1, IsDeletedDate = %s, EntryUpdated = %s WHERE EntryId = %s OR ID = %s",
                (timestamp, datetime.now(UTC).strftime("%d-%m-%Y"), entry_id, entry_id),
            )
        finally:
            cursor.close()
            connection.close()

    def get_stale_entries(self) -> list[str]:
        connection = self.connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT EntryId FROM kaltura_entries WHERE IsDeleted = 0 AND EntryUpdated < %s ORDER BY EntryId",
                (datetime.now(UTC).strftime("%d-%m-%Y"),),
            )
            rows = cursor.fetchall()
            return [str(row["EntryId"]) for row in rows]
        finally:
            cursor.close()
            connection.close()

    def get_last_sync_date(self) -> str | None:
        connection = self.connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT last_sync_date FROM db_sync_state WHERE id = 1 LIMIT 1"
            )
            row = cursor.fetchone()
            return str(row["last_sync_date"]) if row and row.get("last_sync_date") else None
        finally:
            cursor.close()
            connection.close()

    def update_last_sync_date(self, sync_date: str | None = None) -> None:
        value = sync_date or datetime.now(UTC).strftime("%d-%m-%Y")
        connection = self.connect()
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO db_sync_state (id, last_sync_date, last_sync_at) VALUES (1, %s, NOW()) ON DUPLICATE KEY UPDATE last_sync_date = VALUES(last_sync_date), last_sync_at = NOW()",
                (value,),
            )
        finally:
            cursor.close()
            connection.close()

    def sync_if_due(self, client_manager: Any, force: bool = False, page_size: int = 500) -> bool:
        self.ensure_schema()
        today = datetime.now(UTC).strftime("%d-%m-%Y")
        if not force:
            last_sync = self.get_last_sync_date()
            if last_sync == today:
                return False

        synced = self.sync_from_api(client_manager, page_size=page_size)
        self.update_last_sync_date(today)
        return bool(synced)

    def sync_from_api(self, client_manager: Any, page_size: int = 500) -> list[str]:
        self.ensure_schema()
        entries = []
        pager = client_manager._initialize_pager(page_size=page_size, page_index=1) if hasattr(client_manager, "_initialize_pager") else None
        while True:
            if pager is not None:
                response = client_manager.list_all_entries(page_size=page_size) if hasattr(client_manager, "list_all_entries") else client_manager.list_entries(None, pager)
                if not isinstance(response, list):
                    response = list(response) if response is not None else []
                batch = response
            else:
                batch = client_manager.list_entries() if hasattr(client_manager, "list_entries") else []
            if not batch:
                break
            entries.extend(batch)
            if pager is None:
                break
            if len(batch) < page_size:
                break
            if hasattr(client_manager, "_increment_pager"):
                client_manager._increment_pager(pager)
            else:
                break

        for item in entries:
            if hasattr(item, "getId"):
                payload = item
            elif isinstance(item, dict):
                payload = item
            else:
                payload = {"id": str(item)}
            self.upsert_entry(payload)

        return [str(item.get("id") or item.get("EntryId") or item.get("entry_id") or "") for item in entries if item is not None]
