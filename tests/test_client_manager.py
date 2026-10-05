from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from kaltura_backup.client import KalturaClientManager, KalturaSession, _KalturaSdkLogger
from kaltura_backup.config import (
    Configuration,
    ConnectionConfig,
    PathConfig,
    DownloadConfig,
    ExportConfig,
    MetadataConfig,
    LoggingConfig,
)
from kaltura_backup.exceptions import ApiError, ClientError


class _FakeBaseEntry:
    def __init__(self) -> None:
        self.calls = []

    def list(self, filter_object, pager):
        self.calls.append((filter_object, pager))
        return [{"id": "1", "name": "demo"}]


class _FakeClient:
    def __init__(self) -> None:
        self.baseEntry = _FakeBaseEntry()


class _NullLogger:
    def __init__(self) -> None:
        self.debug_messages = []

    def debug(self, *args, **kwargs) -> None:
        self.debug_messages.append(args)

    def info(self, *args, **kwargs) -> None:
        return None

    def warning(self, *args, **kwargs) -> None:
        return None

    def error(self, *args, **kwargs) -> None:
        return None

    def exception(self, *args, **kwargs) -> None:
        return None

    def retry(self, *args, **kwargs) -> None:
        return None


def _make_configuration(tmp_path: Path) -> Configuration:
    return Configuration(
        connection=ConnectionConfig(
            partner_id=123,
            admin_secret="secret",
            service_url="https://example.invalid",
        ),
        paths=PathConfig(
            backup_dir=tmp_path / "backup",
            csv_dir=tmp_path / "csv",
            log_dir=tmp_path / "logs",
            report_dir=tmp_path / "reports",
            state_file=tmp_path / "backup_state.json",
            xml_dir=tmp_path / "xml",
        ),
        download=DownloadConfig(
            workers=1,
            retry_count=0,
            retry_delay_seconds=1,
            timeout=5,
            skip_older_than_hours=0,
            resume_downloads=False,
            verify_checksum=False,
        ),
        export=ExportConfig(
            save_metadata=False,
            save_api_responses=False,
            save_captions=False,
            save_thumbnails=False,
            save_attachments=False,
        ),
        metadata=MetadataConfig(profile_fields={}),
        logging=LoggingConfig(level="INFO", keep_logs=1),
    )


class _FakeEntryService:
    def __init__(self) -> None:
        self.calls = []

    def list(self, filter_object, pager):
        self.calls.append((filter_object, pager))
        return [{"id": "1", "name": "demo"}]

    def get(self, entry_id):
        self.calls.append(entry_id)
        return {"id": entry_id, "name": "demo"}


class _FakeClientWithEntryService:
    def __init__(self) -> None:
        self.entry = _FakeEntryService()


class _FakeClientWithoutEntryService:
    pass


def test_list_entries_accepts_default_arguments(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    manager = KalturaClientManager(configuration, _NullLogger())
    fake_client = _FakeClient()
    manager._pool = [KalturaSession(fake_client, "ks")]
    manager._connected = True

    result = manager.list_entries()

    assert result == [{"id": "1", "name": "demo"}]
    assert fake_client.baseEntry.calls[0][0].typeIn == "1"
    assert fake_client.baseEntry.calls[0][1] is None


def test_list_entries_falls_back_to_entry_service(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    manager = KalturaClientManager(configuration, _NullLogger())
    fake_client = _FakeClientWithEntryService()
    manager._pool = [KalturaSession(fake_client, "ks")]
    manager._connected = True

    result = manager.list_entries()

    assert result == [{"id": "1", "name": "demo"}]
    assert fake_client.entry.calls[0][0].typeIn == "1"
    assert fake_client.entry.calls[0][1] is None


def test_list_entries_raises_client_error_when_no_entry_service(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    manager = KalturaClientManager(configuration, _NullLogger())
    fake_client = _FakeClientWithoutEntryService()
    manager._pool = [KalturaSession(fake_client, "ks")]
    manager._connected = True

    with pytest.raises(ClientError):
        manager.list_entries()


def test_get_caption_json_uses_serve_as_json(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    manager = KalturaClientManager(configuration, _NullLogger())

    class CaptionAssetService:
        def serveAsJson(self, caption_asset_id):
            assert caption_asset_id == "caption-1"
            return "https://example.invalid/caption.json"

    class CaptionService:
        captionAsset = CaptionAssetService()

    class Client:
        caption = CaptionService()

    manager._pool = [KalturaSession(Client(), "ks")]
    manager._connected = True

    assert manager.get_caption_json("caption-1") == "https://example.invalid/caption.json"


def test_get_metadata_xml_fetches_served_url(tmp_path: Path, monkeypatch) -> None:
    from kaltura_backup import client as client_module

    configuration = _make_configuration(tmp_path)

    class MetadataService:
        def serve(self, metadata_id):
            assert metadata_id == "metadata-1"
            return "https://example.invalid/metadata.xml"

    class MetadataPlugin:
        metadata = MetadataService()

    class Client:
        metadata = MetadataPlugin()

    class Response:
        text = "<metadata><Title>Example</Title></metadata>"

        def raise_for_status(self):
            return None

    requests = []

    def fake_get(url, timeout):
        requests.append((url, timeout))
        return Response()

    monkeypatch.setattr(client_module.requests, "get", fake_get)
    manager = KalturaClientManager(configuration, _NullLogger())
    manager._pool = [KalturaSession(Client(), "ks")]
    manager._connected = True

    assert manager.get_metadata_xml("metadata-1") == Response.text
    assert requests == [("https://example.invalid/metadata.xml", configuration.download.timeout)]


def test_get_metadata_xml_accepts_direct_xml_response(tmp_path: Path, monkeypatch) -> None:
    from kaltura_backup import client as client_module

    configuration = _make_configuration(tmp_path)

    class MetadataService:
        def serve(self, _metadata_id):
            return "<metadata><Title>Example</Title></metadata>"

    class MetadataPlugin:
        metadata = MetadataService()

    class Client:
        metadata = MetadataPlugin()

    def unexpected_get(*args, **kwargs):
        raise AssertionError("Direct XML should not trigger an HTTP fetch")

    monkeypatch.setattr(client_module.requests, "get", unexpected_get)
    manager = KalturaClientManager(configuration, _NullLogger())
    manager._pool = [KalturaSession(Client(), "ks")]
    manager._connected = True

    assert manager.get_metadata_xml("metadata-1") == "<metadata><Title>Example</Title></metadata>"


def test_list_metadata_objects_filters_entry_object_type(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    manager = KalturaClientManager(configuration, _NullLogger())

    class MetadataService:
        def list(self, filter_object, pager):
            self.filter_object = filter_object
            self.pager = pager
            return []

    service = MetadataService()
    client = SimpleNamespace(metadata=SimpleNamespace(metadata=service))
    manager._pool = [KalturaSession(client, "ks")]
    manager._connected = True

    assert manager.list_metadata_objects("entry-123", "4696") == []
    assert service.filter_object.objectIdEqual == "entry-123"
    assert service.filter_object.metadataProfileIdEqual == 4696
    assert service.filter_object.metadataObjectTypeEqual == "1"


def test_ks_is_logged_only_when_explicitly_enabled(tmp_path: Path, monkeypatch) -> None:
    from kaltura_backup import client as client_module

    configuration = _make_configuration(tmp_path)

    class Logger(_NullLogger):
        def __init__(self) -> None:
            super().__init__()
            self.file_messages = []

        def info_file_only(self, event, message):
            self.file_messages.append((event, message))

    class FakeConfiguration:
        def __init__(self, partner_id):
            self.partner_id = partner_id

        def setLogger(self, logger):
            self.logger = logger

    class FakeSdkClient:
        def __init__(self, _configuration):
            self.session = SimpleNamespace(start=lambda *args: "test-ks")
            self.doHttpRequest = lambda *args, **kwargs: b"<result />"
            self.parsePostResult = lambda payload: payload

        def setKs(self, _ks):
            return None

    monkeypatch.setattr(client_module, "KalturaConfiguration", FakeConfiguration)
    monkeypatch.setattr(client_module, "KalturaClient", FakeSdkClient)

    logger = Logger()
    manager = KalturaClientManager(configuration, logger)
    manager.set_log_ks_enabled(True)

    manager._create_session()

    assert logger.file_messages == [
        (client_module.EventId.SESSION_CREATED, "Kaltura KS for dry-run: test-ks")
    ]


def test_kaltura_sdk_logger_forwards_messages_to_debug_logger() -> None:
    logger = _NullLogger()
    adapter = _KalturaSdkLogger(logger)

    adapter.log("request retrying in 5 seconds")

    assert logger.debug_messages[0][1] == "Kaltura SDK: request retrying in 5 seconds"


def test_raw_xml_response_is_saved_with_request_name(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    manager = KalturaClientManager(configuration, _NullLogger())
    client = SimpleNamespace()

    manager._save_xml_response("baseEntry_list", b"<response><result /></response>")

    files = list(configuration.paths.xml_dir.glob("baseEntry_list_*.xml"))
    assert len(files) == 1
    assert files[0].read_bytes() == b"<response><result /></response>"


def test_xml_capture_wraps_sdk_http_boundary(tmp_path: Path) -> None:
    configuration = _make_configuration(tmp_path)
    manager = KalturaClientManager(configuration, _NullLogger())

    class Client:
        def doHttpRequest(self, url, params=None, files=None):
            return b"<response><result /></response>"

    client = Client()
    manager._install_xml_capture(client)

    assert client.doHttpRequest("https://example.invalid/api_v3/service/baseEntry/action/list")
    files = list(configuration.paths.xml_dir.glob("baseEntry_list_*.xml"))
    assert len(files) == 1
