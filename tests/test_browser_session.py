from __future__ import annotations

from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

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
