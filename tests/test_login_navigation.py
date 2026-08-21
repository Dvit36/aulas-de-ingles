from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import streamlit_app
from english_leaderboard.models import Role


def test_demo_authentication_requires_explicit_session_state(
    session,
    settings,
    users,
    monkeypatch,
) -> None:
    state: dict[str, object] = {}
    monkeypatch.setattr(streamlit_app.st, "session_state", state)
    demo_settings = replace(settings, demo_auth_enabled=True)

    logged_out = streamlit_app.authenticate(session, demo_settings)

    assert logged_out.actor is None
    state["demo_user_id"] = users[Role.STUDENT].id
    logged_in = streamlit_app.authenticate(session, demo_settings)
    assert logged_in.actor is users[Role.STUDENT]
    assert logged_in.local_token is not None
    assert state["browser_session_command"]["op"] == "write"


def test_pending_browser_write_holds_private_ui_until_ack(
    session,
    settings,
    users,
    monkeypatch,
) -> None:
    token = "A" * 64
    state: dict[str, object] = {
        "local_auth_token": token,
        "browser_session_command": {
            "id": "command-1",
            "op": "write",
            "token": token,
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    }
    monkeypatch.setattr(streamlit_app.st, "session_state", state)

    auth_state = streamlit_app.authenticate(session, settings)

    assert auth_state.actor is None
    assert auth_state.restoring_session is True


def test_stale_component_snapshot_cannot_replace_pending_login_token(
    settings,
    monkeypatch,
) -> None:
    new_token = "B" * 64
    state: dict[str, object] = {
        "local_auth_token": new_token,
        "browser_session_command": {
            "id": "new-login",
            "op": "write",
            "token": new_token,
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
    }
    monkeypatch.setattr(streamlit_app.st, "session_state", state)
    monkeypatch.setattr(
        streamlit_app,
        "mount_browser_session",
        lambda _st: streamlit_app.BrowserSessionSnapshot(
            ready=True,
            token="A" * 64,
        ),
    )

    streamlit_app._mount_persistent_browser_session(settings)

    assert state["local_auth_token"] == new_token
    assert state["browser_session_command"]["op"] == "write"


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
    assert {route.label for route in admin} == {
        "Visão geral",
        "Envios",
        "Alunos",
        "Catálogo",
    }
    assert "Relatórios" not in {route.label for route in admin}
    assert "Lembretes" not in {route.label for route in admin}
    assert "Reuniões" not in {route.label for route in admin}
    assert {route.label for route in student} >= {"Início", "Enviar", "Ranking"}
    # Recursos é compartilhada pelos dois papéis, como Minha conta.
    assert (
        streamlit_app._resources_route(session, users[Role.ADMIN], settings).url_path
        == "resources"
    )
    assert "Entrar" not in {route.label for route in admin}
    assert "Entrar" not in {route.label for route in student}
    assert {route.url_path for route in public} == {"root"}


def test_review_dropdown_groups_submission_statuses() -> None:
    statuses = streamlit_app.SubmissionStatus
    assert streamlit_app._review_statuses("needs_review") == {statuses.NEEDS_REVIEW}
    assert streamlit_app._review_statuses("approved") == {
        statuses.APPROVED_AUTO,
        statuses.APPROVED_MANUAL,
    }
    assert streamlit_app._review_statuses("rejected") == {statuses.REJECTED}
    assert streamlit_app._review_statuses("all") == set(statuses)


def test_student_metrics_exclude_administrator_accounts() -> None:
    accounts = [
        SimpleNamespace(role=Role.ADMIN, active=True, archived_at=None),
        SimpleNamespace(role=Role.STUDENT, active=True, archived_at=None),
        SimpleNamespace(role=Role.STUDENT, active=False, archived_at=None),
        SimpleNamespace(role=Role.STUDENT, active=False, archived_at=object()),
    ]
    assert streamlit_app._student_account_counts(accounts) == (1, 1, 1)


def test_registered_navigation_is_stable_across_authentication_states(
    session,
    settings,
    users,
) -> None:
    states = [
        streamlit_app.AuthenticationState(actor=None),
        streamlit_app.AuthenticationState(actor=None, restoring_session=True),
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

    assert snapshots[0] == snapshots[1] == snapshots[2] == snapshots[3]
    assert [url_path for _, url_path, _ in snapshots[0]] == [
        "root",
        "submissions",
        "users",
        "catalog",
        "submit",
        "history",
        "leaderboard",
        "resources",
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
    restoring = streamlit_app.AuthenticationState(
        actor=None,
        restoring_session=True,
    )
    admin = streamlit_app.AuthenticationState(actor=users[Role.ADMIN])
    student = streamlit_app.AuthenticationState(actor=users[Role.STUDENT])

    assert [
        route.label
        for route in streamlit_app._visible_routes(session, settings, anonymous)
    ] == ["Entrar"]
    assert streamlit_app._visible_routes(session, settings, restoring) == []
    assert [
        route.label for route in streamlit_app._visible_routes(session, settings, admin)
    ] == [
        "Visão geral",
        "Envios",
        "Alunos",
        "Catálogo",
        "Recursos",
        "Minha conta",
    ]
    assert [
        route.label
        for route in streamlit_app._visible_routes(session, settings, student)
    ] == [
        "Início",
        "Enviar",
        "Histórico",
        "Ranking",
        "Recursos",
        "Minha conta",
    ]


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


def test_command_handoff_registers_router_before_rerun_without_rendering(
    monkeypatch,
) -> None:
    events: list[str] = []
    selected = SimpleNamespace(run=lambda: events.append("page"))
    monkeypatch.setattr(
        streamlit_app.st,
        "Page",
        lambda render, **kwargs: SimpleNamespace(render=render, **kwargs),
    )
    monkeypatch.setattr(
        streamlit_app.st,
        "navigation",
        lambda _pages, *, position: events.append(f"navigation:{position}") or selected,
    )

    class RerunRequested(RuntimeError):
        pass

    def request_rerun() -> None:
        events.append("rerun")
        raise RerunRequested

    monkeypatch.setattr(streamlit_app.st, "rerun", request_rerun)
    route = streamlit_app.PageRoute(
        "English Activities",
        "root",
        ":material/home:",
        lambda: None,
    )

    with pytest.raises(RerunRequested):
        streamlit_app._run_navigation([route], rerun_before_render=True)

    assert events == ["navigation:hidden", "rerun"]


def test_leaderboard_markup_has_no_empty_wrapper_and_ranks_the_podium() -> None:
    rows = [
        {"position": 1, "student_id": "a", "student": "Ana Souza (Demo)", "points": 85},
        {"position": 2, "student_id": "b", "student": "Bruno Lima (Demo)", "points": 77},
        {"position": 3, "student_id": "c", "student": "Carla Mendes", "points": 70},
        {"position": 4, "student_id": "d", "student": "Diego Rocha", "points": 69},
    ]

    podium = streamlit_app._podium_html(rows[:3])
    board = streamlit_app._board_html(rows, "d")

    # Um único cartão do pódio: nada de containers vazios acima dos colocados.
    assert podium.count('class="robo-podium"') == 1
    assert podium.count('class="robo-podium-slot"') == 3
    # Ordem visual 2º, 1º, 3º com o primeiro lugar em amarelo e mais alto.
    assert podium.index("2º") < podium.index("1º") < podium.index("3º")
    assert "background: var(--robo-yellow); min-height: 7.25rem;" in podium
    assert board.count('class="robo-board-row"') == 4
    assert board.count("robo-board-badge") == 1


def test_leaderboard_initials_ignore_demo_markers_and_escape_names() -> None:
    assert streamlit_app._initials("Ana Souza (Demo)") == "AS"
    assert streamlit_app._initials("José da Silva Neto") == "JN"
    assert streamlit_app._initials("Madonna") == "M"
    assert streamlit_app._initials("") == "?"

    rows = [
        {
            "position": 1,
            "student_id": "x",
            "student": "<script>alert(1)</script>",
            "points": 3,
        }
    ]
    markup = streamlit_app._board_html(rows, None)
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_resource_cards_escape_content_and_open_links_safely() -> None:
    from english_leaderboard.models import Resource

    resources = [
        Resource(
            title="<script>alert(1)</script>",
            url="https://exemplo.org/caminho?a=1&b=2",
            description="Descrição com <b>tag</b> & e-comercial",
            position=1,
        ),
        Resource(title="Sem descrição", url="https://www.outro.org/", position=2),
    ]

    markup = streamlit_app._resources_html(resources)

    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup
    assert "&lt;b&gt;tag&lt;/b&gt;" in markup
    # Aspas do href escapadas para o atributo não poder ser fechado.
    assert 'href="https://exemplo.org/caminho?a=1&amp;b=2"' in markup
    assert markup.count('rel="noopener noreferrer"') == 2
    assert markup.count('target="_blank"') == 2
    # O domínio aparece para o aluno saber o destino antes de tocar.
    assert ">exemplo.org<" in markup
    assert ">outro.org<" in markup  # o www. é removido


def test_dashboard_shows_the_weekly_goal_and_no_lesson_progress_metric() -> None:
    import inspect

    source = inspect.getsource(streamlit_app.student_dashboard)

    assert "Meta da semana" in source
    assert "Progresso de lições" not in source
    assert "lesson_progress" not in source


def test_runtime_cache_is_keyed_by_the_expected_schema() -> None:
    """O Cloud troca o código sem reiniciar o processo.

    Sem a impressão do schema na chave do cache, o engine inicializado pela
    versão anterior sobreviveria e uma tabela recém-adicionada nunca seria
    criada — foi assim que `resources` sumiu em produção.
    """

    import inspect as inspect_module
    from hashlib import sha256

    from english_leaderboard.database import Base

    esperado = sha256(
        ",".join(sorted(Base.metadata.tables)).encode("utf-8")
    ).hexdigest()[:16]
    assert streamlit_app._schema_fingerprint() == esperado

    # A impressão precisa cobrir toda tabela mapeada, não uma lista fixa.
    assert "resources" in Base.metadata.tables
    assert "goal_configuration" in Base.metadata.tables

    # E o runtime precisa mesmo receber a impressão, senão o cache não muda.
    assert "schema_fingerprint" in inspect_module.signature(
        streamlit_app.runtime.__wrapped__
    ).parameters
    assert "runtime(_schema_fingerprint())" in inspect_module.getsource(
        streamlit_app.main
    )
