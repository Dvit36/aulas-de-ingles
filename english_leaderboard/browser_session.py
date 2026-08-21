"""Bridge an opaque database session token to a same-site browser cookie.

Streamlit exposes request cookies read-only, so setting/clearing uses a tiny static
component.  The cookie contains only a random opaque token; password and role are
always resolved server-side from the database.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any


COOKIE_NAME = "english_activity_session"


def request_cookie(st_module: Any) -> str | None:
    state_token = st_module.session_state.get("local_auth_token")
    if state_token:
        return str(state_token)
    try:
        value = st_module.context.cookies.get(COOKIE_NAME)
    except Exception:
        return None
    return str(value) if value else None


def remember_token(st_module: Any, token: str) -> None:
    st_module.session_state["local_auth_token"] = token


def forget_token(st_module: Any) -> None:
    st_module.session_state.pop("local_auth_token", None)
    st_module.session_state["clear_local_auth_cookie"] = True


def render_cookie_bridge(
    st_module: Any,
    *,
    token: str | None = None,
    expires_at: datetime | None = None,
    clear: bool = False,
    secure: bool = False,
) -> None:
    """Write or clear the opaque cookie without interpolating untrusted markup."""

    if clear:
        assignment = (
            f"{COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Lax"
            + ("; Secure" if secure else "")
        )
    elif token and expires_at:
        expires = expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        max_age = max(0, int((expires - datetime.now(timezone.utc)).total_seconds()))
        assignment = (
            f"{COOKIE_NAME}={token}; Path=/; Max-Age={max_age}; SameSite=Lax"
            + ("; Secure" if secure else "")
        )
    else:
        return
    script = (
        "<script>window.parent.document.cookie = "
        + json.dumps(assignment)
        + ";</script>"
    )
    # Streamlit rejects zero-sized iframes. One CSS pixel keeps the bridge
    # effectively invisible while allowing the browser to execute it.
    st_module.iframe(script, width=1, height=1)


__all__ = [
    "COOKIE_NAME",
    "forget_token",
    "remember_token",
    "render_cookie_bridge",
    "request_cookie",
]
