from __future__ import annotations

from dataclasses import replace

from sqlalchemy import func, select

import streamlit_app
from english_leaderboard.models import LedgerKind, LedgerTransaction, Role


def test_google_failure_does_not_rollback_committed_ledger(
    session,
    settings,
    users,
    monkeypatch,
) -> None:
    student = users[Role.STUDENT]
    session.add(
        LedgerTransaction(
            student_id=student.id,
            points=7,
            kind=LedgerKind.ADJUSTMENT,
            source_type="test",
            source_key="test:google-failure-after-commit",
        )
    )
    session.commit()

    enabled = replace(
        settings,
        google_sheets_auto_sync=True,
        google_sheets_spreadsheet_id="sheet-123",
    )
    monkeypatch.setattr(
        streamlit_app,
        "sync_leaderboard_and_ledger",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(streamlit_app.st, "warning", lambda *args, **kwargs: None)

    assert streamlit_app.sync_google_sheets_snapshot(session, enabled) is False
    assert session.scalar(select(func.count(LedgerTransaction.id))) == 1
