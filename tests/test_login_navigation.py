from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import streamlit_app
from english_leaderboard.models import Role


def test_demo_authentication_requires_explicit_session_state(
    session,
    settings,
    users,
    monkeypatch,
) -> None:
    state: dict[str, str] = {}
    monkeypatch.setattr(streamlit_app.st, "session_state", state)
    demo_settings = replace(settings, demo_auth_enabled=True)

    logged_out = streamlit_app.authenticate(session, demo_settings)

    assert logged_out.actor is None
    state["demo_user_id"] = users[Role.STUDENT].id
    logged_in = streamlit_app.authenticate(session, demo_settings)
    assert logged_in.actor is users[Role.STUDENT]


def test_missing_oidc_configuration_does_not_crash(
    session,
    settings,
    monkeypatch,
) -> None:
    monkeypatch.setattr(streamlit_app.st, "user", {})

    state = streamlit_app.authenticate(session, settings)

    assert state.actor is None
    assert state.oidc_available is False
    assert state.oidc_logged_in is False
    assert "não configurada" in (state.error or "")


def test_logged_out_oidc_configuration_remains_available(
    session,
    settings,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        streamlit_app.st,
        "user",
        SimpleNamespace(is_logged_in=False),
    )

    state = streamlit_app.authenticate(session, settings)

    assert state.actor is None
    assert state.oidc_available is True
    assert state.error is None


def test_public_and_authenticated_navigation_expose_account_routes(
    session,
    settings,
    users,
) -> None:
    public = streamlit_app._public_routes(
        session,
        settings,
        streamlit_app.AuthenticationState(actor=None),
    )
    admin = streamlit_app._admin_routes(session, users[Role.ADMIN], settings)
    student = streamlit_app._student_routes(session, users[Role.STUDENT], settings)

    assert [route.label for route in public] == ["Entrar"]
    assert streamlit_app._account_route(users[Role.ADMIN], settings).label == "Minha conta"
    assert {route.label for route in admin} >= {"Visão geral", "Revisões", "Relatórios"}
    assert {route.label for route in student} >= {"Início", "Enviar", "Ranking"}
    assert "Entrar" not in {route.label for route in admin}
    assert "Entrar" not in {route.label for route in student}
    assert {route.url_path for route in public} == {"login"}


def test_navigation_uses_hidden_router_and_visible_page_links(monkeypatch) -> None:
    calls: dict[str, object] = {"links": [], "containers": [], "images": []}

    class FakeContainer:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    selected = SimpleNamespace(run=lambda: calls.__setitem__("ran", True))

    monkeypatch.setattr(
        streamlit_app.st,
        "Page",
        lambda render, **kwargs: SimpleNamespace(render=render, **kwargs),
    )
    monkeypatch.setattr(
        streamlit_app.st,
        "navigation",
        lambda pages, *, position: (
            calls.update({"pages": pages, "position": position}) or selected
        ),
    )
    monkeypatch.setattr(
        streamlit_app.st,
        "container",
        lambda **kwargs: calls["containers"].append(kwargs) or FakeContainer(),
    )
    monkeypatch.setattr(
        streamlit_app.st,
        "page_link",
        lambda page, **kwargs: calls["links"].append((page, kwargs)),
    )
    monkeypatch.setattr(
        streamlit_app.st,
        "image",
        lambda *args, **kwargs: calls["images"].append((args, kwargs)),
    )
    monkeypatch.setattr(streamlit_app.st, "markdown", lambda *args, **kwargs: None)

    routes = [
        streamlit_app.PageRoute("Entrar", "login", ":material/login:", lambda: None),
    ]
    streamlit_app._run_navigation(routes)

    assert calls["position"] == "hidden"
    assert calls["containers"] == [
        {
            "key": "brand_header",
            "horizontal": True,
            "horizontal_alignment": "left",
            "vertical_alignment": "center",
            "gap": "small",
        },
        {
            "key": "top_nav",
            "horizontal": True,
            "horizontal_alignment": "left",
            "vertical_alignment": "center",
            "gap": "small",
        },
    ]
    assert [link[1]["label"] for link in calls["links"]] == ["Entrar"]
    assert len(calls["images"]) == 1
    assert calls["ran"] is True
