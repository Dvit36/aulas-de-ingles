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

    assert [route.label for route in public] == ["Início", "Entrar"]
    assert streamlit_app._account_route(users[Role.ADMIN], settings).label == "Minha conta"
    assert {route.label for route in admin} >= {"Visão geral", "Revisões", "Relatórios"}
    assert {route.label for route in student} >= {"Início", "Enviar", "Ranking"}
    assert "Entrar" not in {route.label for route in admin}
    assert "Entrar" not in {route.label for route in student}
    assert {route.url_path for route in public} == {"home", "login"}


def test_navigation_uses_hidden_router_and_visible_page_links(monkeypatch) -> None:
    calls: dict[str, object] = {"links": []}

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
        lambda **kwargs: calls.update({"container": kwargs}) or FakeContainer(),
    )
    monkeypatch.setattr(
        streamlit_app.st,
        "page_link",
        lambda page, **kwargs: calls["links"].append((page, kwargs)),
    )
    monkeypatch.setattr(
        streamlit_app.st,
        "title",
        lambda value: calls.update({"title": value}),
    )

    routes = [
        streamlit_app.PageRoute("Início", "home", ":material/home:", lambda: None),
        streamlit_app.PageRoute("Entrar", "login", ":material/login:", lambda: None),
    ]
    streamlit_app._run_navigation(routes)

    assert calls["position"] == "hidden"
    assert calls["container"] == {
        "key": "top_nav",
        "horizontal": True,
        "horizontal_alignment": "left",
        "vertical_alignment": "center",
        "gap": "small",
    }
    assert [link[1]["label"] for link in calls["links"]] == ["Início", "Entrar"]
    assert calls["title"] == "English Activities & Leaderboard"
    assert calls["ran"] is True
