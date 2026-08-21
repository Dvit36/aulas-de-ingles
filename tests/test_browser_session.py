from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from english_leaderboard import browser_session
from english_leaderboard.browser_session import (
    COMMAND_KEY,
    COOKIE_NAME,
    forget_token,
    mount_browser_session,
    queue_token_write,
    request_token,
    valid_session_token,
)

TOKEN = "A" * 64


def _future_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def _payload(
    *,
    token: str | None = TOKEN,
    expires_at: datetime | None = None,
    ack_id: str | None = None,
    storage_available: bool = True,
) -> dict[str, object]:
    expiry = expires_at or _future_expiry()
    return {
        "ready": True,
        "storage_available": storage_available,
        "token": token,
        "expires_at": expiry.isoformat() if token else None,
        "ack_id": ack_id,
    }


def test_request_token_accepts_only_bounded_opaque_values() -> None:
    browser = SimpleNamespace(
        session_state={},
        context=SimpleNamespace(cookies={COOKIE_NAME: TOKEN}),
    )
    assert request_token(browser) == TOKEN
    browser.session_state["local_auth_token"] = "short"
    assert request_token(browser) == TOKEN
    browser.context.cookies[COOKIE_NAME] = "x" * 10_000
    assert request_token(browser) is None
    assert valid_session_token("_" * 64) == "_" * 64
    assert valid_session_token("!" * 64) is None


def test_queue_write_keeps_command_pending_until_matching_ack() -> None:
    state: dict[str, object] = {}
    streamlit = SimpleNamespace(session_state=state)
    expires_at = _future_expiry()
    queue_token_write(streamlit, TOKEN, expires_at)
    command = dict(state[COMMAND_KEY])
    calls: list[dict[str, object]] = []

    def renderer(**kwargs):
        calls.append(kwargs)
        return {"payload": _payload(ack_id="wrong-command")}

    snapshot = mount_browser_session(streamlit, renderer=renderer)

    assert snapshot.token == TOKEN
    assert state[COMMAND_KEY] == command
    assert calls[0]["data"]["command"] == command


def test_matching_write_ack_consumes_command_idempotently() -> None:
    state: dict[str, object] = {}
    streamlit = SimpleNamespace(session_state=state)
    queue_token_write(streamlit, TOKEN, _future_expiry())
    command_id = state[COMMAND_KEY]["id"]

    snapshot = mount_browser_session(
        streamlit,
        renderer=lambda **_kwargs: {
            "payload": _payload(ack_id=str(command_id)),
        },
    )

    assert snapshot.ready is True
    assert snapshot.token == TOKEN
    assert COMMAND_KEY not in state


def test_matching_ack_cannot_confirm_a_different_token() -> None:
    state: dict[str, object] = {}
    streamlit = SimpleNamespace(session_state=state)
    queue_token_write(streamlit, TOKEN, _future_expiry())
    command_id = state[COMMAND_KEY]["id"]

    mount_browser_session(
        streamlit,
        renderer=lambda **_kwargs: {
            "payload": _payload(token="B" * 64, ack_id=str(command_id)),
        },
    )

    assert COMMAND_KEY in state


def test_clear_command_waits_for_ack_and_removes_local_token() -> None:
    state: dict[str, object] = {"local_auth_token": TOKEN}
    streamlit = SimpleNamespace(session_state=state)
    forget_token(streamlit)
    command_id = state[COMMAND_KEY]["id"]
    assert "local_auth_token" not in state

    mount_browser_session(
        streamlit,
        renderer=lambda **_kwargs: {
            "payload": _payload(token=None, ack_id=str(command_id)),
        },
    )

    assert COMMAND_KEY not in state


def test_untrusted_payload_is_rejected_and_expiry_uses_server_authority() -> None:
    streamlit = SimpleNamespace(session_state={})
    expired = datetime.now(UTC) - timedelta(seconds=1)

    invalid = mount_browser_session(
        streamlit,
        renderer=lambda **_kwargs: {
            "payload": _payload(token="x" * 10_000),
        },
    )
    expired_snapshot = mount_browser_session(
        streamlit,
        renderer=lambda **_kwargs: {
            "payload": _payload(expires_at=expired),
        },
    )

    assert invalid.token is None
    assert expired_snapshot.token == TOKEN


def test_overflowing_expiry_is_rejected_without_crashing() -> None:
    streamlit = SimpleNamespace(session_state={})

    snapshot = mount_browser_session(
        streamlit,
        renderer=lambda **_kwargs: {
            "payload": {
                "ready": True,
                "storage_available": True,
                "token": TOKEN,
                "expires_at": "9999-12-31T23:59:59-23:59",
                "ack_id": None,
            },
        },
    )

    assert snapshot.token is None


def test_storage_failure_is_reported_without_restoring_token() -> None:
    streamlit = SimpleNamespace(session_state={})
    queue_token_write(streamlit, TOKEN, _future_expiry())

    snapshot = mount_browser_session(
        streamlit,
        renderer=lambda **_kwargs: {
            "payload": _payload(storage_available=False),
        },
    )

    assert snapshot.ready is True
    assert snapshot.storage_available is False
    assert snapshot.token is None
    assert COMMAND_KEY not in streamlit.session_state


def test_component_uses_local_storage_without_exposing_token_in_markup() -> None:
    source = browser_session._BROWSER_SESSION_JS
    assert "window.localStorage.getItem" in source
    assert "window.localStorage.setItem" in source
    assert "setStateValue" in source
    assert "Date.now" not in source
    assert "innerHTML" not in source


def test_browser_session_component_mounts_with_real_streamlit() -> None:
    app = AppTest.from_string(
        """
import streamlit as st
from english_leaderboard.browser_session import mount_browser_session

snapshot = mount_browser_session(st)
st.write(f"ready={snapshot.ready}")
"""
    )
    app.run(timeout=10)
    assert not app.exception
    assert app.markdown[0].value == "ready=False"
