from pathlib import Path
import sys
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kaltura_backup.database import DatabaseManager


def test_extract_xml_fields_includes_nested_leaf_tags() -> None:
    payload = """
    <entry>
        <id>entry-123</id>
        <name>Example</name>
        <customData>
            <department>Archive</department>
            <retentionYears>7</retentionYears>
        </customData>
    </entry>
    """

    fields = DatabaseManager._extract_xml_fields(payload)

    assert fields == {
        "id": "entry-123",
        "name": "Example",
        "department": "Archive",
        "retentionYears": "7",
    }


def test_extract_xml_fields_preserves_escaped_html_values() -> None:
    payload = """
    <entry>
        <id>entry-123</id>
        <dataUrl>&lt;div&gt;videoplayback&lt;/div&gt;</dataUrl>
    </entry>
    """

    fields = DatabaseManager._extract_xml_fields(payload)

    assert fields["dataUrl"] == "<div>videoplayback</div>"


def test_id_maps_only_to_entry_id() -> None:
    assert DatabaseManager._column_name("id") == "EntryId"
    assert DatabaseManager._column_name("EntryId") == "EntryId"
    assert DatabaseManager._column_name("ID") == "EntryId"


def test_unknown_xml_tags_get_safe_column_names() -> None:
    assert DatabaseManager._column_name("custom-field") == "custom_field"
    assert DatabaseManager._column_name("123field") == "Xml_123field"


def test_sync_from_api_calls_all_entries_once(monkeypatch) -> None:
    manager = object.__new__(DatabaseManager)
    manager._logger = None
    calls = []

    class Client:
        def list_all_entries(self, page_size: int):
            calls.append(page_size)
            return [{"id": "entry-123"}]

    monkeypatch.setattr(manager, "ensure_schema", lambda: None)
    monkeypatch.setattr(manager, "upsert_entry", lambda payload: None)

    assert manager.sync_from_api(Client(), page_size=500) == ["entry-123"]
    assert calls == [500]


def test_sdk_entry_objects_are_converted_before_upsert(monkeypatch) -> None:
    manager = object.__new__(DatabaseManager)
    manager._logger = None
    stored = []

    class SdkEntry:
        def __init__(self) -> None:
            self.id = "entry-123"
            self.name = "Example"
            self.createdAt = 100
            self.unset = NotImplemented

    class Client:
        def list_all_entries(self, page_size: int):
            return [SdkEntry()]

    monkeypatch.setattr(manager, "ensure_schema", lambda: None)
    monkeypatch.setattr(manager, "upsert_entry", stored.append)

    assert manager.sync_from_api(Client()) == ["entry-123"]
    assert stored[0] == {"id": "entry-123", "name": "Example", "createdAt": 100}


def test_sdk_enum_values_are_unwrapped_for_integer_columns() -> None:
    class SdkEnum:
        value = "2"

        def getValue(self):
            return self.value

    status = SdkEnum()

    assert DatabaseManager._parse_int(status) == 2
    assert DatabaseManager._database_value(status) == "2"


def test_unix_epochs_are_converted_to_utc_timestamps() -> None:
    assert DatabaseManager._parse_timestamp(0) == "1970-01-01 00:00:00 UTC"
    assert DatabaseManager._parse_timestamp(1726574400) == "2024-09-17 12:00:00 UTC"


def test_schema_migration_adds_human_readable_timestamp_columns(monkeypatch) -> None:
    manager = object.__new__(DatabaseManager)
    manager._logger = None

    class Cursor:
        def __init__(self) -> None:
            self.statements = []

        def execute(self, statement, *parameters):
            self.statements.append(statement)

        def fetchall(self):
            return [("EntryId", "varchar(255)"), ("CreatedAt", "bigint")]

        def close(self):
            return None

    class Connection:
        def __init__(self) -> None:
            self.cursor_instance = Cursor()

        def cursor(self):
            return self.cursor_instance

        def close(self):
            return None

    connection = Connection()
    manager._database = object()
    monkeypatch.setattr(manager, "connect", lambda: connection)
    monkeypatch.setattr(manager, "_log", lambda *args: None)

    manager.ensure_schema()

    assert any("ADD COLUMN `CreatedAtHR` VARCHAR(50) NULL" in statement for statement in connection.cursor_instance.statements)
    assert any("ADD COLUMN `UpdatedAtHR` VARCHAR(50) NULL" in statement for statement in connection.cursor_instance.statements)


def test_get_backup_entry_ids_selects_active_type_one_rows(monkeypatch) -> None:
    manager = object.__new__(DatabaseManager)
    manager._logger = None

    class Cursor:
        def __init__(self) -> None:
            self.statement = ""
            self.parameters = None

        def execute(self, statement, parameters):
            self.statement = statement
            self.parameters = parameters

        def fetchall(self):
            return [{"EntryId": "entry-1"}, {"EntryId": "entry-2"}]

        def close(self):
            return None

    class Connection:
        def __init__(self) -> None:
            self.cursor_instance = Cursor()

        def cursor(self, dictionary=False):
            return self.cursor_instance

        def close(self):
            return None

    connection = Connection()
    monkeypatch.setattr(manager, "connect", lambda: connection)

    assert manager.get_backup_entry_ids() == ["entry-1", "entry-2"]
    assert "Type = %s" in connection.cursor_instance.statement
    assert "IsDeleted = 0" in connection.cursor_instance.statement
    assert connection.cursor_instance.parameters == (1,)


def test_caption_list_xml_extracts_one_record_per_asset() -> None:
    payload = """
    <result><objects>
        <item><id>caption-1</id><entryId>entry-1</entryId><language>en</language></item>
        <item><id>caption-2</id><entryId>entry-1</entryId><language>nl</language></item>
    </objects></result>
    """

    records = DatabaseManager._caption_xml_records(ElementTree.fromstring(payload))

    assert records == [
        {"id": "caption-1", "entryId": "entry-1", "language": "en"},
        {"id": "caption-2", "entryId": "entry-1", "language": "nl"},
    ]


def test_serve_as_json_xml_response_is_not_stored(monkeypatch) -> None:
    manager = object.__new__(DatabaseManager)
    manager._logger = None

    def fail_connect():
        raise AssertionError("serveAsJson response must not use the database")

    monkeypatch.setattr(manager, "connect", fail_connect)
    manager.store_xml_response("caption_captionasset_serveAsJson", b"<result />")


def test_thumbnail_and_attachment_lists_use_separate_tables(monkeypatch) -> None:
    manager = object.__new__(DatabaseManager)
    manager._logger = None
    statements = []

    class Cursor:
        def execute(self, statement, *parameters):
            statements.append(statement)

        def fetchall(self):
            return [("CaptionAssetId", "varchar(255)"), ("CreatedAt", "bigint")]

        def close(self):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            return None

    monkeypatch.setattr(manager, "connect", lambda: Connection())
    payload = b"<result><objects><item><id>asset-1</id><createdAt>0</createdAt><updatedAt>1</updatedAt></item></objects></result>"

    manager.store_xml_response("thumbasset_thumbasset_list", payload)
    manager.store_xml_response("attachment_attachmentasset_list", payload)

    assert any("INSERT INTO kaltura_thumb_assets" in statement for statement in statements)
    assert any("INSERT INTO kaltura_attachment_assets" in statement for statement in statements)
    assert any("CreatedAtHR" in statement for statement in statements)


def test_unknown_columns_use_longtext_for_large_kaltura_values() -> None:
    manager = object.__new__(DatabaseManager)

    class Cursor:
        def __init__(self) -> None:
            self.statements = []

        def execute(self, statement):
            self.statements.append(statement)

        def fetchall(self):
            return [("EntryId", "varchar(255)"), ("dataContent", "text")]

    cursor = Cursor()
    manager._ensure_entry_columns(cursor, {"dataContent": "large value"})

    assert any("MODIFY COLUMN `dataContent` LONGTEXT" in statement for statement in cursor.statements)
