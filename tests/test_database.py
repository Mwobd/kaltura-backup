from pathlib import Path
import sys

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
