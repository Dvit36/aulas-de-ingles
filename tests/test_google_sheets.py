from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import pytest

from english_leaderboard.google_sheets import (
    GoogleSheetsApiGateway,
    GoogleSheetsConfigurationError,
    sync_leaderboard,
    sync_leaderboard_and_ledger,
)


class FakeSheetsGateway:
    def __init__(self) -> None:
        self.tabs: dict[str, int] = {}
        self.values: dict[str, list[list[Any]]] = {}
        self.create_calls: list[list[str]] = []
        self.replace_calls: list[dict[str, list[list[Any]]]] = []
        self._next_sheet_id = 100

    def list_tabs(self, spreadsheet_id: str) -> dict[str, int]:
        assert spreadsheet_id
        return dict(self.tabs)

    def create_tabs(
        self,
        spreadsheet_id: str,
        titles: list[str],
    ) -> dict[str, int]:
        assert spreadsheet_id
        self.create_calls.append(list(titles))
        created: dict[str, int] = {}
        for title in titles:
            self.tabs[title] = self._next_sheet_id
            self._next_sheet_id += 1
            self.values[title] = []
            created[title] = self.tabs[title]
        return created

    def read_tab_values(
        self,
        spreadsheet_id: str,
        titles: list[str],
    ) -> dict[str, list[list[Any]]]:
        assert spreadsheet_id
        return {title: deepcopy(self.values.get(title, [])) for title in titles}

    def replace_tabs(
        self,
        spreadsheet_id: str,
        values_by_title: dict[str, list[list[Any]]],
        sheet_ids: dict[str, int],
    ) -> None:
        assert spreadsheet_id
        assert all(title in sheet_ids for title in values_by_title)
        copied = deepcopy(values_by_title)
        self.replace_calls.append(copied)
        self.values.update(copied)


def _leaderboard(points: int = 20) -> list[dict[str, Any]]:
    return [
        {
            "position": 1,
            "student_id": "student-1",
            "student": "Ana",
            "email": "ana@example.test",
            "points": points,
        }
    ]


def _ledger() -> list[dict[str, Any]]:
    return [
        {
            "occurred_at": datetime(2026, 8, 14, 12, tzinfo=timezone.utc),
            "student": "Ana",
            "transaction_type": "imported_daily_score",
            "points": 20,
            "source_key": "legacy|Página1|ana|2026-08-14",
            "description": "=untrusted-text-stays-raw",
        }
    ]


def test_sync_creates_tabs_then_is_a_noop_when_values_match() -> None:
    gateway = FakeSheetsGateway()

    first = sync_leaderboard_and_ledger(
        "spreadsheet-1",
        _leaderboard(),
        _ledger(),
        gateway=gateway,
    )

    assert first.created == 2
    assert first.updated == 0
    assert gateway.create_calls == [["Leaderboard", "Ledger"]]
    assert len(gateway.replace_calls) == 1
    assert gateway.values["Leaderboard"][0] == [
        "position",
        "student",
        "points",
    ]
    assert gateway.values["Ledger"][1][0] == "2026-08-14T12:00:00Z"
    assert gateway.values["Ledger"][1][-1] == "=untrusted-text-stays-raw"

    second = sync_leaderboard_and_ledger(
        "spreadsheet-1",
        _leaderboard(),
        _ledger(),
        gateway=gateway,
    )

    assert second.unchanged == 2
    assert second.changed is False
    assert len(gateway.create_calls) == 1
    assert len(gateway.replace_calls) == 1


def test_sync_updates_only_the_changed_tab() -> None:
    gateway = FakeSheetsGateway()
    sync_leaderboard_and_ledger(
        "spreadsheet-1",
        _leaderboard(),
        _ledger(),
        gateway=gateway,
    )

    result = sync_leaderboard_and_ledger(
        "spreadsheet-1",
        _leaderboard(points=25),
        _ledger(),
        gateway=gateway,
    )

    assert result.updated == 1
    assert result.unchanged == 1
    assert set(gateway.replace_calls[-1]) == {"Leaderboard"}
    assert gateway.values["Leaderboard"][1][-1] == 25


def test_comparison_ignores_trailing_empty_cells_and_rows() -> None:
    gateway = FakeSheetsGateway()
    gateway.tabs["Leaderboard"] = 7
    gateway.values["Leaderboard"] = [
        ["position", "student", "points", ""],
        [1, "Ana", 20, ""],
        [],
    ]

    result = sync_leaderboard(
        "spreadsheet-1",
        _leaderboard(),
        gateway=gateway,
    )

    assert result.unchanged == 1
    assert gateway.replace_calls == []


def test_tabs_must_be_distinct_and_valid() -> None:
    gateway = FakeSheetsGateway()
    with pytest.raises(GoogleSheetsConfigurationError):
        sync_leaderboard_and_ledger(
            "spreadsheet-1",
            [],
            [],
            leaderboard_tab="Data",
            ledger_tab="Data",
            gateway=gateway,
        )

    with pytest.raises(GoogleSheetsConfigurationError):
        sync_leaderboard(
            "spreadsheet-1",
            [],
            tab_name="Bad/Tab",
            gateway=gateway,
        )


def test_default_credentials_gateway_is_used_when_no_key_is_passed(monkeypatch) -> None:
    gateway = FakeSheetsGateway()
    monkeypatch.setattr(
        GoogleSheetsApiGateway,
        "from_default_credentials",
        classmethod(lambda cls: gateway),
    )

    result = sync_leaderboard("spreadsheet-1", _leaderboard())

    assert result.created == 1
    assert gateway.values["Leaderboard"][1] == [1, "Ana", 20]
