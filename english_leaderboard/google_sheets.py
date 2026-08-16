"""Optional, idempotent synchronization with Google Sheets.

This module has no import-time dependency on Google's SDK and performs no network
access until a sync function is called without an injected gateway.  Production
uses a service account; tests can provide any object implementing ``SheetsGateway``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
import json
import math
from pathlib import Path
import random
from threading import Lock
import time
from typing import Any, Literal, Protocol, runtime_checkable

GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

LEADERBOARD_HEADERS = ("position", "student", "points")
LEDGER_HEADERS = (
    "occurred_at",
    "student",
    "transaction_type",
    "points",
    "source_key",
    "description",
)

TabAction = Literal["created", "updated", "unchanged"]
_LOCKS_GUARD = Lock()
_SPREADSHEET_LOCKS: dict[str, Lock] = {}
GOOGLE_API_TIMEOUT_SECONDS = 15


class GoogleSheetsDependencyError(RuntimeError):
    """Raised when the optional Google client libraries are unavailable."""


class GoogleSheetsConfigurationError(ValueError):
    """Raised for missing credentials, spreadsheet ids, or invalid tab names."""


@dataclass(frozen=True, slots=True)
class TabSyncResult:
    title: str
    action: TabAction
    rows: int
    columns: int

    @property
    def changed(self) -> bool:
        return self.action != "unchanged"


@dataclass(frozen=True, slots=True)
class GoogleSheetsSyncResult:
    spreadsheet_id: str
    tabs: tuple[TabSyncResult, ...]

    @property
    def created(self) -> int:
        return sum(tab.action == "created" for tab in self.tabs)

    @property
    def updated(self) -> int:
        return sum(tab.action == "updated" for tab in self.tabs)

    @property
    def unchanged(self) -> int:
        return sum(tab.action == "unchanged" for tab in self.tabs)

    @property
    def changed(self) -> bool:
        return any(tab.changed for tab in self.tabs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "spreadsheet_id": self.spreadsheet_id,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "tabs": [asdict(tab) for tab in self.tabs],
        }


@runtime_checkable
class SheetsGateway(Protocol):
    """Small API boundary used by the synchronization algorithm."""

    def list_tabs(self, spreadsheet_id: str) -> Mapping[str, int]: ...

    def create_tabs(
        self,
        spreadsheet_id: str,
        titles: Sequence[str],
    ) -> Mapping[str, int]: ...

    def read_tab_values(
        self,
        spreadsheet_id: str,
        titles: Sequence[str],
    ) -> Mapping[str, Sequence[Sequence[Any]]]: ...

    def replace_tabs(
        self,
        spreadsheet_id: str,
        values_by_title: Mapping[str, Sequence[Sequence[Any]]],
        sheet_ids: Mapping[str, int],
    ) -> None: ...


def _execute(request: Any) -> Mapping[str, Any]:
    """Execute a Google request with a short retry for transient API failures."""

    for attempt in range(4):
        try:
            response = request.execute()
            return response if isinstance(response, Mapping) else {}
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
            time.sleep(0.5 * (2**attempt) + random.uniform(0, 0.25))
    return {}  # pragma: no cover - the loop always returns or raises


def _spreadsheet_lock(spreadsheet_id: str) -> Lock:
    with _LOCKS_GUARD:
        return _SPREADSHEET_LOCKS.setdefault(spreadsheet_id, Lock())


def _build_google_service(credentials: Any) -> Any:
    try:
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - depends on optional SDK
        raise GoogleSheetsDependencyError(
            "Install google-api-python-client, google-auth and "
            "google-auth-httplib2 to enable Google Sheets synchronization."
        ) from exc
    http = AuthorizedHttp(
        credentials,
        http=httplib2.Http(timeout=GOOGLE_API_TIMEOUT_SECONDS),
    )
    return build(
        "sheets",
        "v4",
        http=http,
        cache_discovery=False,
    )


def _quoted_tab(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _column_pixel_width(matrix: Sequence[Sequence[Any]], column_index: int) -> int:
    visible_lengths = []
    for row in matrix[:501]:
        value = row[column_index] if column_index < len(row) else ""
        visible_lengths.append(len(str(value)))
    max_length = max(visible_lengths, default=10)
    return min(320, max(90, max_length * 7 + 18))


class GoogleSheetsApiGateway:
    """Google Sheets API v4 adapter backed by a service account."""

    def __init__(self, service: Any) -> None:
        self._service = service

    @classmethod
    def from_service_account_file(
        cls,
        credentials_file: str | Path,
        *,
        scopes: Sequence[str] = (GOOGLE_SHEETS_SCOPE,),
    ) -> "GoogleSheetsApiGateway":
        try:
            from google.oauth2.service_account import Credentials
        except ImportError as exc:  # pragma: no cover - depends on optional SDK
            raise GoogleSheetsDependencyError(
                "Install google-api-python-client and google-auth to enable "
                "Google Sheets synchronization."
            ) from exc

        path = Path(credentials_file)
        if not path.is_file():
            raise GoogleSheetsConfigurationError(
                f"Service-account credentials file was not found: {path}"
            )
        credentials = Credentials.from_service_account_file(
            str(path),
            scopes=list(scopes),
        )
        return cls(_build_google_service(credentials))

    @classmethod
    def from_service_account_info(
        cls,
        credentials_info: Mapping[str, Any],
        *,
        scopes: Sequence[str] = (GOOGLE_SHEETS_SCOPE,),
    ) -> "GoogleSheetsApiGateway":
        try:
            from google.oauth2.service_account import Credentials
        except ImportError as exc:  # pragma: no cover - depends on optional SDK
            raise GoogleSheetsDependencyError(
                "Install google-api-python-client and google-auth to enable "
                "Google Sheets synchronization."
            ) from exc

        credentials = Credentials.from_service_account_info(
            dict(credentials_info),
            scopes=list(scopes),
        )
        return cls(_build_google_service(credentials))

    @classmethod
    def from_default_credentials(
        cls,
        *,
        scopes: Sequence[str] = (GOOGLE_SHEETS_SCOPE,),
    ) -> "GoogleSheetsApiGateway":
        """Use ADC, including GOOGLE_APPLICATION_CREDENTIALS when configured."""

        try:
            import google.auth
        except ImportError as exc:  # pragma: no cover - depends on optional SDK
            raise GoogleSheetsDependencyError(
                "Install google-api-python-client and google-auth to enable "
                "Google Sheets synchronization."
            ) from exc
        credentials, _ = google.auth.default(scopes=list(scopes))
        return cls(_build_google_service(credentials))

    def list_tabs(self, spreadsheet_id: str) -> Mapping[str, int]:
        response = _execute(
            self._service.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                includeGridData=False,
                fields="sheets.properties(sheetId,title)",
            )
        )
        tabs: dict[str, int] = {}
        for sheet in response.get("sheets", []):
            properties = sheet.get("properties", {})
            title = properties.get("title")
            sheet_id = properties.get("sheetId")
            if isinstance(title, str) and isinstance(sheet_id, int):
                tabs[title] = sheet_id
        return tabs

    def create_tabs(
        self,
        spreadsheet_id: str,
        titles: Sequence[str],
    ) -> Mapping[str, int]:
        if not titles:
            return {}
        response = _execute(
            self._service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "addSheet": {
                                "properties": {
                                    "title": title,
                                    "gridProperties": {
                                        "rowCount": 1000,
                                        "columnCount": 26,
                                        "frozenRowCount": 1,
                                    },
                                }
                            }
                        }
                        for title in titles
                    ]
                },
            )
        )
        created: dict[str, int] = {}
        for reply in response.get("replies", []):
            properties = reply.get("addSheet", {}).get("properties", {})
            title = properties.get("title")
            sheet_id = properties.get("sheetId")
            if isinstance(title, str) and isinstance(sheet_id, int):
                created[title] = sheet_id
        if len(created) != len(titles):
            # A defensive metadata refresh also handles sparse API replies.
            refreshed = self.list_tabs(spreadsheet_id)
            created.update(
                {
                    title: refreshed[title]
                    for title in titles
                    if title in refreshed
                }
            )
        missing = [title for title in titles if title not in created]
        if missing:
            raise RuntimeError(f"Google Sheets did not create tabs: {missing}")
        return created

    def read_tab_values(
        self,
        spreadsheet_id: str,
        titles: Sequence[str],
    ) -> Mapping[str, Sequence[Sequence[Any]]]:
        if not titles:
            return {}
        ranges = [f"{_quoted_tab(title)}!A:Z" for title in titles]
        response = _execute(
            self._service.spreadsheets()
            .values()
            .batchGet(
                spreadsheetId=spreadsheet_id,
                ranges=ranges,
                majorDimension="ROWS",
                valueRenderOption="UNFORMATTED_VALUE",
                dateTimeRenderOption="FORMATTED_STRING",
            )
        )
        value_ranges = response.get("valueRanges", [])
        return {
            title: value_ranges[index].get("values", [])
            if index < len(value_ranges)
            else []
            for index, title in enumerate(titles)
        }

    def replace_tabs(
        self,
        spreadsheet_id: str,
        values_by_title: Mapping[str, Sequence[Sequence[Any]]],
        sheet_ids: Mapping[str, int],
    ) -> None:
        if not values_by_title:
            return
        titles = list(values_by_title)
        sheets_resource = self._service.spreadsheets()
        values_resource = sheets_resource.values()
        # Write the new snapshot first. If a later cleanup request fails, the
        # current data remains visible and the next idempotent sync reconciles
        # only stale trailing cells.
        _execute(
            values_resource.batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "valueInputOption": "RAW",
                    "data": [
                        {
                            "range": f"{_quoted_tab(title)}!A1",
                            "majorDimension": "ROWS",
                            "values": [list(row) for row in values_by_title[title]],
                        }
                        for title in titles
                    ],
                },
            )
        )
        stale_ranges: list[str] = []
        for title, matrix in values_by_title.items():
            row_count = max(len(matrix), 1)
            column_count = max((len(row) for row in matrix), default=1)
            quoted = _quoted_tab(title)
            stale_ranges.append(f"{quoted}!A{row_count + 1}:Z")
            if column_count < 26:
                first_stale_column = chr(ord("A") + column_count)
                stale_ranges.append(
                    f"{quoted}!{first_stale_column}1:Z{row_count}"
                )
        _execute(
            values_resource.batchClear(
                spreadsheetId=spreadsheet_id,
                body={"ranges": stale_ranges},
            )
        )

        formatting_requests: list[dict[str, Any]] = []
        for title, matrix in values_by_title.items():
            sheet_id = sheet_ids[title]
            column_count = max((len(row) for row in matrix), default=1)
            row_count = max(len(matrix), 1)
            formatting_requests.extend(
                [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheet_id,
                                "gridProperties": {"frozenRowCount": 1},
                            },
                            "fields": "gridProperties.frozenRowCount",
                        }
                    },
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": 0,
                                "endIndex": 1,
                            },
                            "properties": {"pixelSize": 30},
                            "fields": "pixelSize",
                        }
                    },
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": column_count,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColorStyle": {
                                        "rgbColor": {
                                            "red": 31 / 255,
                                            "green": 78 / 255,
                                            "blue": 120 / 255,
                                        }
                                    },
                                    "textFormat": {
                                        "bold": True,
                                        "foregroundColorStyle": {
                                            "rgbColor": {
                                                "red": 1,
                                                "green": 1,
                                                "blue": 1,
                                            }
                                        },
                                    },
                                    "verticalAlignment": "MIDDLE",
                                }
                            },
                            "fields": (
                                "userEnteredFormat(backgroundColorStyle,textFormat,"
                                "verticalAlignment)"
                            ),
                        }
                    },
                    {
                        "setBasicFilter": {
                            "filter": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": 0,
                                    "endRowIndex": row_count,
                                    "startColumnIndex": 0,
                                    "endColumnIndex": column_count,
                                }
                            }
                        }
                    },
                ]
            )
            for column_index in range(column_count):
                formatting_requests.append(
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": column_index,
                                "endIndex": column_index + 1,
                            },
                            "properties": {
                                "pixelSize": _column_pixel_width(matrix, column_index)
                            },
                            "fields": "pixelSize",
                        }
                    }
                )

        _execute(
            sheets_resource.batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": formatting_requests},
            )
        )


def _row_to_mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    mapping = getattr(row, "_mapping", None)
    if isinstance(mapping, Mapping):
        return mapping
    if is_dataclass(row) and not isinstance(row, type):
        return asdict(row)
    table = getattr(row, "__table__", None)
    columns = getattr(table, "columns", None)
    if columns is not None:
        return {column.name: getattr(row, column.name) for column in columns}
    if hasattr(row, "__dict__"):
        return {
            key: value
            for key, value in vars(row).items()
            if not key.startswith("_")
        }
    raise TypeError(f"Unsupported Google Sheets row type: {type(row).__name__}")


def _sheet_value(value: Any) -> str | int | float | bool:
    if value is None:
        return ""
    if isinstance(value, Enum):
        return _sheet_value(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
            return value.isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return value
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def _records_matrix(
    rows: Iterable[Any],
    headers: Sequence[str],
) -> list[list[str | int | float | bool]]:
    matrix: list[list[str | int | float | bool]] = [list(headers)]
    for row in rows:
        mapping = _row_to_mapping(row)
        matrix.append([_sheet_value(mapping.get(header)) for header in headers])
    return matrix


def _normalise_grid(values: Sequence[Sequence[Any]] | None) -> list[list[Any]]:
    normalised: list[list[Any]] = []
    for source_row in values or []:
        row = ["" if value is None else value for value in source_row]
        while row and row[-1] == "":
            row.pop()
        normalised.append(row)
    while normalised and not normalised[-1]:
        normalised.pop()
    return normalised


def _validate_tab_title(title: str) -> None:
    if not title or not title.strip():
        raise GoogleSheetsConfigurationError("Google Sheets tab title cannot be empty")
    if len(title) > 100 or any(character in title for character in ":\\/?*[]"):
        raise GoogleSheetsConfigurationError(f"Invalid Google Sheets tab title: {title!r}")


def _build_gateway(
    *,
    gateway: SheetsGateway | None,
    service_account_file: str | Path | None,
    service_account_info: Mapping[str, Any] | None,
) -> SheetsGateway:
    if gateway is not None:
        return gateway
    if service_account_file is not None and service_account_info is not None:
        raise GoogleSheetsConfigurationError(
            "Provide service_account_file or service_account_info, not both"
        )
    if service_account_file is not None:
        return GoogleSheetsApiGateway.from_service_account_file(service_account_file)
    if service_account_info is not None:
        return GoogleSheetsApiGateway.from_service_account_info(service_account_info)
    return GoogleSheetsApiGateway.from_default_credentials()


def _sync_payloads(
    spreadsheet_id: str,
    payloads: Mapping[str, Sequence[Sequence[Any]]],
    *,
    gateway: SheetsGateway | None = None,
    service_account_file: str | Path | None = None,
    service_account_info: Mapping[str, Any] | None = None,
) -> GoogleSheetsSyncResult:
    if not spreadsheet_id or not spreadsheet_id.strip():
        raise GoogleSheetsConfigurationError("spreadsheet_id cannot be empty")
    if not payloads:
        return GoogleSheetsSyncResult(spreadsheet_id=spreadsheet_id, tabs=())
    for title in payloads:
        _validate_tab_title(title)

    client = _build_gateway(
        gateway=gateway,
        service_account_file=service_account_file,
        service_account_info=service_account_info,
    )
    with _spreadsheet_lock(spreadsheet_id):
        original_tabs = dict(client.list_tabs(spreadsheet_id))
        missing_titles = [title for title in payloads if title not in original_tabs]
        sheet_ids = dict(original_tabs)
        if missing_titles:
            sheet_ids.update(client.create_tabs(spreadsheet_id, missing_titles))

        unresolved = [title for title in payloads if title not in sheet_ids]
        if unresolved:
            raise RuntimeError(f"Could not resolve Google Sheets tabs: {unresolved}")

        current_values = client.read_tab_values(spreadsheet_id, list(payloads))
        changed_payloads = {
            title: values
            for title, values in payloads.items()
            if _normalise_grid(current_values.get(title)) != _normalise_grid(values)
        }
        if changed_payloads:
            client.replace_tabs(spreadsheet_id, changed_payloads, sheet_ids)

        tab_results = tuple(
            TabSyncResult(
                title=title,
                action=(
                    "created"
                    if title in missing_titles
                    else "updated"
                    if title in changed_payloads
                    else "unchanged"
                ),
                rows=max(len(values) - 1, 0),
                columns=max((len(row) for row in values), default=0),
            )
            for title, values in payloads.items()
        )
        return GoogleSheetsSyncResult(
            spreadsheet_id=spreadsheet_id,
            tabs=tab_results,
        )


def sync_leaderboard(
    spreadsheet_id: str,
    rows: Iterable[Any],
    *,
    tab_name: str = "Leaderboard",
    gateway: SheetsGateway | None = None,
    service_account_file: str | Path | None = None,
    service_account_info: Mapping[str, Any] | None = None,
) -> GoogleSheetsSyncResult:
    """Create or update the leaderboard tab and skip writes when unchanged."""

    return _sync_payloads(
        spreadsheet_id,
        {tab_name: _records_matrix(rows, LEADERBOARD_HEADERS)},
        gateway=gateway,
        service_account_file=service_account_file,
        service_account_info=service_account_info,
    )


def sync_ledger(
    spreadsheet_id: str,
    rows: Iterable[Any],
    *,
    tab_name: str = "Ledger",
    gateway: SheetsGateway | None = None,
    service_account_file: str | Path | None = None,
    service_account_info: Mapping[str, Any] | None = None,
) -> GoogleSheetsSyncResult:
    """Create or update the ledger tab and skip writes when unchanged."""

    return _sync_payloads(
        spreadsheet_id,
        {tab_name: _records_matrix(rows, LEDGER_HEADERS)},
        gateway=gateway,
        service_account_file=service_account_file,
        service_account_info=service_account_info,
    )


def sync_leaderboard_and_ledger(
    spreadsheet_id: str,
    leaderboard_rows: Iterable[Any],
    ledger_rows: Iterable[Any],
    *,
    leaderboard_tab: str = "Leaderboard",
    ledger_tab: str = "Ledger",
    gateway: SheetsGateway | None = None,
    service_account_file: str | Path | None = None,
    service_account_info: Mapping[str, Any] | None = None,
) -> GoogleSheetsSyncResult:
    """Synchronize both reporting views in one idempotent operation."""

    if leaderboard_tab == ledger_tab:
        raise GoogleSheetsConfigurationError(
            "leaderboard_tab and ledger_tab must be different"
        )
    return _sync_payloads(
        spreadsheet_id,
        {
            leaderboard_tab: _records_matrix(
                leaderboard_rows,
                LEADERBOARD_HEADERS,
            ),
            ledger_tab: _records_matrix(ledger_rows, LEDGER_HEADERS),
        },
        gateway=gateway,
        service_account_file=service_account_file,
        service_account_info=service_account_info,
    )


__all__ = [
    "GOOGLE_SHEETS_SCOPE",
    "GoogleSheetsApiGateway",
    "GoogleSheetsConfigurationError",
    "GoogleSheetsDependencyError",
    "GoogleSheetsSyncResult",
    "SheetsGateway",
    "TabSyncResult",
    "sync_leaderboard",
    "sync_leaderboard_and_ledger",
    "sync_ledger",
]
