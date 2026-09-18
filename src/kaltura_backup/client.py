"""
Kaltura client abstraction.

This module encapsulates all interaction with the Kaltura Python SDK.

Responsibilities
----------------
- Session management
- Session pooling
- Automatic KS renewal
- Retry handling
- Translation of SDK exceptions
- Download helper methods

The remainder of the application never communicates directly with the
Kaltura SDK.
"""

from __future__ import annotations

import time
import sys
import traceback
import os
import re
from datetime import UTC, datetime

#from collections import deque
from functools import wraps
from threading import Condition
from typing import Any
from typing import Callable
from typing import TypeVar

KALTURA_SDK_AVAILABLE = True

try:
    from KalturaClient import KalturaClient  # type: ignore[import-not-found]
    from KalturaClient import KalturaConfiguration  # type: ignore[import-not-found]
    from KalturaClient.Base import IKalturaLogger  # type: ignore[import-not-found]
    try:
        from KalturaClient import KalturaSessionType  # type: ignore[import-not-found]
    except ImportError:
        try:
            from KalturaClient.Plugins.Core import KalturaSessionType  # type: ignore[import-not-found]
        except ImportError:
            class KalturaSessionType:
                ADMIN = "ADMIN"

    try:
        from KalturaClient.Plugins.Core import KalturaFilterPager, KalturaBaseEntryFilter, KalturaThumbAssetFilter  # type: ignore[import-not-found]
    except ImportError:
        KalturaFilterPager = None  # type: ignore[name-defined]
        KalturaBaseEntryFilter = None  # type: ignore[name-defined]
        KalturaThumbAssetFilter = None  # type: ignore[name-defined]

    try:
        from KalturaClient.Plugins.Caption import KalturaCaptionAssetFilter  # type: ignore[import-not-found]
    except ImportError:
        KalturaCaptionAssetFilter = None  # type: ignore[name-defined]

    try:
        from KalturaClient.Plugins.Metadata import KalturaMetadataFilter  # type: ignore[import-not-found]
    except ImportError:
        KalturaMetadataFilter = None  # type: ignore[name-defined]

    try:
        from KalturaClient.Plugins.Attachment import KalturaAttachmentAssetFilter  # type: ignore[import-not-found]
    except ImportError:
        KalturaAttachmentAssetFilter = None  # type: ignore[name-defined]
except ImportError as exc:  # pragma: no cover - exercised when SDK is absent or missing SDK dependencies
    missing_module = getattr(exc, "name", None)
    if missing_module is not None and missing_module.lower() != "kalturaclient":
        raise RuntimeError(
            "Kaltura SDK import failed because a dependency is missing. "
            "Install the SDK and its dependencies (for example: lxml, requests), "
            "then retry."
        ) from exc

    KALTURA_SDK_AVAILABLE = False
    IKalturaLogger = object  # type: ignore[misc,assignment]
    try:
        tb = traceback.format_exc()
        info = (
            f"Kaltura SDK import failed in interpreter: {sys.executable}\n"
            f"CWD: {os.getcwd()}\n"
            f"sys.path: {sys.path}\n"
            f"Traceback:\n{tb}\n"
        )
        log_path = os.path.join(os.getcwd(), "kaltura_import_error.log")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(info)

        print(
            "Kaltura SDK import failed. See kaltura_import_error.log for details.",
            file=sys.stderr,
        )
    except Exception:
        pass

    class KalturaConfiguration:
        def __init__(self, partner_id: int) -> None:
            self.partner_id = partner_id
            self.serviceUrl = ""

    class KalturaSessionType:
        ADMIN = "ADMIN"

    class KalturaClient:
        def __init__(self, configuration: KalturaConfiguration) -> None:
            self.configuration = configuration
            self.session = type(
                "SessionProxy",
                (),
                {"start": lambda self, *_args, **_kwargs: ""},
            )()

        def setKs(self, ks: str) -> None:
            self.ks = ks

from .config import Configuration
from .exceptions import (
    ApiError,
    AuthenticationError,
    ClientError,
    NetworkError,
    RetryExceededError,
    RetryableError,
    SessionExpiredError,
    TimeoutError,
)

from .logging_utils import (
    EventId,
    BackupLogger,
)

MAX_SESSIONS = 4

RETRY_COUNT = 3

RETRY_DELAY = 5

T = TypeVar("T")


class _KalturaSdkLogger(IKalturaLogger):
    """Adapt Kaltura SDK string logging to the application DEBUG logger."""

    def __init__(self, logger: BackupLogger) -> None:
        self._logger = logger
        self._xml_response_handler: Callable[[str, Any], None] | None = None

    def log(self, message: str) -> None:
        self._logger.debug(EventId.API_ERROR, f"Kaltura SDK: {message}")


# ============================================================================
# Session object
# ============================================================================


class KalturaSession:
    """
    Wrapper around a KalturaClient instance and its KS.
    """
   
    def __init__(
        self,
        client: KalturaClient,
        ks: str,
    ) -> None:

        self.client = client

        self.ks = ks

        self.created = time.time()

        self.last_used = self.created
        self.in_use = False
        self.use_count = 0
        self.last_error = ""

    def touch(self) -> None:
        """
        Update last-used timestamp.
        """

        self.last_used = time.time()
        self.use_count += 1

# ============================================================================
# Retry decorator
# ============================================================================


def retryable(
    func: Callable[..., T],
) -> Callable[..., T]:
    """
    Retry Kaltura operations on retryable failures.
    """

    @wraps(func)
    def wrapper(
        self,
        *args,
        **kwargs,
    ) -> T:

        last_exception: Exception | None = None

        for attempt in range(
            1,
            RETRY_COUNT + 1,
        ):

            try:

                return func(
                    self,
                    *args,
                    **kwargs,
                )

            except RetryableError as exc:

                last_exception = exc

                self._logger.retry(
                    attempt=attempt,
                    maximum=RETRY_COUNT,
                    delay=RETRY_DELAY,
                    message=str(exc),
                )

                if attempt == RETRY_COUNT:
                    break

                time.sleep(
                    RETRY_DELAY,
                )

        raise RetryExceededError(
            str(last_exception)
        ) from last_exception

    return wrapper


# ============================================================================
# Client manager
# ============================================================================


class KalturaClientManager:
    """
    Thread-safe Kaltura session pool.

    A maximum of four authenticated sessions are created and reused
    by the worker threads.
    """

    def __init__(
        self,
        configuration: Configuration,
        logger: BackupLogger,
    ) -> None:

        self._configuration = configuration

        self._logger = logger

        self._pool: list[KalturaSession] = [] # self._pool: deque[KalturaSession] = deque()

        self._condition = Condition()

        self._connected = False

    def set_xml_response_handler(self, handler: Callable[[str, Any], None] | None) -> None:
        """Register an optional handler for raw API XML responses."""
        self._xml_response_handler = handler

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        Create the configured number of authenticated Kaltura sessions.
        """

        if self._connected:
            return

        self._logger.info(
            EventId.CONNECTING,
            "Creating Kaltura session pool.",
        )

        for _ in range(MAX_SESSIONS):

            session = self._create_session()

            self._pool.append(session)

        self._connected = True

        self._logger.info(
            EventId.CONNECTED,
            f"Created {len(self._pool)} authenticated sessions.",
        )

    # ------------------------------------------------------------------

    def disconnect(self) -> None:
        """
        Dispose all pooled sessions.
        """

        with self._condition:

            self._pool.clear()

            self._connected = False

            self._condition.notify_all()

        self._logger.info(
            EventId.DISCONNECTED,
            "Kaltura session pool closed.",
        )

    # ------------------------------------------------------------------

    def _create_session(self) -> KalturaSession:
        """
        Create one authenticated Kaltura session.
        """

        if not KALTURA_SDK_AVAILABLE:
            raise ClientError(
                "Kaltura SDK not available in this Python interpreter: "
                f"{sys.executable}. Activate the virtual environment you installed the SDK into "
                "or install the SDK and its dependencies (for example: lxml, requests) and retry. "
                "Example: python -m pip install lxml requests git+https://github.com/kaltura/KalturaGeneratedAPIClientsPython.git"
            )

        try:

            cfg = KalturaConfiguration(
                self._configuration.connection.partner_id
            )

            cfg.serviceUrl = (
                self._configuration.connection.service_url
            )
            cfg.requestTimeout = self._configuration.download.timeout
            cfg.setLogger(_KalturaSdkLogger(self._logger))

            client = KalturaClient(cfg)
            self._install_xml_capture(client)

            # Add disableentitlement to privileges
            privileges = self._configuration.connection.privileges or ""
            privilege_list = [p.strip() for p in privileges.split(",") if p.strip()]
            if "disableentitlement" not in privilege_list:
                privilege_list.append("disableentitlement")
            privileges_str = ",".join(privilege_list)

            ks = client.session.start(
                self._configuration.connection.admin_secret,
                None,
                KalturaSessionType.ADMIN,
                self._configuration.connection.partner_id,
                self._configuration.connection.expiry,
                privileges_str,
            )

            client.setKs(ks)

            return KalturaSession(
                client,
                ks,
            )

        except Exception as exc:

            raise AuthenticationError(
                str(exc)
            ) from exc

    def _install_xml_capture(self, client: KalturaClient) -> None:
        """Capture raw XML responses directly from the SDK HTTP boundary."""

        original_http_request = client.doHttpRequest

        def do_http_request(*args, **kwargs):
            payload = original_http_request(*args, **kwargs)
            url = str(args[0] if args else kwargs.get("url", ""))
            match = re.search(r"/service/([^/]+)/action/([^/?]+)", url)
            request_name = f"{match.group(1)}_{match.group(2)}" if match else "kaltura_request"
            self._save_xml_response(request_name, payload)
            return payload

        client.doHttpRequest = do_http_request

    def _save_xml_response(self, request_name: str, payload: Any) -> None:
        try:
            xml_dir = self._configuration.paths.xml_dir
            xml_dir.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request_name).strip("._") or "kaltura_request"
            timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
            path = xml_dir / f"{safe_name}_{timestamp}.xml"
            if isinstance(payload, bytes):
                path.write_bytes(payload)
            else:
                path.write_text(str(payload), encoding="utf-8")
            self._logger.debug(EventId.API_ERROR, f"Saved raw XML response to {path}")
            if self._xml_response_handler is not None:
                self._xml_response_handler(request_name, payload)
        except Exception as exc:
            self._logger.warning(EventId.WARNING, f"Could not save raw XML response: {exc}")

    # ------------------------------------------------------------------

    def acquire(self) -> KalturaSession:
        """
        Acquire an available session from the pool.

        Blocks until a session becomes available.
        """

        with self._condition:

            while True:

                for session in self._pool:

                    if not session.in_use:

                        session.in_use = True

                        session.touch()

                        self._logger.debug(
                            EventId.SESSION_ACQUIRED,
                            (
                                "Session acquired "
                                f"(uses={session.use_count})"
                            ),
                        )

                        return session

                self._condition.wait()

    # ------------------------------------------------------------------

    def release(
        self,
        session: KalturaSession,
    ) -> None:
        """
        Return a session to the pool.
        """

        with self._condition:

            session.in_use = False

            session.touch()

            self._condition.notify()

        self._logger.debug(
            EventId.SESSION_RELEASED,
            "Session released.",
        )

    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """
        Return True if the client manager is connected.
        """

        return self._connected

    # ------------------------------------------------------------------

    @property
    def active_sessions(self) -> int:
        """
        Number of sessions currently leased.
        """

        return sum(
            1
            for session in self._pool
            if session.in_use
        )

    # ------------------------------------------------------------------

    @property
    def available_sessions(self) -> int:
        """
        Number of available sessions.
        """

        return (
            len(self._pool)
            - self.active_sessions
        )
    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _renew_session(
        self,
        session: KalturaSession,
    ) -> None:
        """
        Renew an expired Kaltura session.
        """

        self._logger.info(
            EventId.SESSION_RENEW,
            "Renewing Kaltura session.",
        )

        new_session = self._create_session()

        session.client = new_session.client
        session.ks = new_session.ks
        session.created = new_session.created
        session.last_used = new_session.last_used
        session.last_error = ""

    # ------------------------------------------------------------------

    class _SessionContext:
        """
        Context manager for pooled sessions.
        """

        def __init__(
            self,
            manager: "KalturaClientManager",
        ) -> None:

            self._manager = manager
            self._session: KalturaSession | None = None

        def __enter__(self) -> KalturaSession:

            self._session = self._manager.acquire()
            return self._session

        def __exit__(
            self,
            exc_type,
            exc,
            tb,
        ) -> bool:

            if self._session is not None:
                self._manager.release(self._session)

            return False

    # ------------------------------------------------------------------

    def session(self) -> "_SessionContext":
        """
        Return a context manager that automatically acquires and
        releases a pooled session.
        """

        return self._SessionContext(self)

    # ------------------------------------------------------------------
    # Entry operations
    # ------------------------------------------------------------------

    def _entry_service(self, client: KalturaClient) -> Any:
        """
        Return the entry service for the current Kaltura client.

        Some SDK versions expose the service as `baseEntry`; others may use
        alternate service names.
        """

        for service_name in (
            "baseEntry",
            "entry",
            "entryService",
            "entry_service",
        ):
            service = getattr(client, service_name, None)
            if service is not None:
                return service

        # Provide extra diagnostic context to help identify SDK mismatches at runtime
        available_attrs = [a for a in dir(client) if not a.startswith("_")]
        sample = ", ".join(available_attrs[:50])
        raise ClientError(
            "Configured Kaltura client does not expose an entry service. "
            "Verify the installed Kaltura SDK and its dependencies. "
            f"Available client attributes: {sample}"
        )

    @staticmethod
    def _media_entry_filter(filter_object: Any = None) -> Any:
        """Restrict default entry discovery to media entries (type 1)."""
        if filter_object is not None or KalturaBaseEntryFilter is None:
            return filter_object
        entry_filter = KalturaBaseEntryFilter()
        if hasattr(entry_filter, "setTypeIn"):
            entry_filter.setTypeIn("1")
        else:
            entry_filter.typeIn = "1"
        return entry_filter

    # ------------------------------------------------------------------

    def _thumb_asset_service(self, client: KalturaClient) -> Any:
        service = getattr(client, "thumbAsset", None)
        if service is not None:
            return service

        raise ClientError(
            "Configured Kaltura client does not expose a thumbAsset service. "
            "Verify the installed Kaltura SDK and its dependencies."
        )

    # ------------------------------------------------------------------

    def _caption_asset_service(self, client: KalturaClient) -> Any:
        caption = getattr(client, "caption", None)
        if caption is not None:
            service = getattr(caption, "captionAsset", None)
            if service is not None:
                return service

        raise ClientError(
            "Configured Kaltura client does not expose a captionAsset service. "
            "Verify the installed Kaltura SDK and its dependencies."
        )

    # ------------------------------------------------------------------

    def _attachment_asset_service(self, client: KalturaClient) -> Any:
        attachment = getattr(client, "attachment", None)
        if attachment is not None:
            service = getattr(attachment, "attachmentAsset", None)
            if service is not None:
                return service

        raise ClientError(
            "Configured Kaltura client does not expose an attachmentAsset service. "
            "Verify the installed Kaltura SDK and its dependencies."
        )

    # ------------------------------------------------------------------

    def _metadata_service(self, client: KalturaClient) -> Any:
        metadata = getattr(client, "metadata", None)
        if metadata is not None:
            service = getattr(metadata, "metadata", None)
            if service is not None:
                return service

        raise ClientError(
            "Configured Kaltura client does not expose a metadata service. "
            "Verify the installed Kaltura SDK and its dependencies."
        )

    # ------------------------------------------------------------------

    def _initialize_pager(
        self,
        pager: Any | None = None,
        page_size: int = 500,
        page_index: int = 1,
    ) -> Any | None:
        if pager is not None:
            return pager

        if KalturaFilterPager is None:
            return None

        result = KalturaFilterPager()

        if hasattr(result, "setPageSize"):
            result.setPageSize(page_size)
        elif hasattr(result, "pageSize"):
            result.pageSize = page_size

        if hasattr(result, "setPageIndex"):
            result.setPageIndex(page_index)
        elif hasattr(result, "pageIndex"):
            result.pageIndex = page_index

        return result

    # ------------------------------------------------------------------

    def _increment_pager(self, pager: Any) -> None:
        if hasattr(pager, "setPageIndex") and hasattr(pager, "getPageIndex"):
            pager.setPageIndex(pager.getPageIndex() + 1)
        elif hasattr(pager, "pageIndex"):
            pager.pageIndex += 1
        else:
            raise ClientError("Unable to advance Kaltura pager; unsupported pager type.")

    # ------------------------------------------------------------------

    def _objects_from_response(self, response: Any) -> list[Any]:
        if response is None:
            return []

        if hasattr(response, "getObjects"):
            return list(response.getObjects() or [])

        if hasattr(response, "objects"):
            return list(response.objects or [])

        try:
            return list(response)
        except Exception:
            return [response]

    # ------------------------------------------------------------------

    def _total_count_from_response(self, response: Any) -> int | None:
        if response is None:
            return None

        if hasattr(response, "getTotalCount"):
            return response.getTotalCount()

        if hasattr(response, "totalCount"):
            return response.totalCount

        return None

    # ------------------------------------------------------------------

    @retryable
    def get_entry(
        self,
        entry_id: str,
    ) -> Any:
        """
        Retrieve a single Kaltura entry.
        """

        with self.session() as session:

            try:
                return self._entry_service(session.client).get(
                    entry_id
                )

            except Exception as exc:

                self._translate_exception(exc)

    # ------------------------------------------------------------------

    def _make_filter(
        self,
        filter_cls: type[Any] | None,
        **kwargs: Any,
    ) -> Any:
        if filter_cls is None:
            raise ClientError(
                "The configured Kaltura SDK does not support the requested filter type. "
                "Verify the installed SDK package."
            )

        filter_object = filter_cls()
        for key, value in kwargs.items():
            if value is None:
                continue

            setter = f"set{key[0].upper()}{key[1:]}"
            if hasattr(filter_object, setter):
                getattr(filter_object, setter)(value)
            elif hasattr(filter_object, key):
                setattr(filter_object, key, value)

        return filter_object

    # ------------------------------------------------------------------

    @retryable
    def get_entry_media_url(
        self,
        entry_id: str,
    ) -> str:
        """
        Build the media download URL using the playManifest API.
        
        The URL format is:
        {base}{pid}/sp/{pid}00/playManifest/entryId/{entry_id}/format/download/protocol/https/flavorParamIds/0/ks/{ks}
        """
        with self.session() as session:
            pid = self._configuration.connection.partner_id
            base = self._configuration.connection.play_manifest_url
            ks = session.ks
            
            # Construct the download URL
            url = (
                f"{base}{pid}/sp/{pid}00/playManifest/entryId/{entry_id}/"
                f"format/download/protocol/https/flavorParamIds/0/ks/{ks}"
            )
            
            return url

    # ------------------------------------------------------------------

    @retryable
    def list_metadata_objects(
        self,
        entry_id: str,
        metadata_profile_id: str | int | None = None,
        page_size: int = 500,
    ) -> list[Any]:
        filter_object = self._make_filter(
            KalturaMetadataFilter,
            objectIdEqual=entry_id,
            metadataProfileIdEqual=int(metadata_profile_id)
            if metadata_profile_id is not None
            else None,
        )

        # Do not set metadataObjectTypeEqual to a raw int - the SDK expects
        # an enum-like object with a `getValue()` method. Leave the filter
        # as-is and let callers specify the correct typed value when needed.

        entries: list[Any] = []
        pager = self._initialize_pager(page_size=page_size, page_index=1)

        while True:
            with self.session() as session:
                try:
                    response = self._metadata_service(session.client).list(
                        filter_object,
                        pager,
                    )
                except Exception as exc:
                    self._translate_exception(exc)

            batch = self._objects_from_response(response)
            if not batch:
                break

            entries.extend(batch)

            total_count = self._total_count_from_response(response)
            if total_count is not None and len(entries) >= total_count:
                break

            if len(batch) < page_size:
                break

            self._increment_pager(pager)

        return entries

    # ------------------------------------------------------------------

    @retryable
    def get_metadata_xml(
        self,
        metadata_id: str,
    ) -> str:
        with self.session() as session:
            try:
                return self._metadata_service(session.client).serve(
                    metadata_id
                )
            except Exception as exc:
                self._translate_exception(exc)

    # ------------------------------------------------------------------

    @retryable
    def list_thumb_assets(
        self,
        entry_id: str,
        page_size: int = 500,
    ) -> list[Any]:
        filter_object = self._make_filter(
            KalturaThumbAssetFilter,
            entryIdEqual=entry_id,
        )

        assets: list[Any] = []
        pager = self._initialize_pager(page_size=page_size, page_index=1)

        while True:
            with self.session() as session:
                try:
                    response = self._thumb_asset_service(session.client).list(
                        filter_object,
                        pager,
                    )
                except Exception as exc:
                    self._translate_exception(exc)

            batch = self._objects_from_response(response)
            if not batch:
                break

            assets.extend(batch)
            total_count = self._total_count_from_response(response)
            if total_count is not None and len(assets) >= total_count:
                break

            if len(batch) < page_size:
                break

            self._increment_pager(pager)

        return assets

    # ------------------------------------------------------------------

    @retryable
    def get_thumb_url(
        self,
        thumb_asset_id: str,
        thumb_params_id: Any | None = None,
    ) -> str:
        with self.session() as session:
            try:
                return self._thumb_asset_service(session.client).getUrl(
                    thumb_asset_id,
                    None,
                    thumb_params_id,
                )
            except Exception as exc:
                self._translate_exception(exc)

    # ------------------------------------------------------------------

    @retryable
    def list_caption_assets(
        self,
        entry_id: str,
        page_size: int = 500,
    ) -> list[Any]:
        filter_object = self._make_filter(
            KalturaCaptionAssetFilter,
            entryIdEqual=entry_id,
        )

        assets: list[Any] = []
        pager = self._initialize_pager(page_size=page_size, page_index=1)

        while True:
            with self.session() as session:
                try:
                    response = self._caption_asset_service(session.client).list(
                        filter_object,
                        pager,
                    )
                except Exception as exc:
                    self._translate_exception(exc)

            batch = self._objects_from_response(response)
            if not batch:
                break

            assets.extend(batch)
            total_count = self._total_count_from_response(response)
            if total_count is not None and len(assets) >= total_count:
                break

            if len(batch) < page_size:
                break

            self._increment_pager(pager)

        return assets

    # ------------------------------------------------------------------

    @retryable
    def get_caption_json(
        self,
        caption_asset_id: str,
    ) -> str:
        with self.session() as session:
            try:
                return self._caption_asset_service(session.client).serveAsJson(
                    caption_asset_id,
                )
            except Exception as exc:
                self._translate_exception(exc)

    # ------------------------------------------------------------------

    @retryable
    def list_attachment_assets(
        self,
        entry_id: str,
        page_size: int = 500,
    ) -> list[Any]:
        filter_object = self._make_filter(
            KalturaAttachmentAssetFilter,
            entryIdEqual=entry_id,
        )

        assets: list[Any] = []
        pager = self._initialize_pager(page_size=page_size, page_index=1)

        while True:
            with self.session() as session:
                try:
                    response = self._attachment_asset_service(session.client).list(
                        filter_object,
                        pager,
                    )
                except Exception as exc:
                    self._translate_exception(exc)

            batch = self._objects_from_response(response)
            if not batch:
                break

            assets.extend(batch)
            total_count = self._total_count_from_response(response)
            if total_count is not None and len(assets) >= total_count:
                break

            if len(batch) < page_size:
                break

            self._increment_pager(pager)

        return assets

    # ------------------------------------------------------------------

    @retryable
    def get_attachment_url(
        self,
        attachment_asset_id: str,
    ) -> str:
        with self.session() as session:
            try:
                return self._attachment_asset_service(session.client).getUrl(
                    attachment_asset_id,
                )
            except Exception as exc:
                self._translate_exception(exc)

    # ------------------------------------------------------------------

    @retryable
    def list_entries(
        self,
        filter_object: Any = None,
        pager: Any = None,
    ) -> Any:
        """
        Retrieve a page of entries.
        """

        filter_object = self._media_entry_filter(filter_object)
        with self.session() as session:

            try:
                response = self._entry_service(session.client).list(
                    filter_object,
                    pager,
                )

                return self._objects_from_response(response)

            except Exception as exc:

                self._translate_exception(exc)

    # ------------------------------------------------------------------

    @retryable
    def list_all_entries(
        self,
        filter_object: Any = None,
        page_size: int = 500,
    ) -> list[Any]:
        """
        Retrieve all entries using Kaltura paging.
        """

        filter_object = self._media_entry_filter(filter_object)
        entries: list[Any] = []
        pager = self._initialize_pager(page_size=page_size, page_index=1)
        page = 1

        while True:
            self._logger.info(
                EventId.ENTRY_DISCOVERED,
                f"Requesting Kaltura entry page {page} (page size {page_size}, timeout {self._configuration.download.timeout}s).",
            )
            with self.session() as session:
                try:
                    response = self._entry_service(session.client).list(
                        filter_object,
                        pager,
                    )
                except Exception as exc:
                    self._translate_exception(exc)

            batch = self._objects_from_response(response)
            self._logger.info(
                EventId.ENTRY_DISCOVERED,
                f"Kaltura entry page {page} returned {len(batch)} entries.",
            )
            if not batch:
                break

            entries.extend(batch)

            total_count = self._total_count_from_response(response)
            if total_count is not None and len(entries) >= total_count:
                break

            # If the Kaltura service returned fewer entries than page size,
            # the current page is the last page.
            if len(batch) < page_size:
                break

            self._increment_pager(pager)
            page += 1

        return entries

    # ------------------------------------------------------------------
    # Exception translation
    # ------------------------------------------------------------------

    def _translate_exception(
        self,
        exception: Exception,
    ) -> None:
        """
        Translate SDK exceptions into project exceptions.
        """

        message = str(exception).lower()

        self._logger.exception(
            EventId.API_ERROR,
            "Kaltura API exception",
            exception,
        )

        if "invalid_ks" in message:
            raise SessionExpiredError(
                str(exception)
            ) from exception

        if "timeout" in message:
            raise TimeoutError(
                str(exception)
            ) from exception

        if (
            "connection" in message
            or "network" in message
        ):
            raise NetworkError(
                str(exception)
            ) from exception

        if isinstance(exception, ClientError):
            raise exception

        if (
            "temporarily"
            in message
            or "try again"
            in message
        ):
            raise RetryableError(
                str(exception)
            ) from exception

        raise ApiError(
            str(exception)
        ) from exception
