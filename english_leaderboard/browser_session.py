"""Persist opaque local-auth sessions across Streamlit browser connections.

Streamlit Community Cloud only forwards an allowlist of platform cookies to an
app's WebSocket. A custom cookie therefore works locally but disappears from
``st.context.cookies`` after a Cloud refresh. This module uses an official v2
component to exchange one opaque, revocable token through ``localStorage``.
No identity, role, password, or other PII is stored in the browser.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

# Read only for migration from the previous release. Community Cloud filters
# this cookie, but a self-hosted browser may still send it once after upgrade.
COOKIE_NAME = "english_activity_session"
STORAGE_KEY = "english_activities.session.v1"
COMPONENT_KEY = "browser_session_store"
COMMAND_KEY = "browser_session_command"
LOCAL_TOKEN_KEY = "local_auth_token"

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{64}$")

_BROWSER_SESSION_JS = f"""
const STORAGE_KEY = {STORAGE_KEY!r};
const LEGACY_COOKIE = {COOKIE_NAME!r};

function samePayload(left, right) {{
  return JSON.stringify(left ?? null) === JSON.stringify(right ?? null);
}}

function validRecord(value) {{
  if (!value || value.version !== 1) return null;
  if (typeof value.token !== "string" || !/^[A-Za-z0-9_-]{{64}}$/.test(value.token)) {{
    return null;
  }}
  if (typeof value.expires_at !== "string") return null;
  const expiresAt = Date.parse(value.expires_at);
  if (!Number.isFinite(expiresAt)) return null;
  return {{version: 1, token: value.token, expires_at: value.expires_at}};
}}

export default function(component) {{
  const {{data, setStateValue}} = component;
  let storageAvailable = true;
  let record = null;
  let ackId = data?.previous_payload?.ack_id ?? null;

  try {{
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw) {{
      try {{
        record = validRecord(JSON.parse(raw));
      }} catch (_) {{
        record = null;
      }}
      if (!record) window.localStorage.removeItem(STORAGE_KEY);
    }}

    const command = data?.command;
    if (command?.op === "clear" && typeof command.id === "string") {{
      window.localStorage.removeItem(STORAGE_KEY);
      record = null;
      ackId = command.id;
    }} else if (
      command?.op === "write" &&
      typeof command.id === "string" &&
      typeof command.token === "string" &&
      /^[A-Za-z0-9_-]{{64}}$/.test(command.token) &&
      typeof command.expires_at === "string" &&
      Number.isFinite(Date.parse(command.expires_at))
    ) {{
      record = {{
        version: 1,
        token: command.token,
        expires_at: command.expires_at,
      }};
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(record));
      ackId = command.id;
    }}

  }} catch (_) {{
    storageAvailable = false;
    record = null;
  }}

  // Cookie cleanup is best-effort migration work and must never degrade the
  // localStorage channel used by current sessions.
  try {{
    document.cookie = `${{LEGACY_COOKIE}}=; Path=/; Max-Age=0; SameSite=Lax`;
  }} catch (_) {{}}

  const payload = {{
    ready: true,
    storage_available: storageAvailable,
    token: record?.token ?? null,
    expires_at: record?.expires_at ?? null,
    ack_id: ackId,
  }};
  if (!samePayload(payload, data?.previous_payload)) {{
    setStateValue("payload", payload);
  }}
}}
"""


@dataclass(frozen=True)
class BrowserSessionSnapshot:
    ready: bool = False
    storage_available: bool = True
    token: str | None = None
    expires_at: datetime | None = None
    ack_id: str | None = None


def valid_session_token(value: object) -> str | None:
    """Return a strictly bounded opaque token or ``None`` for untrusted input."""

    if not isinstance(value, str) or not _TOKEN_PATTERN.fullmatch(value):
        return None
    return value


def _parse_expiry(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def _snapshot(payload: object) -> BrowserSessionSnapshot:
    if not isinstance(payload, Mapping):
        return BrowserSessionSnapshot()
    ready = payload.get("ready") is True
    storage_available = payload.get("storage_available") is not False
    token = valid_session_token(payload.get("token"))
    expires_at = _parse_expiry(payload.get("expires_at"))
    if not storage_available or expires_at is None:
        token = None
        expires_at = None
    ack_value = payload.get("ack_id")
    ack_id = ack_value if isinstance(ack_value, str) and len(ack_value) <= 64 else None
    return BrowserSessionSnapshot(
        ready=ready,
        storage_available=storage_available,
        token=token,
        expires_at=expires_at,
        ack_id=ack_id,
    )


@lru_cache(maxsize=1)
def _component_renderer() -> Callable[..., Any]:
    import streamlit as st

    return st.components.v2.component(
        "english_activity_browser_session",
        js=_BROWSER_SESSION_JS,
    )


def _pending_command(st_module: Any) -> dict[str, str] | None:
    value = st_module.session_state.get(COMMAND_KEY)
    if not isinstance(value, Mapping):
        return None
    command_id = value.get("id")
    operation = value.get("op")
    if not isinstance(command_id, str) or len(command_id) > 64:
        return None
    if operation == "clear":
        return {"id": command_id, "op": "clear"}
    token = valid_session_token(value.get("token"))
    expires_at = value.get("expires_at")
    if operation != "write" or token is None or _parse_expiry(expires_at) is None:
        return None
    return {
        "id": command_id,
        "op": "write",
        "token": token,
        "expires_at": str(expires_at),
    }


def mount_browser_session(
    st_module: Any,
    *,
    renderer: Callable[..., Any] | None = None,
) -> BrowserSessionSnapshot:
    """Mount the storage bridge and consume a command only after its ACK."""

    has_raw_command = COMMAND_KEY in st_module.session_state
    command = _pending_command(st_module)
    if has_raw_command and command is None:
        st_module.session_state.pop(COMMAND_KEY, None)
    prior_state = st_module.session_state.get(COMPONENT_KEY)
    previous_payload: object = None
    if isinstance(prior_state, Mapping):
        previous_payload = prior_state.get("payload")
    default_payload = {
        "ready": False,
        "storage_available": True,
        "token": None,
        "expires_at": None,
        "ack_id": None,
    }
    component = renderer or _component_renderer()
    result = component(
        data={"command": command, "previous_payload": previous_payload},
        default={"payload": default_payload},
        key=COMPONENT_KEY,
        on_payload_change=lambda: None,
        width=1,
        height=1,
    )
    result_payload = result.get("payload") if isinstance(result, Mapping) else None
    snapshot = _snapshot(result_payload)
    write_acknowledged = (
        command is not None
        and command["op"] == "write"
        and snapshot.token == command["token"]
    )
    clear_acknowledged = (
        command is not None and command["op"] == "clear" and snapshot.token is None
    )
    if (
        command is not None
        and snapshot.ack_id == command["id"]
        and (write_acknowledged or clear_acknowledged)
    ):
        st_module.session_state.pop(COMMAND_KEY, None)
    elif command is not None and snapshot.ready and not snapshot.storage_available:
        # The browser explicitly rejected localStorage. Keeping a command that
        # can never be acknowledged would strand the UI in its handoff state.
        st_module.session_state.pop(COMMAND_KEY, None)
    return snapshot


def request_token(st_module: Any) -> str | None:
    """Read only bounded session material already delivered to Python."""

    state_token = valid_session_token(st_module.session_state.get(LOCAL_TOKEN_KEY))
    if state_token:
        return state_token
    try:
        legacy_cookie = st_module.context.cookies.get(COOKIE_NAME)
    except Exception:  # noqa: BLE001 - Streamlit context varies by runtime.
        return None
    return valid_session_token(legacy_cookie)


def remember_token(st_module: Any, token: str) -> None:
    normalized = valid_session_token(token)
    if normalized is None:
        raise ValueError("Token de sessão inválido")
    st_module.session_state[LOCAL_TOKEN_KEY] = normalized


def queue_token_write(st_module: Any, token: str, expires_at: datetime) -> None:
    normalized = valid_session_token(token)
    if normalized is None:
        raise ValueError("Token de sessão inválido")
    aware_expiry = expires_at
    if aware_expiry.tzinfo is None:
        aware_expiry = aware_expiry.replace(tzinfo=UTC)
    remember_token(st_module, normalized)
    st_module.session_state[COMMAND_KEY] = {
        "id": secrets.token_hex(16),
        "op": "write",
        "token": normalized,
        "expires_at": aware_expiry.astimezone(UTC).isoformat(),
    }


def forget_token(st_module: Any) -> None:
    st_module.session_state.pop(LOCAL_TOKEN_KEY, None)
    current = _pending_command(st_module)
    if current is not None and current["op"] == "clear":
        return
    st_module.session_state[COMMAND_KEY] = {
        "id": secrets.token_hex(16),
        "op": "clear",
    }


# Backwards-compatible name for callers outside the UI module.
request_cookie = request_token


__all__ = [
    "COMMAND_KEY",
    "COMPONENT_KEY",
    "COOKIE_NAME",
    "BrowserSessionSnapshot",
    "forget_token",
    "mount_browser_session",
    "queue_token_write",
    "remember_token",
    "request_cookie",
    "request_token",
    "valid_session_token",
]
