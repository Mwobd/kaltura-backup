from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree

from .config import Configuration
from .logging_utils import BackupLogger, EventId

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

    _FIXED_COLUMNS = {
        *_COLUMN_ALIASES.values(),
        *_MANAGED_COLUMNS,
    }

    def __init__(self, configuration: Configuration, logger: BackupLogger | None = None) -> None:
        self._configuration = configuration
        self._database = configuration.database
        self._logger = logger
        if self._database is None:
            raise ValueError("Database configuration is not enabled.")

    def connect(self):
        if mysql is None:
            raise RuntimeError(
                "mysql-connector-python is required for database sync. "
                "Install it with: python -m pip install mysql-connector-python"
            )

        self._log(
            EventId.CONNECTING,
            f"Connecting to MySQL database {self._database.database} at {self._database.host}:{self._database.port}.",
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
        self._log(EventId.CONFIGURATION_LOADED, "Checking database schema.")
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
                CREATE TABLE IF NOT EXISTS kaltura_caption_assets (
                    CaptionAssetId VARCHAR(255) NOT NULL PRIMARY KEY,
                    CreatedAt BIGINT NULL,
                    UpdatedAt BIGINT NULL,
                    CreatedAtHR VARCHAR(50) NULL,
                    UpdatedAtHR VARCHAR(50) NULL,
                    RawXml LONGTEXT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            for table_name, id_column in (
                ("kaltura_thumb_assets", "ThumbAssetId"),
                ("kaltura_attachment_assets", "AttachmentAssetId"),
            ):
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table_name} (
                        {id_column} VARCHAR(255) NOT NULL PRIMARY KEY,
                        CreatedAt BIGINT NULL,
                        UpdatedAt BIGINT NULL,
                        CreatedAtHR VARCHAR(50) NULL,
                        UpdatedAtHR VARCHAR(50) NULL,
                        RawXml LONGTEXT NULL
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
            for table_name in (
                "kaltura_caption_assets",
                "kaltura_thumb_assets",
                "kaltura_attachment_assets",
            ):
                cursor.execute(f"SHOW COLUMNS FROM {table_name}")
                asset_columns = {str(row[0]).lower() for row in cursor.fetchall()}
                for column in ("CreatedAt", "UpdatedAt"):
                    if column.lower() not in asset_columns:
                        cursor.execute(
                            f"ALTER TABLE {table_name} ADD COLUMN `{column}` BIGINT NULL"
                        )
                for column in ("CreatedAtHR", "UpdatedAtHR"):
                    if column.lower() not in asset_columns:
                        cursor.execute(
                            f"ALTER TABLE {table_name} ADD COLUMN `{column}` VARCHAR(50) NULL"
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
            required_columns = {
                "CreatedAtHR": "VARCHAR(50) NULL",
                "UpdatedAtHR": "VARCHAR(50) NULL",
            }
            for column, definition in required_columns.items():
                if column.lower() not in columns:
                    cursor.execute(
                        f"ALTER TABLE kaltura_entries ADD COLUMN `{column}` {definition}"
                    )
            self._log(EventId.CONFIGURATION_LOADED, "Database schema check completed.")
        finally:
            cursor.close()
            connection.close()

    def store_xml_response(self, request_name: str, payload: Any) -> None:
        """Store XML from caption, thumbnail, and attachment asset list calls."""
        table_config = {
            "caption_captionasset_list": ("kaltura_caption_assets", "CaptionAssetId"),
            "caption_caption_asset_list": ("kaltura_caption_assets", "CaptionAssetId"),
            "thumbasset_thumbasset_list": ("kaltura_thumb_assets", "ThumbAssetId"),
            "thumbasset_thumb_asset_list": ("kaltura_thumb_assets", "ThumbAssetId"),
            "attachment_attachmentasset_list": ("kaltura_attachment_assets", "AttachmentAssetId"),
            "attachment_attachment_asset_list": ("kaltura_attachment_assets", "AttachmentAssetId"),
        }
        config = table_config.get(request_name.lower())
        if config is None:
            return
        table_name, id_column = config
        root = ElementTree.fromstring(payload)
        records = self._caption_xml_records(root)
        if not records:
            return

        connection = self.connect()
        cursor = connection.cursor()
        try:
            for record in records:
                asset_id = str(record.get("id") or record.get(id_column) or "").strip()
                if not asset_id:
                    continue
                values = {
                    (id_column if name.lower() == "id" else self._column_name(name)): self._database_value(value)
                    for name, value in record.items()
                }
                values[id_column] = asset_id
                created_at = self._parse_int(record.get("createdAt"))
                updated_at = self._parse_int(record.get("updatedAt"))
                values["CreatedAt"] = created_at
                values["UpdatedAt"] = updated_at
                values["CreatedAtHR"] = self._parse_timestamp(created_at)
                values["UpdatedAtHR"] = self._parse_timestamp(updated_at)
                values["RawXml"] = self._raw_payload(payload)
                self._ensure_asset_columns(cursor, table_name, values)
                columns = list(values)
                assignments = ", ".join(
                    f"`{column}` = VALUES(`{column}`)"
                    for column in columns
                    if column != "CaptionAssetId"
                )
                cursor.execute(
                    f"INSERT INTO {table_name} ({', '.join(f'`{column}`' for column in columns)}) "
                    f"VALUES ({', '.join(['%s'] * len(columns))}) "
                    f"ON DUPLICATE KEY UPDATE {assignments}",
                    tuple(values[column] for column in columns),
                )
        finally:
            cursor.close()
            connection.close()

    @classmethod
    def _caption_xml_records(cls, root: ElementTree.Element) -> list[dict[str, Any]]:
        items = [node for node in root.iter() if cls._normalize_tag(node.tag).lower() in {"item", "captionasset"}]
        return [cls._xml_record(item) for item in items]

    @classmethod
    def _xml_record(cls, node: ElementTree.Element) -> dict[str, Any]:
        record: dict[str, Any] = {}
        for leaf in node.iter():
            if list(leaf):
                continue
            tag = cls._normalize_tag(leaf.tag)
            if tag:
                record[tag] = (leaf.text or "").strip()
        return record

    @staticmethod
    def _ensure_asset_columns(cursor: Any, table_name: str, values: dict[str, Any]) -> None:
        cursor.execute(f"SHOW COLUMNS FROM {table_name}")
        existing = {str(row[0]).lower() for row in cursor.fetchall()}
        for column in values:
            if column.lower() in existing:
                continue
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN `{column}` LONGTEXT NULL")
            existing.add(column.lower())

    @staticmethod
    def _unwrap_sdk_value(value: Any) -> Any:
        getter = getattr(value, "getValue", None)
        if callable(getter):
            try:
                return getter()
            except TypeError:
                pass
        if hasattr(value, "value") and not isinstance(value, (str, bytes, bytearray)):
            return getattr(value, "value")
        return value

    @classmethod
    def _parse_int(cls, value: Any) -> int | None:
        value = cls._unwrap_sdk_value(value)
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

    @staticmethod
    def _sdk_object_fields(payload: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for name, value in vars(payload).items():
            if name.startswith("_") or name == "relatedObjects" or value is NotImplemented:
                continue
            if callable(value):
                continue
            fields[name] = value

        if fields:
            return fields

        for name in dir(payload):
            if not name.startswith("get") or len(name) <= 3:
                continue
            getter = getattr(payload, name, None)
            if not callable(getter):
                continue
            try:
                value = getter()
            except TypeError:
                continue
            if value is not None and value is not NotImplemented:
                fields[name[3].lower() + name[4:]] = value
        return fields

    @classmethod
    def _payload_to_mapping(cls, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return dict(payload)
        if isinstance(payload, (str, bytes, bytearray, ElementTree.Element)):
            return cls._extract_xml_fields(payload)
        return cls._sdk_object_fields(payload)

    @staticmethod
    def _database_value(value: Any) -> Any:
        value = DatabaseManager._unwrap_sdk_value(value)
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)

    def _ensure_entry_columns(self, cursor: Any, entry: dict[str, Any]) -> None:
        cursor.execute("SHOW COLUMNS FROM kaltura_entries")
        existing = {str(row[0]).lower(): str(row[1]).lower() for row in cursor.fetchall()}
        for source_name in entry:
            column = self._column_name(source_name)
            if column == "EntryId":
                continue
            existing_type = existing.get(column.lower())
            if existing_type is not None:
                if column not in self._FIXED_COLUMNS and existing_type != "longtext":
                    cursor.execute(
                        f"ALTER TABLE kaltura_entries MODIFY COLUMN `{column}` LONGTEXT NULL"
                    )
                    existing[column.lower()] = "longtext"
                continue
            cursor.execute(
                f"ALTER TABLE kaltura_entries ADD COLUMN `{column}` LONGTEXT NULL"
            )
            existing[column.lower()] = "longtext"

    def upsert_entry(self, entry_payload: dict[str, Any] | str | bytes | ElementTree.Element) -> None:
        entry = self._payload_to_mapping(entry_payload)

        normalized_entry = {
            self._column_name(name): self._unwrap_sdk_value(value)
            for name, value in entry.items()
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
            values = {column: self._database_value(value) for column, value in values.items()}
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

    def get_backup_entry_ids(self) -> list[str]:
        """Return active media entry IDs selected for the main backup run."""
        connection = self.connect()
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT EntryId FROM kaltura_entries "
                "WHERE Type = %s AND IsDeleted = 0 ORDER BY EntryId",
                (1,),
            )
            rows = cursor.fetchall()
            entry_ids = [str(row["EntryId"]) for row in rows if row.get("EntryId")]
            self._log(EventId.ENTRY_DISCOVERED, f"Selected {len(entry_ids)} Type=1 entries from database.")
            return entry_ids
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
        self._log(EventId.APPLICATION_START, "Starting database sync.")
        self.ensure_schema()
        today = datetime.now(UTC).strftime("%d-%m-%Y")
        if not force:
            last_sync = self.get_last_sync_date()
            if last_sync == today:
                self._log(EventId.ENTRY_SKIPPED, f"Database sync skipped; last sync was already recorded for {today}.")
                return False

        synced = self.sync_from_api(client_manager, page_size=page_size)
        self.update_last_sync_date(today)
        self._log(EventId.APPLICATION_STOP, f"Database sync completed; processed {len(synced)} entries.")
        return bool(synced)

    def sync_from_api(self, client_manager: Any, page_size: int = 500) -> list[str]:
        self.ensure_schema()
        if hasattr(client_manager, "list_all_entries"):
            self._log(EventId.ENTRY_DISCOVERED, f"Requesting all Kaltura entries with page size {page_size}.")
            response = client_manager.list_all_entries(page_size=page_size)
            entries = list(response or [])
            self._log(EventId.ENTRY_DISCOVERED, f"Kaltura returned {len(entries)} entries.")
        else:
            entries = list(client_manager.list_entries() if hasattr(client_manager, "list_entries") else [])
            self._log(EventId.ENTRY_DISCOVERED, f"Kaltura returned {len(entries)} entries without list_all_entries support.")

        for index, item in enumerate(entries, start=1):
            payload = self._payload_to_mapping(item)
            self.upsert_entry(payload)
            if index == 1 or index % 100 == 0 or index == len(entries):
                self._log(EventId.ENTRY_COMPLETED, f"Stored database entry {index}/{len(entries)}.")

        return [
            str(self._payload_to_mapping(item).get("id") or self._payload_to_mapping(item).get("EntryId") or "")
            for item in entries
            if item is not None
        ]

    def _log(self, event: EventId, message: str) -> None:
        if self._logger is not None:
            self._logger.info(event, message)
