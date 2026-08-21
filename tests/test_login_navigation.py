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

    state = streamlit_app.authenticate(
        session, replace(settings, local_auth_enabled=False)
    )

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
    assert (
        streamlit_app._account_route(session, users[Role.ADMIN], settings).label
        == "Minha conta"
    )
    assert {route.label for route in admin} >= {"Visão geral", "Envios", "Relatórios"}
    assert "Reuniões" not in {route.label for route in admin}
    assert {route.label for route in student} >= {"Início", "Enviar", "Ranking"}
    assert "Entrar" not in {route.label for route in admin}
    assert "Entrar" not in {route.label for route in student}
    assert {route.url_path for route in public} == {"root"}


def test_registered_navigation_is_stable_across_authentication_states(
    session,
    settings,
    users,
) -> None:
    states = [
        streamlit_app.AuthenticationState(actor=None),
        streamlit_app.AuthenticationState(actor=users[Role.ADMIN]),
        streamlit_app.AuthenticationState(actor=users[Role.STUDENT]),
    ]

    snapshots = [
        [
            (route.label, route.url_path, route.icon)
            for route in streamlit_app._registered_routes(session, settings, state)
        ]
        for state in states
    ]

    assert snapshots[0] == snapshots[1] == snapshots[2]
    assert [url_path for _, url_path, _ in snapshots[0]] == [
        "root",
        "submissions",
        "users",
        "catalog",
        "ledger",
        "reminders",
        "submit",
        "history",
        "leaderboard",
        "account",
        "change-password",
    ]
    assert snapshots[0][0][0] == "English Activities"


def test_visible_navigation_changes_without_changing_registered_pages(
    session,
    settings,
    users,
) -> None:
    anonymous = streamlit_app.AuthenticationState(actor=None)
    admin = streamlit_app.AuthenticationState(actor=users[Role.ADMIN])
    student = streamlit_app.AuthenticationState(actor=users[Role.STUDENT])

    assert [
        route.label
        for route in streamlit_app._visible_routes(session, settings, anonymous)
    ] == ["Entrar"]
    assert [
        route.label for route in streamlit_app._visible_routes(session, settings, admin)
    ] == [
        "Visão geral",
        "Envios",
        "Alunos",
        "Catálogo",
        "Relatórios",
        "Lembretes",
        "Minha conta",
    ]
    assert [
        route.label
        for route in streamlit_app._visible_routes(session, settings, student)
    ] == ["Início", "Enviar", "Histórico", "Ranking", "Minha conta"]


def test_private_route_does_not_run_for_anonymous_user(
    session,
    settings,
    monkeypatch,
) -> None:
    calls: list[str] = []
    auth_state = streamlit_app.AuthenticationState(actor=None)
    protected = streamlit_app.PageRoute(
        "Catálogo",
        "catalog",
        ":material/menu_book:",
        lambda: calls.append("protected"),
    )
    guarded = streamlit_app._guarded_route(
        protected,
        session=session,
        settings=settings,
        auth_state=auth_state,
        allowed_roles=frozenset({Role.ADMIN}),
    )
    monkeypatch.setattr(
        streamlit_app,
        "_render_login_state",
        lambda *_args: calls.append("login"),
    )

    guarded.render()

    assert calls == ["login"]


def test_private_admin_route_does_not_run_for_student(
    session,
    settings,
    users,
    monkeypatch,
) -> None:
    calls: list[str] = []
    auth_state = streamlit_app.AuthenticationState(actor=users[Role.STUDENT])
    protected = streamlit_app.PageRoute(
        "Catálogo",
        "catalog",
        ":material/menu_book:",
        lambda: calls.append("protected"),
    )
    guarded = streamlit_app._guarded_route(
        protected,
        session=session,
        settings=settings,
        auth_state=auth_state,
        allowed_roles=frozenset({Role.ADMIN}),
    )
    monkeypatch.setattr(
        streamlit_app.st,
        "error",
        lambda *_args: calls.append("error"),
    )
    monkeypatch.setattr(
        streamlit_app,
        "_root_view",
        lambda *_args: calls.append("root"),
    )

    guarded.render()

    assert calls == ["error", "root"]


def test_private_route_forces_required_password_change(
    session,
    settings,
    users,
    monkeypatch,
) -> None:
    calls: list[str] = []
    actor = users[Role.ADMIN]
    actor.must_change_password = True
    auth_state = streamlit_app.AuthenticationState(actor=actor)
    protected = streamlit_app.PageRoute(
        "Catálogo",
        "catalog",
        ":material/menu_book:",
        lambda: calls.append("protected"),
    )
    guarded = streamlit_app._guarded_route(
        protected,
        session=session,
        settings=settings,
        auth_state=auth_state,
        allowed_roles=frozenset({Role.ADMIN}),
    )
    monkeypatch.setattr(
        streamlit_app,
        "forced_password_change_view",
        lambda *_args: calls.append("password"),
    )

    guarded.render()

    assert calls == ["password"]


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

    registered_routes = [
        streamlit_app.PageRoute(
            "English Activities", "root", ":material/home:", lambda: None
        ),
        streamlit_app.PageRoute(
            "Catálogo", "catalog", ":material/menu_book:", lambda: None
        ),
    ]
    visible_routes = [
        streamlit_app.PageRoute("Entrar", "root", ":material/login:", lambda: None),
    ]
    streamlit_app._run_navigation(
        registered_routes,
        visible_routes=visible_routes,
    )

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
    assert [page.url_path for page in calls["pages"]] == ["root", "catalog"]
    assert len(calls["images"]) == 1
    assert calls["ran"] is True
