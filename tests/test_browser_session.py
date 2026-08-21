from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

import streamlit_app
from english_leaderboard.browser_session import (
    COOKIE_NAME,
    forget_token,
    render_cookie_bridge,
    request_cookie,
)


def test_refresh_recovers_only_opaque_token_from_cookie() -> None:
    browser = SimpleNamespace(
        session_state={},
        context=SimpleNamespace(cookies={COOKIE_NAME: "opaque-random-token"}),
    )
    assert request_cookie(browser) == "opaque-random-token"
    browser.session_state["local_auth_token"] = "current-session-token"
    assert request_cookie(browser) == "current-session-token"
    forget_token(browser)
    assert browser.session_state == {"clear_local_auth_cookie": True}


def test_cookie_bridge_uses_supported_iframe_api() -> None:
    calls: list[tuple[str, int, int]] = []
    streamlit = SimpleNamespace(
        iframe=lambda source, *, width, height: calls.append(
            (source, width, height)
        )
    )
    render_cookie_bridge(streamlit, clear=True, secure=True)
    assert len(calls) == 1
    source, width, height = calls[0]
    assert "english_activity_session=" in source
    assert "SameSite=Lax" in source
    assert "; Secure" in source
    assert (width, height) == (1, 1)


def test_cookie_bridge_renders_with_real_streamlit() -> None:
    app = AppTest.from_string(
        """
from datetime import datetime, timedelta, timezone
import streamlit as st
from english_leaderboard.browser_session import render_cookie_bridge

render_cookie_bridge(
    st,
    token="opaque-test-token",
    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
)
st.write("ponte renderizada")
"""
    )
    app.run(timeout=10)
    assert not app.exception
    assert app.markdown[0].value == "ponte renderizada"


def test_pending_cookie_is_written_into_stable_slot_and_consumed(monkeypatch) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    state = {"local_auth_expires_at": expires_at.isoformat()}
    monkeypatch.setattr(streamlit_app.st, "session_state", state)
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        streamlit_app,
        "render_cookie_bridge",
        lambda slot, **kwargs: calls.append((slot, kwargs)),
    )
    slot = object()
    auth_state = streamlit_app.AuthenticationState(
        actor=None,
        local_token="opaque-token",
    )

    streamlit_app._flush_browser_session_bridge(
        slot, SimpleNamespace(is_production=True), auth_state
    )

    assert calls == [
        (
            slot,
            {
                "token": "opaque-token",
                "expires_at": expires_at,
                "secure": True,
            },
        )
    ]
    assert "local_auth_expires_at" not in state


def test_cookie_clear_takes_precedence_and_consumes_pending_state(monkeypatch) -> None:
    state = {
        "clear_local_auth_cookie": True,
        "local_auth_expires_at": "must-not-be-used",
    }
    monkeypatch.setattr(streamlit_app.st, "session_state", state)
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        streamlit_app,
        "render_cookie_bridge",
        lambda slot, **kwargs: calls.append((slot, kwargs)),
    )
    slot = object()

    streamlit_app._flush_browser_session_bridge(
        slot,
        SimpleNamespace(is_production=True),
        streamlit_app.AuthenticationState(actor=None, local_token="ignored"),
    )

    assert calls == [(slot, {"clear": True, "secure": True})]
    assert state == {}
