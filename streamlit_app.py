from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from html import escape
from pathlib import Path
from uuid import uuid4

import streamlit as st
from sqlalchemy import func, select

from english_leaderboard.authz import AuthorizationError
from english_leaderboard.browser_session import (
    COMMAND_KEY,
    BrowserSessionSnapshot,
    forget_token,
    mount_browser_session,
    queue_token_write,
    remember_token,
    request_token,
)
from english_leaderboard.catalog import seed_database
from english_leaderboard.config import Settings
from english_leaderboard.database import (
    create_database_engine,
    create_session_factory,
    initialize_database,
    session_scope,
)
from english_leaderboard.exporter import (
    leaderboard_to_xlsx,
    ledger_to_xlsx,
)
from english_leaderboard.google_sheets import sync_leaderboard_and_ledger
from english_leaderboard.contas import contas_de
from english_leaderboard.storage import (
    StorageError,
    SupabaseStorageGateway,
    criar_cliente,
)
from english_leaderboard.supabase_auth import (
    AuthError,
    HttpAuthGateway,
    Sessao,
    entrar,
    renovar,
    sair,
    trocar_senha,
)
from english_leaderboard.schema import (
    Activity,
    AuditLog,
    DuplicateMatch,
    EmailAttempt,
    Resource,
    Role,
    Submission,
    SubmissionFile,
    SubmissionStatus,
    User,
    utcnow,
)
from english_leaderboard.ocr import create_ocr_engine
from english_leaderboard.reminders import (
    get_reminder_configuration,
    render_reminder,
    save_reminder_configuration,
    send_test_reminder,
)
from english_leaderboard.scoring import (
    LESSON_ACTIVITY_CODE,
    activities_closing_gap,
    leaderboard_rows,
    ledger_rows,
    next_rival,
    pending_group_progress,
    student_total,
    students_meeting_weekly_goal,
    weekly_lesson_count,
    weekly_submission_count,
)
from english_leaderboard.services import (
    UploadPayload,
    admin_ledger_rows,
    admin_student_submissions,
    archive_or_delete_activity,
    archive_or_delete_user,
    count_activity_references,
    create_activity,
    create_points_adjustment,
    create_user_account,
    get_goal_configuration,
    get_submission_file_for_user,
    list_resources,
    list_submissions,
    replace_resources,
    reset_user_password,
    review_submission,
    save_activity_changes,
    save_goal_configuration,
    save_user,
    set_activity_active,
    submit_evidence,
)
from english_leaderboard.ui_styles import render_global_styles

APP_ROOT = Path(__file__).resolve().parent
BRAND_ASSETS = APP_ROOT / "assets" / "brand"
BRAND_MARK = BRAND_ASSETS / "logo-mark.png"
BRAND_WORDMARK = BRAND_ASSETS / "logo-wordmark.png"


st.set_page_config(
    page_title="English Activities",
    page_icon=str(BRAND_MARK),
    layout="wide",
    initial_sidebar_state="collapsed",
)
render_global_styles(st)


LOGGER = logging.getLogger("english_leaderboard.ui")


@dataclass(frozen=True)
class PageRoute:
    label: str
    url_path: str
    icon: str
    render: Callable[[], None]


@dataclass(frozen=True)
class AuthenticationState:
    actor: User | None
    error: str | None = None
    sessao: Sessao | None = None
    restoring_session: bool = False
    browser_storage_available: bool = True


def show_operation_error(context: str, error: Exception) -> None:
    if isinstance(error, (ValueError, LookupError, AuthorizationError)):
        st.error(str(error))
        return
    reference = uuid4().hex[:10]
    LOGGER.exception("%s [ref=%s]", context, reference)
    st.error(f"A operação falhou. Consulte o log com a referência {reference}.")


def sync_google_sheets_snapshot(
    session,
    settings: Settings,
    *,
    notify: bool = True,
) -> bool:
    """Mirror committed reporting data without making Google the source of truth."""

    if not settings.google_sheets_auto_sync:
        return False
    try:
        board = leaderboard_rows(session)
        ledger = ledger_rows(session)
        # End the read transaction before the external API call. A slow or
        # unavailable Google API must never hold a database transaction open.
        session.commit()
        result = sync_leaderboard_and_ledger(
            settings.google_sheets_spreadsheet_id,
            board,
            ledger,
            leaderboard_tab=settings.google_sheets_leaderboard_tab,
            ledger_tab=settings.google_sheets_ledger_tab,
        )
    except Exception:
        session.rollback()
        reference = uuid4().hex[:10]
        LOGGER.exception("google_sheets_sync [ref=%s]", reference)
        if notify:
            st.warning(
                "Os dados foram salvos no sistema, mas o Google Sheets não "
                f"foi atualizado. Tente novamente. Referência: {reference}."
            )
        return False
    if notify:
        message = (
            "Google Sheets atualizado automaticamente."
            if result.changed
            else "Google Sheets já estava atualizado."
        )
        st.toast(message, icon="✅")
    return True


def persist_committed_changes(session, settings: Settings, *, notify: bool = True) -> None:
    """Atualiza o espelho opcional depois de um commit bem-sucedido."""

    sync_google_sheets_snapshot(session, settings, notify=notify)


def _schema_fingerprint() -> str:
    """Identidade do conjunto de tabelas que este código espera encontrar.

    O Streamlit Community Cloud troca o código sem reiniciar o processo, e o
    valor de `st.cache_resource` sobrevive a essa troca. Sem a impressão no
    cache, uma versão que adiciona tabela seguiria usando o engine inicializado
    pela versão anterior — `create_all` nunca rodaria de novo e a tabela nova
    não existiria.
    """

    from english_leaderboard.schema import SchemaBase

    names = ",".join(sorted(SchemaBase.metadata.tables))
    return sha256(names.encode("utf-8")).hexdigest()[:16]


@st.cache_resource
def runtime(schema_fingerprint: str):
    """Configuração e fábrica de sessões, uma vez por processo.

    O banco é o PostgreSQL do Supabase sempre que estiver configurado; o SQLite
    fica só para desenvolvimento local, onde não há Supabase à mão.
    """

    settings = Settings.from_env()
    settings.ensure_directories()
    engine = create_database_engine(
        settings.supabase_db_url or settings.database_url
    )
    initialize_database(engine)
    factory = create_session_factory(engine)
    with session_scope(factory, servico=True) as session:
        # Semear catálogo e administrador inicial é manutenção, não operação
        # de aluno: roda como dono do banco, de forma explícita.
        seed_database(
            session,
            settings,
            contas_de(settings) if settings.supabase_ready else None,
        )
    return settings, factory


@st.cache_resource(show_spinner="Carregando OCR local…")
def cached_ocr_engine():
    """Uma única instância ONNX por processo Streamlit."""

    return create_ocr_engine()


SESSAO_KEY = "supabase_sessao"


def _storage_gateway(settings: Settings) -> SupabaseStorageGateway:
    """Bucket autenticado com o token do próprio usuário.

    É o que faz as políticas de ``storage.objects`` valerem: sem o token, a
    operação correria como anônima e o bucket privado recusaria. A chave
    privilegiada nunca aparece aqui — ela ignoraria a RLS e poderia criar
    objeto sem dono.
    """

    sessao = st.session_state.get(SESSAO_KEY)
    if sessao is None:
        raise StorageError("Sessão ausente: operação de arquivo não autorizada")
    cliente = criar_cliente(
        settings.supabase_url,
        settings.supabase_publishable_key,
        sessao.access_token,
    )
    return SupabaseStorageGateway(cliente, settings.storage_bucket)


@st.cache_resource
def _auth_gateway(base_url: str):
    """Um cliente HTTP por processo, reaproveitado entre reruns."""

    return HttpAuthGateway(base_url)


def _esquecer_sessao() -> None:
    st.session_state.pop(SESSAO_KEY, None)
    forget_token(st)


def _guardar_sessao(sessao: Sessao) -> None:
    """Mantém a sessão viva no rerun e o refresh token no navegador.

    O que vai para o navegador é o *refresh* token, nunca o de acesso: ele é
    o único que precisa sobreviver ao fechamento da aba, e trocá-lo por um
    acesso novo exige falar com o Supabase.
    """

    st.session_state[SESSAO_KEY] = sessao
    if sessao.refresh_token:
        queue_token_write(st, sessao.refresh_token, sessao.expires_at)


def _sessao_viva(settings: Settings) -> Sessao | None:
    """Devolve a sessão do Supabase Auth, renovando quando está perto do fim.

    Duas origens: a sessão em memória (rerun normal) e o refresh token que o
    navegador guardou (aba reaberta). Em qualquer caso a renovação acontece
    antes do vencimento, para o token não expirar no meio de uma requisição.
    """

    gateway = _auth_gateway(settings.supabase_url)
    sessao = st.session_state.get(SESSAO_KEY)

    if sessao is None:
        token = request_token(st)
        if not token:
            return None
        try:
            sessao = renovar(
                gateway,
                refresh_token=token,
                chave_publica=settings.supabase_publishable_key,
            )
        except AuthError:
            _esquecer_sessao()
            return None
        _guardar_sessao(sessao)
        return sessao

    if sessao.precisa_renovar():
        try:
            sessao = renovar(
                gateway,
                refresh_token=sessao.refresh_token,
                chave_publica=settings.supabase_publishable_key,
            )
        except AuthError:
            _esquecer_sessao()
            return None
        _guardar_sessao(sessao)
    return sessao


def authenticate(
    session,
    settings: Settings,
    *,
    browser_session_ready: bool = True,
    browser_storage_available: bool = True,
) -> AuthenticationState:
    """Resolve a sessão existente. Nunca autentica ninguém implicitamente."""

    command = st.session_state.get(COMMAND_KEY)
    limpando = isinstance(command, dict) and command.get("op") == "clear"
    salvando = isinstance(command, dict) and command.get("op") == "write"

    if limpando:
        return AuthenticationState(
            actor=None, browser_storage_available=browser_storage_available
        )
    if salvando and browser_storage_available:
        return AuthenticationState(
            actor=None,
            restoring_session=True,
            browser_storage_available=True,
        )
    # Sem o componente pronto ainda não dá para saber se há sessão guardada;
    # renderizar o login aqui faria a tela piscar para quem já está logado.
    if st.session_state.get(SESSAO_KEY) is None and not browser_session_ready:
        return AuthenticationState(
            actor=None,
            restoring_session=True,
            browser_storage_available=browser_storage_available,
        )

    sessao = _sessao_viva(settings)
    if sessao is None:
        return AuthenticationState(
            actor=None, browser_storage_available=browser_storage_available
        )

    actor = session.get(User, sessao.user_id)
    if actor is None or not actor.active or actor.archived_at is not None:
        # A conta foi desativada ou removida enquanto a sessão vivia.
        _esquecer_sessao()
        return AuthenticationState(
            actor=None,
            error="Sua conta não está mais ativa. Procure o administrador.",
            browser_storage_available=browser_storage_available,
        )
    return AuthenticationState(
        actor=actor,
        sessao=sessao,
        browser_storage_available=browser_storage_available,
    )


def login_view(
    session,
    settings: Settings,
    *,
    auth_error: str | None = None,
    browser_storage_available: bool = True,
) -> None:
    st.header("Entrar")
    with st.container(border=True, key="login_card"):
        st.image(str(BRAND_WORDMARK), width=280)
        st.markdown(
            '<span class="login-kicker">English Activities</span>',
            unsafe_allow_html=True,
        )
        st.write("Acesse sua área para registrar atividades e acompanhar pontos.")
        if auth_error:
            st.error(auth_error)
        login_notice = st.session_state.pop("login_notice", None)
        if login_notice:
            st.success(str(login_notice))
        if not browser_storage_available:
            st.warning(
                "O navegador bloqueou o armazenamento da sessão. Abra o app "
                "diretamente, fora de uma incorporação, e permita os dados do site."
            )
        if not settings.supabase_ready:
            st.error(
                "Autenticação não configurada. Defina SUPABASE_URL, "
                "SUPABASE_PUBLISHABLE_KEY e SUPABASE_DB_URL nos Secrets."
            )
            return

        with st.form("login_form"):
            username = st.text_input("Usuário", autocomplete="username")
            password = st.text_input(
                "Senha", type="password", autocomplete="current-password"
            )
            submitted = st.form_submit_button("Entrar", type="primary")
        if submitted:
            try:
                sessao = entrar(
                    _auth_gateway(settings.supabase_url),
                    username=username,
                    password=password,
                    chave_publica=settings.supabase_publishable_key,
                    dominio=settings.supabase_username_domain,
                )
            except (AuthError, ValueError) as error:
                st.error(str(error))
            else:
                _guardar_sessao(sessao)
                st.rerun()


def account_view(session, actor: User, settings: Settings) -> None:
    st.header("Minha conta")
    role_label = "Administrador" if actor.role == Role.ADMIN else "Aluno"
    with st.container(border=True, key="account_card"):
        st.write(f"**Nome:** {actor.display_name}")
        st.write(f"**Usuário:** {actor.username}")
        st.write(f"**Papel:** {role_label}")
        if actor.last_login_at:
            st.write(f"**Último acesso:** {actor.last_login_at:%d/%m/%Y %H:%M}")

        sessao = st.session_state.get(SESSAO_KEY)
        with st.expander("Alterar minha senha"):
            with st.form("account_password_change"):
                new_password = st.text_input(
                    "Nova senha", type="password", key="account_new_password"
                )
                confirmation = st.text_input(
                    "Confirmar nova senha",
                    type="password",
                    key="account_password_confirmation",
                )
                password_submitted = st.form_submit_button("Alterar senha")
            if password_submitted:
                if new_password != confirmation:
                    st.error("As novas senhas não coincidem.")
                elif sessao is None:
                    st.error("Sessão expirada. Entre novamente.")
                else:
                    try:
                        # A troca usa o token do próprio usuário: a aplicação
                        # nunca precisa da chave privilegiada para isso.
                        trocar_senha(
                            _auth_gateway(settings.supabase_url),
                            access_token=sessao.access_token,
                            nova_senha=new_password,
                        )
                    except (AuthError, ValueError) as error:
                        st.error(str(error))
                    else:
                        _esquecer_sessao()
                        st.session_state["login_notice"] = (
                            "Senha alterada. Entre novamente."
                        )
                        st.rerun()

        if st.button("Sair", type="primary"):
            if sessao is not None:
                sair(
                    _auth_gateway(settings.supabase_url),
                    access_token=sessao.access_token,
                    chave_publica=settings.supabase_publishable_key,
                )
            _esquecer_sessao()
            st.rerun()


def _run_navigation(
    routes: Sequence[PageRoute],
    *,
    visible_routes: Sequence[PageRoute] | None = None,
    rerun_before_render: bool = False,
) -> None:
    """Render a role-specific, always-visible navigation bar above the page."""

    if not routes:
        return
    pages = [
        st.Page(
            route.render,
            title=route.label,
            icon=route.icon,
            url_path=route.url_path,
            default=index == 0,
        )
        for index, route in enumerate(routes)
    ]
    selected_page = st.navigation(pages, position="hidden")
    if rerun_before_render:
        # Register the stable router first so a deep link survives, but do not
        # emit a page generation that will immediately become stale.
        st.rerun()
    pages_by_path = {
        route.url_path: page for page, route in zip(pages, routes, strict=True)
    }
    with st.container(
        key="brand_header",
        horizontal=True,
        horizontal_alignment="left",
        vertical_alignment="center",
        gap="small",
    ):
        st.image(str(BRAND_WORDMARK), width=210)
        st.markdown(
            """
            <div class="brand-copy">
              <p class="brand-title">English Activities</p>
              <p class="brand-subtitle">Robonáticos #7565 · Temporada 2026</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with st.container(
        key="top_nav",
        horizontal=True,
        horizontal_alignment="left",
        vertical_alignment="center",
        gap="small",
    ):
        navigation_routes = routes if visible_routes is None else visible_routes
        for route in navigation_routes:
            page = pages_by_path[route.url_path]
            st.page_link(
                page,
                label=route.label,
                icon=route.icon,
                width="content",
            )
    selected_page.run()


def _public_routes(
    session, settings: Settings, auth_state: AuthenticationState
) -> list[PageRoute]:
    return [
        PageRoute(
            "Entrar",
            "root",
            ":material/login:",
            lambda: login_view(
                session,
                settings,
                auth_error=auth_state.error,
            ),
        ),
    ]


def _resources_route(session, actor: User | None, settings: Settings) -> PageRoute:
    """Recursos é uma rota só, compartilhada pelos dois papéis.

    O registro de páginas concatena as rotas de administrador e de aluno, então
    uma mesma URL declarada nas duas listas viraria entrada duplicada.
    """

    return PageRoute(
        "Recursos",
        "resources",
        ":material/library_books:",
        lambda: resources_view(session, actor, settings),
    )


def _account_route(session, actor: User | None, settings: Settings) -> PageRoute:
    return PageRoute(
        "Minha conta",
        "account",
        ":material/account_circle:",
        lambda: account_view(session, actor, settings),
    )


def _admin_routes(session, actor: User | None, settings: Settings) -> list[PageRoute]:
    return [
        PageRoute(
            "Visão geral",
            "root",
            ":material/dashboard:",
            lambda: admin_dashboard(session, actor),
        ),
        PageRoute(
            "Envios",
            "submissions",
            ":material/inbox:",
            lambda: review_queue_view(session, actor, settings),
        ),
        PageRoute(
            "Alunos",
            "users",
            ":material/group:",
            lambda: users_view(session, actor, settings),
        ),
        PageRoute(
            "Catálogo",
            "catalog",
            ":material/menu_book:",
            lambda: catalog_view(session, actor, settings),
        ),
    ]


def _student_routes(session, actor: User | None, settings: Settings) -> list[PageRoute]:
    return [
        PageRoute(
            "Início",
            "root",
            ":material/home:",
            lambda: student_dashboard(session, actor),
        ),
        PageRoute(
            "Enviar",
            "submit",
            ":material/upload:",
            lambda: submission_form(session, actor, settings),
        ),
        PageRoute(
            "Histórico",
            "history",
            ":material/history:",
            lambda: student_history(session, actor, settings),
        ),
        PageRoute(
            "Ranking",
            "leaderboard",
            ":material/leaderboard:",
            lambda: leaderboard_view(session, key_prefix="student_page", actor=actor),
        ),
    ]


def _render_login_state(
    session,
    settings: Settings,
    auth_state: AuthenticationState,
) -> None:
    if auth_state.restoring_session:
        st.header("Restaurando sessão")
        with st.container(border=True, key="session_restore_card"):
            st.info("Recuperando seu acesso e a página atual…")
        return
    login_view(
        session,
        settings,
        auth_error=auth_state.error,
        browser_storage_available=auth_state.browser_storage_available,
    )


def _root_view(
    session,
    settings: Settings,
    auth_state: AuthenticationState,
) -> None:
    actor = auth_state.actor
    if actor is None:
        _render_login_state(session, settings, auth_state)
    elif actor.role == Role.ADMIN:
        admin_dashboard(session, actor)
    else:
        student_dashboard(session, actor)


def _guarded_route(
    route: PageRoute,
    *,
    session,
    settings: Settings,
    auth_state: AuthenticationState,
    allowed_roles: frozenset[Role],
) -> PageRoute:
    def render() -> None:
        actor = auth_state.actor
        if actor is None:
            _render_login_state(session, settings, auth_state)
            return
        if actor.role not in allowed_roles:
            st.error("Você não tem acesso a esta página.")
            _root_view(session, settings, auth_state)
            return
        route.render()

    return PageRoute(route.label, route.url_path, route.icon, render)


def _registered_routes(
    session,
    settings: Settings,
    auth_state: AuthenticationState,
) -> list[PageRoute]:
    """Return the same page registry for anonymous, admin and student runs."""

    actor = auth_state.actor
    admin_routes = _admin_routes(session, actor, settings)
    student_routes = _student_routes(session, actor, settings)
    account_route = _account_route(session, actor, settings)
    root_route = PageRoute(
        "English Activities",
        "root",
        ":material/home:",
        lambda: _root_view(session, settings, auth_state),
    )
    registered = [root_route]
    registered.extend(
        _guarded_route(
            route,
            session=session,
            settings=settings,
            auth_state=auth_state,
            allowed_roles=frozenset({Role.ADMIN}),
        )
        for route in admin_routes[1:]
    )
    registered.extend(
        _guarded_route(
            route,
            session=session,
            settings=settings,
            auth_state=auth_state,
            allowed_roles=frozenset({Role.STUDENT}),
        )
        for route in student_routes[1:]
    )
    for shared_route in (
        _resources_route(session, actor, settings),
        account_route,
    ):
        registered.append(
            _guarded_route(
                shared_route,
                session=session,
                settings=settings,
                auth_state=auth_state,
                allowed_roles=frozenset({Role.ADMIN, Role.STUDENT}),
            )
        )

    return registered


def _visible_routes(
    session,
    settings: Settings,
    auth_state: AuthenticationState,
) -> list[PageRoute]:
    actor = auth_state.actor
    if auth_state.restoring_session:
        return []
    if actor is None:
        return _public_routes(session, settings, auth_state)
    routes = (
        _admin_routes(session, actor, settings)
        if actor.role == Role.ADMIN
        else _student_routes(session, actor, settings)
    )
    routes.append(_resources_route(session, actor, settings))
    routes.append(_account_route(session, actor, settings))
    return routes


def _initials(name: str) -> str:
    """Iniciais do avatar, ignorando marcações como "(Demo)"."""

    parts = [
        cleaned
        for part in str(name).split()
        if "(" not in part
        and ")" not in part
        and (cleaned := "".join(c for c in part if c.isalpha()))
    ]
    if not parts:
        return "?"
    letters = parts[0][:1] + (parts[-1][:1] if len(parts) > 1 else "")
    return letters.upper()


# Ordem visual do pódio e altura relativa de cada bloco: 2º, 1º, 3º.
_PODIUM_LAYOUT = (
    (1, "var(--robo-silver)", "5.25rem"),
    (0, "var(--robo-yellow)", "7.25rem"),
    (2, "var(--robo-white)", "4.125rem"),
)


def _podium_html(podium: Sequence[dict[str, object]]) -> str:
    slots = []
    for index, color, height in _PODIUM_LAYOUT:
        if index >= len(podium):
            continue
        row = podium[index]
        name = escape(str(row["student"]))
        slots.append(
            f'<div class="robo-podium-slot">'
            f'<div class="robo-avatar">{escape(_initials(row["student"]))}</div>'
            f'<span class="robo-podium-name">{name}</span>'
            f'<div class="robo-podium-block" '
            f'style="background: {color}; min-height: {height};">'
            f'<span class="robo-podium-place">{int(row["position"])}º</span>'
            f'<span class="robo-podium-points">{int(row["points"])} pts</span>'
            f"</div></div>"
        )
    return f'<div class="robo-podium">{"".join(slots)}</div>'


def _board_html(
    rows: Sequence[dict[str, object]], highlight_id: str | None
) -> str:
    entries = []
    for row in rows:
        badge = (
            '<span class="robo-board-badge">Você</span>'
            if highlight_id is not None and row["student_id"] == highlight_id
            else ""
        )
        entries.append(
            f'<div class="robo-board-row">'
            f'<span class="robo-board-place">{int(row["position"])}</span>'
            f'<div class="robo-avatar">{escape(_initials(row["student"]))}</div>'
            f'<span class="robo-board-name">{escape(str(row["student"]))}</span>'
            f"{badge}"
            f'<span class="robo-board-points">{int(row["points"])}</span>'
            f"</div>"
        )
    return f'<div class="robo-board">{"".join(entries)}</div>'


def leaderboard_view(
    session,
    *,
    key_prefix: str = "leaderboard",
    actor: User | None = None,
) -> None:
    st.subheader("Leaderboard")
    use_period = st.checkbox("Filtrar por período", key=f"{key_prefix}_period")
    start = end = None
    if use_period:
        left, right = st.columns(2)
        start = left.date_input(
            "Início", value=date.today().replace(day=1), key=f"{key_prefix}_start"
        )
        end = right.date_input("Fim", value=date.today(), key=f"{key_prefix}_end")
        if start > end:
            st.error("A data inicial deve ser anterior à final.")
            return
    rows = leaderboard_rows(session, start=start, end=end)
    if not rows:
        st.info("Ainda não há alunos no leaderboard.")
        return
    highlight_id = actor.id if actor is not None else None
    markup = _podium_html(rows[:3]) + _board_html(rows, highlight_id)
    st.markdown(markup, unsafe_allow_html=True)


def _next_rival_panel(session, actor: User) -> None:
    """Mostra a diferença até o próximo colocado e como fechá-la."""

    rival = next_rival(session, actor.id)
    if rival is None:
        st.success(
            "Você está na liderança do leaderboard. Continue enviando para manter "
            "a distância."
        )
        return
    gap = int(rival["gap"])
    with st.container(border=True, key="next_rival_card"):
        st.markdown(
            f"**Faltam {gap} ponto(s)** para alcançar **{rival['student']}** "
            f"(#{rival['position']}, {rival['points']} pontos)."
        )
        suggestions = activities_closing_gap(session, actor.id, gap)
        if not suggestions:
            st.caption("Nenhuma atividade ativa disponível no catálogo.")
            return
        st.caption("Atividades que fecham essa diferença:")
        for item in suggestions:
            if int(item["threshold"]) > 1:
                detail = (
                    f"{item['needed']} comprovação(ões) — "
                    f"{item['points']} pontos a cada {item['threshold']}"
                )
            else:
                detail = (
                    f"{item['needed']} envio(s) — {item['points']} pontos cada"
                )
            st.markdown(f"- **{item['activity']}**: {detail}")


def _resource_host(url: str) -> str:
    """Domínio do link, para o aluno ver o destino antes de tocar."""

    without_scheme = url.split("://", 1)[-1]
    host = without_scheme.split("/", 1)[0]
    return host[4:] if host.startswith("www.") else host


def _resources_html(resources: Sequence[Resource]) -> str:
    cards = []
    for resource in resources:
        url = escape(resource.url, quote=True)
        description = (
            f'<p class="robo-resource-description">{escape(resource.description)}</p>'
            if resource.description
            else ""
        )
        cards.append(
            f'<div class="robo-resource">'
            f'<span class="robo-resource-title">{escape(resource.title)}</span>'
            f"{description}"
            f'<span class="robo-resource-host">{escape(_resource_host(resource.url))}</span>'
            f'<a class="robo-resource-link" href="{url}" target="_blank" '
            f'rel="noopener noreferrer">Abrir recurso</a>'
            f"</div>"
        )
    return f'<div class="robo-resources">{"".join(cards)}</div>'


def resources_view(session, actor: User, settings: Settings | None = None) -> None:
    is_admin = actor.role == Role.ADMIN
    st.header("Recursos")
    st.caption(
        "Materiais escolhidos pela equipe para estudar inglês. Os links abrem "
        "em uma nova aba."
    )
    resources = list_resources(session, include_inactive=is_admin)
    visible = [item for item in resources if item.active]
    if visible:
        st.markdown(_resources_html(visible), unsafe_allow_html=True)
    else:
        st.info("Nenhum recurso publicado ainda.")
    if is_admin:
        _resources_editor(session, actor, resources)


def _resources_editor(session, actor: User, resources: Sequence[Resource]) -> None:
    hidden = [item for item in resources if not item.active]
    if hidden:
        st.caption(f"{len(hidden)} recurso(s) desativado(s), visíveis só para você.")
    with st.expander("Editar recursos"):
        st.caption(
            "Edite as células, arraste para reordenar pela coluna Ordem e use a "
            "última linha para adicionar. Links precisam começar com https://."
        )
        rows = [
            {
                "Ordem": item.position,
                "Título": item.title,
                "Link": item.url,
                "Descrição": item.description,
                "Ativo": item.active,
            }
            for item in resources
        ]
        edited = st.data_editor(
            rows,
            key="resources_editor",
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            column_config={
                "Ordem": st.column_config.NumberColumn(min_value=1, step=1, width="small"),
                "Título": st.column_config.TextColumn(required=True),
                "Link": st.column_config.LinkColumn(required=True),
                "Descrição": st.column_config.TextColumn(width="large"),
                "Ativo": st.column_config.CheckboxColumn(default=True, width="small"),
            },
        )
        if st.button("Salvar recursos", type="primary", key="save_resources"):
            entries = [
                {
                    "title": row.get("Título"),
                    "url": row.get("Link"),
                    "description": row.get("Descrição"),
                    "active": row.get("Ativo", True),
                }
                for row in sorted(
                    edited, key=lambda item: (item.get("Ordem") or 9999)
                )
                if (row.get("Título") or row.get("Link"))
            ]
            try:
                replace_resources(session, actor=actor, entries=entries)
                session.commit()
            except Exception as error:
                session.rollback()
                show_operation_error("replace_resources", error)
            else:
                st.success("Recursos atualizados.")
                st.rerun()


def student_dashboard(session, actor: User) -> None:
    first_name = actor.display_name.strip().split()[0]
    st.header(f"Olá, {first_name}")
    board = leaderboard_rows(session)
    own = next((row for row in board if row["student_id"] == actor.id), None)
    pending = int(
        session.scalar(
            select(func.count(Submission.id)).where(
                Submission.student_id == actor.id,
                Submission.status.in_(
                    [SubmissionStatus.PROCESSING, SubmissionStatus.NEEDS_REVIEW]
                ),
            )
        )
        or 0
    )
    goal = get_goal_configuration(session).weekly_lesson_goal
    done_this_week = weekly_lesson_count(session, actor.id)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Pontuação", student_total(session, actor.id))
    col2.metric("Posição", f"#{own['position']}" if own else "—")
    col3.metric("Meta da semana", f"{done_this_week} de {goal}")
    col4.metric("Pendências", pending)
    st.progress(
        min(1.0, done_this_week / goal) if goal else 0.0,
        text=(
            "Meta semanal cumprida. 🎉"
            if done_this_week >= goal
            else f"Faltam {goal - done_this_week} lição(ões) para a meta da semana."
        ),
    )
    _next_rival_panel(session, actor)
    others = [
        row
        for row in pending_group_progress(session, actor.id)
        if row["code"] != LESSON_ACTIVITY_CODE
    ]
    for row in others:
        st.caption(
            f"{row['activity']}: {row['unused']} de {row['threshold']} "
            "unidades para o próximo grupo."
        )
    st.caption(
        "O login identifica quem enviou, mas um print sem nome não prova de forma absoluta quem realizou a atividade."
    )

    st.link_button("Novo envio", "submit", icon=":material/add:")
    st.subheader("Atividades recentes")
    recent = list_submissions(session, actor=actor)[:3]
    _render_submission_cards(session, actor, recent, settings=None, compact=True)


def submission_form(session, actor: User, settings: Settings) -> None:
    st.header("Enviar atividade")
    activities = list(
        session.scalars(
            select(Activity)
            .where(Activity.active.is_(True), Activity.archived_at.is_(None))
            .order_by(Activity.name)
        ).all()
    )
    if not activities:
        st.info("Não há atividades disponíveis.")
        return
    activity_id = st.selectbox(
        "Atividade",
        [activity.id for activity in activities],
        format_func=lambda value: next(a.name for a in activities if a.id == value),
    )
    activity = next(item for item in activities if item.id == activity_id)
    requirements = []
    if activity.requires_images:
        requirements.append("comprovação")
    if activity.requires_summary:
        requirements.append(
            f"resumo/anotações ({activity.summary_min_chars}+ caracteres)"
        )
    if activity.requires_title_or_url:
        requirements.append("título ou URL")
    st.caption(
        "Campos exigidos: " + (", ".join(requirements) or "nenhum campo adicional")
    )

    with st.form("submission_form", clear_on_submit=True):
        title = st.text_input("Título (quando aplicável)", max_chars=500)
        url = st.text_input("URL (quando aplicável)", max_chars=2048)
        summary = st.text_area("Resumo ou anotações", height=180)
        allowed_types = (
            ["jpg", "jpeg", "png", "webp"]
            if activity.code == "duolingo_beconfident"
            else ["jpg", "jpeg", "png", "webp", "pdf", "docx", "txt"]
        )
        files = st.file_uploader(
            "Comprovação",
            type=allowed_types,
            accept_multiple_files=True,
            max_upload_size=max(1, settings.max_upload_bytes // (1024 * 1024)),
            help=f"Até {settings.max_upload_bytes // (1024 * 1024)} MB por arquivo.",
        )
        submitted = st.form_submit_button("Enviar e analisar", type="primary")
    if not submitted:
        return
    files = list(files or [])
    if len(files) > settings.max_upload_files:
        st.error(f"Envie no máximo {settings.max_upload_files} arquivos por submissão.")
        return
    total_upload_bytes = sum(int(file.size) for file in files)
    if total_upload_bytes > settings.max_upload_total_bytes:
        total_limit_mb = settings.max_upload_total_bytes // (1024 * 1024)
        st.error(f"O conjunto de arquivos não pode ultrapassar {total_limit_mb} MB.")
        return
    payloads = [UploadPayload(file.name, file.getvalue()) for file in files]
    with st.spinner("Validando arquivo e executando OCR local…"):
        try:
            image_extensions = {".jpg", ".jpeg", ".png", ".webp"}
            engine = (
                cached_ocr_engine()
                if any(
                    Path(file.name).suffix.casefold() in image_extensions
                    for file in files
                )
                else None
            )
            result = submit_evidence(
                session,
                gateway=_storage_gateway(settings),
                actor=actor,
                activity_id=activity.id,
                uploads=payloads,
                settings=settings,
                ocr_engine=engine,
                title=title,
                url=url,
                summary=summary,
            )
            session.commit()
            persist_committed_changes(session, settings)
        except Exception as error:
            session.rollback()
            show_operation_error("submission_processing", error)
            return
    if result.status in {
        SubmissionStatus.APPROVED_AUTO,
        SubmissionStatus.APPROVED_MANUAL,
    }:
        st.success(
            f"Aprovada. Unidades: {result.recognized_units}; pontos gerados agora: {result.points_created}."
        )
    elif result.status == SubmissionStatus.REJECTED:
        st.error(f"Rejeitada: {result.reason}")
    else:
        st.warning("Recebida e encaminhada para revisão administrativa.")
    st.caption(f"Confiança: {result.confidence:.0%} · ID {result.submission_id}")


STATUS_VISUAL = {
    SubmissionStatus.PROCESSING: ("⏳", "Processando", "processing"),
    SubmissionStatus.APPROVED_AUTO: ("✅", "Aprovado automaticamente", "approved"),
    SubmissionStatus.NEEDS_REVIEW: ("👀", "Aguardando revisão", "review"),
    SubmissionStatus.APPROVED_MANUAL: ("✅", "Aprovado manualmente", "approved"),
    SubmissionStatus.REJECTED: ("❌", "Rejeitado", "rejected"),
    SubmissionStatus.CANCELLED: ("⊘", "Cancelado", "cancelled"),
}


def _status_badge(status: SubmissionStatus) -> None:
    icon, label, css_class = STATUS_VISUAL[status]
    st.markdown(
        f'<span class="status-badge status-{css_class}">{icon} {label}</span>',
        unsafe_allow_html=True,
    )


def _submission_filters(
    session,
    *,
    prefix: str,
    include_students: bool = False,
    include_status: bool = True,
):
    students = list(
        session.scalars(
            select(User).where(User.role == Role.STUDENT).order_by(User.display_name)
        ).all()
    )
    activities = list(session.scalars(select(Activity).order_by(Activity.name)).all())
    columns = st.columns(2 + int(include_students) + int(include_status))
    query = st.query_params
    column_index = 0
    student_id = None
    if include_students:
        student_options = [None, *[student.id for student in students]]
        requested_student = query.get(f"{prefix}_student") or None
        student_id = columns[column_index].selectbox(
            "Aluno",
            student_options,
            index=student_options.index(requested_student)
            if requested_student in student_options
            else 0,
            format_func=lambda value: (
                "Todos os alunos"
                if value is None
                else next(item.display_name for item in students if item.id == value)
            ),
            key=f"{prefix}_student",
        )
        column_index += 1
    status_value = None
    if include_status:
        status_options = [None, *list(SubmissionStatus)]
        requested_status = query.get(f"{prefix}_status") or None
        requested_status_value = (
            SubmissionStatus(requested_status)
            if requested_status in {status.value for status in SubmissionStatus}
            else None
        )
        status_value = columns[column_index].selectbox(
            "Estado",
            status_options,
            index=status_options.index(requested_status_value),
            format_func=lambda value: (
                "Todos os estados" if value is None else STATUS_VISUAL[value][1]
            ),
            key=f"{prefix}_status",
        )
        column_index += 1
    activity_options = [None, *[activity.id for activity in activities]]
    requested_activity = query.get(f"{prefix}_activity") or None
    activity_id = columns[column_index].selectbox(
        "Atividade",
        activity_options,
        index=activity_options.index(requested_activity)
        if requested_activity in activity_options
        else 0,
        format_func=lambda value: (
            "Todas as atividades"
            if value is None
            else next(item.name for item in activities if item.id == value)
        ),
        key=f"{prefix}_activity",
    )
    period_options = [0, 7, 30, 90]
    try:
        requested_period = int(query.get(f"{prefix}_period", "0"))
    except (TypeError, ValueError):
        requested_period = 0
    period = columns[column_index + 1].selectbox(
        "Período",
        period_options,
        index=period_options.index(requested_period)
        if requested_period in period_options
        else 0,
        format_func=lambda value: (
            "Todo o período" if value == 0 else f"Últimos {value} dias"
        ),
        key=f"{prefix}_period",
    )
    if include_students:
        query[f"{prefix}_student"] = student_id or ""
    if include_status:
        query[f"{prefix}_status"] = status_value.value if status_value else ""
    query[f"{prefix}_activity"] = activity_id or ""
    query[f"{prefix}_period"] = str(period)
    start = utcnow() - timedelta(days=period) if period else None
    return student_id, status_value, activity_id, start


def _render_submission_files(
    session,
    actor: User,
    submission: Submission,
    settings: Settings | None,
) -> None:
    if settings is None:
        file_count = len(submission.files) or len(submission.images)
        st.caption(f"📎 {file_count} arquivo(s) enviado(s)")
        return
    if not submission.files:
        st.caption("Nenhum arquivo anexado.")
        return
    for index, stored_file in enumerate(submission.files, start=1):
        icon = {"image": "🖼️", "pdf": "📕", "docx": "📘", "txt": "📄"}.get(
            stored_file.file_kind, "📎"
        )
        label = stored_file.filename or f"Arquivo {index}"
        try:
            authorized_file, dados = get_submission_file_for_user(
                session,
                actor=actor,
                file_id=stored_file.id,
                settings=settings,
                gateway=_storage_gateway(settings),
            )
        except (LookupError, StorageError):
            st.warning(f"{icon} {label} não está disponível no armazenamento.")
            continue
        except AuthorizationError:
            st.warning(f"{icon} {label}: sem permissão para abrir.")
            continue
        if authorized_file.file_kind == "image":
            st.image(dados, caption=label, width="stretch")
        st.download_button(
            f"{icon} Abrir/baixar {label}",
            data=dados,
            file_name=label,
            mime=authorized_file.content_type,
            key=f"download_{submission.id}_{stored_file.id}",
        )


def _render_submission_timeline(submission: Submission) -> None:
    st.markdown("**Evolução do processamento**")
    st.write(f"✅ Enviado · {submission.received_at:%d/%m/%Y %H:%M}")
    if submission.processed_at:
        st.write(f"✅ Processado · {submission.processed_at:%d/%m/%Y %H:%M}")
    else:
        st.write("⏳ Processamento em andamento")
    if submission.status == SubmissionStatus.NEEDS_REVIEW:
        st.write("👀 Aguardando decisão administrativa")
    elif submission.decided_at:
        icon, label, _ = STATUS_VISUAL[submission.status]
        st.write(f"{icon} {label} · {submission.decided_at:%d/%m/%Y %H:%M}")


def _render_duplicate_matches(
    session,
    submission: Submission,
    matches: list[DuplicateMatch],
    settings: Settings,
) -> None:
    if not matches:
        return
    st.warning(f"{len(matches)} possível(is) duplicidade(s) encontrada(s).")
    st.markdown("**Comparação de evidências**")
    for index, match in enumerate(matches, start=1):
        current_image = session.get(SubmissionFile, match.file_id)
        matched_image = session.get(SubmissionFile, match.matched_file_id)
        if current_image is None or matched_image is None:
            st.warning(f"Correspondência {index}: imagem não disponível.")
            continue
        matched_submission = session.get(Submission, matched_image.submission_id)
        kind_label = (
            "Duplicata exata" if match.kind.value == "exact" else "Imagem semelhante"
        )
        similarity = (
            "conteúdo idêntico"
            if match.distance is None or match.distance == 0
            else f"distância perceptual {match.distance}"
        )
        with st.container(border=True, key=f"duplicate_comparison_{match.id}"):
            st.write(f"**{kind_label}** · {similarity}")
            if matched_submission is not None:
                st.caption(
                    "Correspondência com "
                    f"{matched_submission.student.display_name} · "
                    f"{matched_submission.activity.name} · "
                    f"{matched_submission.received_at:%d/%m/%Y %H:%M}"
                )
            current_column, duplicate_column = st.columns(2)
            current_path = settings.upload_dir / current_image.storage_key
            duplicate_path = settings.upload_dir / matched_image.storage_key
            with current_column:
                st.caption("Envio atual")
                if current_path.is_file():
                    st.image(str(current_path), width="stretch")
                else:
                    st.warning("Imagem atual indisponível.")
            with duplicate_column:
                st.caption("Possível duplicata")
                if duplicate_path.is_file():
                    st.image(str(duplicate_path), width="stretch")
                else:
                    st.warning("Imagem correspondente indisponível.")


def _render_submission_cards(
    session,
    actor: User,
    submissions: list[Submission],
    settings: Settings | None,
    *,
    compact: bool = False,
    admin_mode: bool = False,
) -> None:
    if not submissions:
        st.info("Nenhum envio encontrado para os filtros selecionados.")
        return
    for submission in submissions:
        icon, _, _ = STATUS_VISUAL[submission.status]
        heading = (
            f"{icon} {submission.activity.name} · "
            f"{submission.received_at:%d/%m/%Y %H:%M}"
        )
        if admin_mode:
            heading = f"{submission.student.display_name} · {heading}"
        with st.expander(heading, expanded=False):
            _status_badge(submission.status)
            possible_points = int(
                submission.rule_snapshot_json.get("points", submission.activity.points)
            )
            metrics = st.columns(3)
            metrics[0].metric("Pontos possíveis", possible_points)
            metrics[1].metric("Pontos concedidos", submission.points_awarded)
            metrics[2].metric("Unidades", submission.recognized_units)
            if submission.admin_reason:
                st.info(f"Justificativa: {submission.admin_reason}")
            if compact:
                _render_submission_files(session, actor, submission, None)
                continue
            if admin_mode:
                with st.container(key=f"review_evidence_{submission.id}"):
                    _render_submission_files(session, actor, submission, settings)
            else:
                _render_submission_files(session, actor, submission, settings)
            _render_submission_timeline(submission)
            if submission.title:
                st.write(f"**Título:** {submission.title}")
            if submission.url:
                st.write(f"**URL:** {submission.url}")
            if submission.summary:
                st.text_area(
                    "Resumo/anotações",
                    submission.summary,
                    height=140,
                    disabled=True,
                    key=f"summary_{submission.id}",
                )
            if admin_mode:
                st.text_area(
                    "Texto extraído",
                    submission.ocr_text or "(vazio)",
                    height=160,
                    disabled=True,
                    key=f"ocr_{submission.id}",
                )
                with st.expander("Verificações e auditoria"):
                    for check in submission.checks:
                        symbol = {"pass": "✅", "review": "⚠️", "fail": "❌"}[
                            check.outcome.value
                        ]
                        st.write(f"{symbol} **{check.rule_name}** — {check.message}")
                    audit_logs = list(
                        session.scalars(
                            select(AuditLog)
                            .where(
                                AuditLog.entity_type == "submission",
                                AuditLog.entity_id == submission.id,
                            )
                            .order_by(AuditLog.created_at)
                        ).all()
                    )
                    for log in audit_logs:
                        st.write(
                            f"🕘 {log.created_at:%d/%m/%Y %H:%M} · "
                            f"{log.action} · {log.reason or 'sem observação'}"
                        )
                image_ids = [image.id for image in submission.images]
                matches = (
                    list(
                        session.scalars(
                            select(DuplicateMatch).where(
                                DuplicateMatch.image_id.in_(image_ids)
                            )
                        ).all()
                    )
                    if image_ids
                    else []
                )
                if settings is not None:
                    _render_duplicate_matches(session, submission, matches, settings)
                if submission.status == SubmissionStatus.NEEDS_REVIEW:
                    with st.form(f"review_{submission.id}"):
                        units = st.number_input(
                            "Unidades reconhecidas",
                            min_value=0,
                            max_value=max(1, len(submission.images)),
                            value=max(1, submission.recognized_units),
                            disabled=submission.activity.code != "duolingo_beconfident",
                        )
                        approve = st.form_submit_button("Aprovar", type="primary")
                        reject = st.form_submit_button("Rejeitar")
                    if approve or reject:
                        try:
                            result = review_submission(
                                session,
                                actor=actor,
                                submission_id=submission.id,
                                approve=approve,
                                recognized_units=int(units) if approve else None,
                            )
                            session.commit()
                            if settings is not None:
                                persist_committed_changes(session, settings)
                        except Exception as error:
                            session.rollback()
                            show_operation_error("review_submission", error)
                        else:
                            st.success(
                                "Decisão salva. "
                                f"Pontos gerados: {result.points_created}."
                            )


def student_history(session, actor: User, settings: Settings) -> None:
    st.header("Histórico")
    _, status_value, activity_id, start = _submission_filters(
        session, prefix="student_history"
    )
    submissions = list_submissions(
        session,
        actor=actor,
        status=status_value,
        activity_id=activity_id,
        start=start,
    )
    _render_submission_cards(session, actor, submissions, settings)


def review_queue_view(session, actor: User, settings: Settings) -> None:
    st.header("Envios")
    review_view = st.selectbox(
        "Revisão",
        ["needs_review", "approved", "rejected", "all"],
        format_func={
            "needs_review": "Pendentes de revisão",
            "approved": "Aprovados",
            "rejected": "Rejeitados",
            "all": "Todos os envios",
        }.get,
        key="admin_review_view",
    )
    student_id, _, activity_id, start = _submission_filters(
        session,
        prefix="admin_submissions",
        include_students=True,
        include_status=False,
    )
    submissions = list_submissions(
        session,
        actor=actor,
        student_id=student_id,
        activity_id=activity_id,
        start=start,
    )
    allowed_statuses = _review_statuses(review_view)
    submissions = [item for item in submissions if item.status in allowed_statuses]
    pending = sum(item.status == SubmissionStatus.NEEDS_REVIEW for item in submissions)
    if pending:
        st.warning(f"{pending} envio(s) aguardando revisão nos filtros atuais.")
    _render_submission_cards(
        session,
        actor,
        submissions,
        settings,
        admin_mode=True,
    )


def _review_statuses(review_view: str) -> set[SubmissionStatus]:
    return {
        "needs_review": {SubmissionStatus.NEEDS_REVIEW},
        "approved": {
            SubmissionStatus.APPROVED_AUTO,
            SubmissionStatus.APPROVED_MANUAL,
        },
        "rejected": {SubmissionStatus.REJECTED},
        "all": set(SubmissionStatus),
    }[review_view]


def _student_account_counts(users: Sequence[User]) -> tuple[int, int, int]:
    students = [user for user in users if user.role == Role.STUDENT]
    return (
        sum(user.active and user.archived_at is None for user in students),
        sum(not user.active and user.archived_at is None for user in students),
        sum(user.archived_at is not None for user in students),
    )


def users_view(session, actor: User, settings: Settings | None = None) -> None:
    st.header("Alunos e administradores")
    users = list(session.scalars(select(User).order_by(User.display_name)).all())
    active_count, inactive_count, archived_count = _student_account_counts(users)
    summary = st.columns(3)
    summary[0].metric("Alunos ativos", active_count)
    summary[1].metric("Alunos inativos", inactive_count)
    summary[2].metric("Alunos arquivados", archived_count)
    st.dataframe(
        [
            {
                "Nome": user.display_name,
                "Usuário": user.username,
                "Papel": "Administrador" if user.role == Role.ADMIN else "Aluno",
                "Estado": "Arquivado"
                if user.archived_at
                else "Ativo"
                if user.active
                else "Inativo",
                "Último acesso": user.last_login_at,
            }
            for user in users
        ],
        width="stretch",
        hide_index=True,
    )
    generated = st.session_state.pop("generated_temp_password", None)
    if generated:
        st.success(
            "Senha temporária gerada. Copie-a agora; ela não será exibida novamente."
        )
        st.code(generated, language=None)
    with st.expander("Criar nova conta"):
        with st.form("new_user_form", clear_on_submit=True):
            new_name = st.text_input("Nome")
            new_username = st.text_input(
                "Usuário",
                help="Letras, números, ponto, hífen ou sublinhado. Sem espaços.",
            )
            new_role = st.selectbox("Papel", [Role.STUDENT.value, Role.ADMIN.value])
            create_submitted = st.form_submit_button("Criar e gerar senha temporária")
        if create_submitted:
            try:
                _, temporary_password = create_user_account(
                    session,
                    actor=actor,
                    contas=contas_de(settings),
                    username=new_username,
                    display_name=new_name,
                    role=new_role,
                )
                session.commit()
            except Exception as error:
                session.rollback()
                show_operation_error("create_user", error)
            else:
                st.session_state["generated_temp_password"] = temporary_password
                st.rerun()

    editable_users = [user for user in users if user.archived_at is None]
    if not editable_users:
        return
    selected = st.selectbox(
        "Conta para administrar",
        [user.id for user in editable_users],
        format_func=lambda value: next(
            user.display_name for user in editable_users if user.id == value
        ),
    )
    current = next(user for user in editable_users if user.id == selected)
    with st.form("user_form"):
        name = st.text_input("Nome", value=current.display_name)
        username = st.text_input("Usuário", value=current.username)
        role = st.selectbox(
            "Papel",
            [Role.STUDENT.value, Role.ADMIN.value],
            index=1 if current.role == Role.ADMIN else 0,
        )
        active = st.checkbox("Conta ativa", value=current.active)
        submitted = st.form_submit_button("Salvar alterações", type="primary")
    if submitted:
        try:
            save_user(
                session,
                actor=actor,
                contas=contas_de(settings),
                username=username,
                display_name=name,
                role=role,
                active=active,
                user_id=current.id,
            )
            session.commit()
            if settings is not None:
                persist_committed_changes(session, settings)
            st.success("Usuário salvo.")
        except Exception as error:
            session.rollback()
            show_operation_error("save_user", error)

    with st.expander("Redefinir senha"):
        with st.form("reset_password_form"):
            reset_confirm = st.checkbox("Confirmo a redefinição da senha")
            reset_submitted = st.form_submit_button("Gerar nova senha temporária")
        if reset_submitted:
            if not reset_confirm:
                st.error("Confirme a redefinição.")
            else:
                try:
                    temporary_password = reset_user_password(
                        session,
                        actor=actor,
                        contas=contas_de(settings),
                        user_id=current.id,
                    )
                    session.commit()
                except Exception as error:
                    session.rollback()
                    show_operation_error("reset_password", error)
                else:
                    st.session_state["generated_temp_password"] = temporary_password
                    st.rerun()

    with st.expander("Excluir ou arquivar conta"):
        st.warning(
            "Contas com histórico serão arquivadas. Contas nunca utilizadas "
            "podem ser removidas permanentemente."
        )
        with st.form("delete_user_form"):
            confirmation = st.text_input(
                f'Digite "{current.username}" para confirmar'
            )
            delete_submitted = st.form_submit_button("Confirmar exclusão")
        if delete_submitted:
            if confirmation.strip().lower() != current.username.lower():
                st.error("A confirmação não corresponde ao usuário da conta.")
            else:
                try:
                    result = archive_or_delete_user(
                        session,
                        actor=actor,
                        contas=contas_de(settings),
                        user_id=current.id,
                    )
                    session.commit()
                except Exception as error:
                    session.rollback()
                    show_operation_error("delete_user", error)
                else:
                    st.success(
                        "Conta arquivada."
                        if result == "archived"
                        else "Conta excluída."
                    )
                    st.rerun()


def _confirm_activity_delete(
    session, actor: User, activity: Activity, settings: Settings | None
) -> None:
    """Pergunta antes de excluir, dizendo se o efeito será arquivar ou remover."""

    references = count_activity_references(session, activity.id)
    if references:
        st.warning(
            f"**{activity.name}** possui {references} registro(s) no histórico. "
            "Ela será arquivada e sairá do catálogo, mas as submissões e os "
            "lançamentos já pontuados permanecem intactos. Confirmar?"
        )
    else:
        st.warning(
            f"**{activity.name}** nunca foi usada e será removida "
            "permanentemente. Esta ação não pode ser desfeita. Confirmar?"
        )
    actions = st.columns(2)
    if actions[0].button(
        "Sim, arquivar" if references else "Sim, excluir",
        key=f"confirm_delete_activity_{activity.id}",
        type="primary",
        width="stretch",
    ):
        try:
            result = archive_or_delete_activity(
                session,
                actor=actor,
                activity_id=activity.id,
            )
            session.commit()
            if settings is not None:
                persist_committed_changes(session, settings)
        except Exception as error:
            session.rollback()
            show_operation_error("delete_activity", error)
        else:
            st.session_state.pop("pending_activity_delete", None)
            st.success(
                "Atividade arquivada." if result == "archived" else "Atividade excluída."
            )
            st.rerun()
    if actions[1].button(
        "Cancelar",
        key=f"cancel_delete_activity_{activity.id}",
        width="stretch",
    ):
        st.session_state.pop("pending_activity_delete", None)
        st.rerun()


def catalog_view(session, actor: User, settings: Settings | None = None) -> None:
    st.header("Catálogo")
    activities = list(session.scalars(select(Activity).order_by(Activity.name)).all())
    st.caption(
        "Use a checkbox para ativar ou desativar. Excluir remove atividades sem "
        "uso e arquiva as que possuem histórico."
    )
    header = st.columns([4, 2, 1, 1, 1, 1.2])
    for column, label in zip(
        header,
        ["Atividade", "Código", "Pontos", "Unidades", "Ativa", "Ações"],
        strict=True,
    ):
        column.markdown(f"**{label}**")
    for item in activities:
        row = st.columns([4, 2, 1, 1, 1, 1.2], vertical_alignment="center")
        row[0].write(item.name)
        row[1].code(item.code, language=None)
        row[2].write(item.points)
        row[3].write(item.unit_threshold)
        checked = row[4].checkbox(
            f"Ativar {item.name}",
            value=item.active,
            key=f"activity_active_{item.id}",
            disabled=item.archived_at is not None,
            label_visibility="collapsed",
        )
        if checked != item.active:
            try:
                set_activity_active(
                    session,
                    actor=actor,
                    activity_id=item.id,
                    active=checked,
                )
                session.commit()
                if settings is not None:
                    persist_committed_changes(session, settings)
            except Exception as error:
                session.rollback()
                show_operation_error("set_activity_active", error)
            else:
                st.rerun()
        protected = item.code == "duolingo_beconfident"
        if row[5].button(
            "Excluir",
            key=f"delete_activity_{item.id}",
            disabled=protected or item.archived_at is not None,
            width="stretch",
            help="A atividade principal não pode ser excluída." if protected else None,
        ):
            st.session_state["pending_activity_delete"] = item.id
            st.rerun()
        if st.session_state.get("pending_activity_delete") == item.id:
            _confirm_activity_delete(session, actor, item, settings)
    with st.expander("Criar atividade"):
        with st.form("new_activity_form", clear_on_submit=True):
            new_code = st.text_input("Código interno")
            new_name = st.text_input("Nome da atividade")
            new_points = st.number_input("Pontos", min_value=1, value=10)
            new_threshold = st.number_input("Unidades por prêmio", min_value=1, value=1)
            new_requires_proof = st.checkbox("Exige comprovação", value=True)
            new_requires_summary = st.checkbox("Exige resumo/anotações")
            new_requires_title = st.checkbox("Exige título ou URL")
            new_review = st.checkbox("Exige revisão humana", value=True)
            create_submitted = st.form_submit_button("Criar atividade")
        if create_submitted:
            try:
                create_activity(
                    session,
                    actor=actor,
                    code=new_code,
                    name=new_name,
                    points=int(new_points),
                    unit_threshold=int(new_threshold),
                    requires_images=new_requires_proof,
                    requires_summary=new_requires_summary,
                    requires_title_or_url=new_requires_title,
                    content_review_required=new_review,
                )
                session.commit()
            except Exception as error:
                session.rollback()
                show_operation_error("create_activity", error)
            else:
                st.success("Atividade criada.")
                st.rerun()

    editable = [item for item in activities if item.archived_at is None]
    if not editable:
        return
    selected = st.selectbox(
        "Atividade para editar",
        [item.id for item in editable],
        format_func=lambda value: next(
            item.name for item in editable if item.id == value
        ),
    )
    activity = next(item for item in editable if item.id == selected)
    fixed_lesson_policy = activity.code == "duolingo_beconfident"
    with st.form("activity_form"):
        name = st.text_input("Nome", value=activity.name)
        points = st.number_input(
            "Pontos para futuras aprovações",
            min_value=1,
            max_value=1000,
            value=5 if fixed_lesson_policy else activity.points,
            disabled=fixed_lesson_policy,
            help="Política fixa: 5 pontos." if fixed_lesson_policy else None,
        )
        minimum = st.number_input(
            "Mínimo de caracteres",
            min_value=0,
            max_value=10000,
            value=activity.summary_min_chars,
        )
        threshold = st.number_input(
            "Unidades por premiação",
            min_value=1,
            max_value=100,
            value=5 if fixed_lesson_policy else activity.unit_threshold,
            disabled=fixed_lesson_policy,
            help="Política fixa: um grupo contém 5 lições."
            if fixed_lesson_policy
            else None,
        )
        requires_images = st.checkbox(
            "Exige comprovação", value=activity.requires_images
        )
        requires_summary = st.checkbox(
            "Exige resumo/anotações", value=activity.requires_summary
        )
        requires_title = st.checkbox(
            "Exige título ou URL", value=activity.requires_title_or_url
        )
        content_review = st.checkbox(
            "Exige revisão humana de conteúdo", value=activity.content_review_required
        )
        auto_approvable = st.checkbox(
            "Pode ser autoaprovada", value=activity.auto_approvable
        )
        submitted = st.form_submit_button("Salvar", type="primary")
    if submitted:
        try:
            save_activity_changes(
                session,
                actor=actor,
                activity_id=activity.id,
                name=name,
                points=int(points),
                active=activity.active,
                summary_min_chars=int(minimum),
                unit_threshold=int(threshold),
                requires_images=requires_images,
                requires_summary=requires_summary,
                requires_title_or_url=requires_title,
                content_review_required=content_review,
                auto_approvable=auto_approvable,
            )
            session.commit()
            if settings is not None:
                persist_committed_changes(session, settings)
            st.success(
                "Catálogo atualizado. Transações históricas permaneceram intactas."
            )
        except Exception as error:
            session.rollback()
            show_operation_error("save_activity", error)


def reminders_view(session, actor: User, settings: Settings) -> None:
    st.header("Lembretes por e-mail")
    configuration = get_reminder_configuration(session)
    mode_label = (
        "Dry-run: nenhum e-mail será enviado"
        if settings.reminder_dry_run
        else "Envio SMTP real habilitado"
    )
    st.info(mode_label)
    weekdays = [
        "Segunda-feira",
        "Terça-feira",
        "Quarta-feira",
        "Quinta-feira",
        "Sexta-feira",
        "Sábado",
        "Domingo",
    ]
    with st.form("reminder_configuration_form"):
        enabled = st.checkbox("Ativar lembretes", value=configuration.enabled)
        frequency = st.selectbox(
            "Frequência",
            ["daily", "weekly"],
            index=0 if configuration.frequency == "daily" else 1,
            format_func=lambda value: "Diária" if value == "daily" else "Semanal",
        )
        weekday = st.selectbox(
            "Dia da semana",
            list(range(7)),
            index=configuration.weekday,
            format_func=lambda value: weekdays[value],
            disabled=frequency == "daily",
        )
        send_hour = st.number_input(
            "Horário (hora cheia)",
            min_value=0,
            max_value=23,
            value=configuration.send_hour,
        )
        timezone_name = st.text_input("Fuso horário", value=configuration.timezone_name)
        inactive_days = st.number_input(
            "Dias sem atividade aprovada",
            min_value=1,
            max_value=365,
            value=configuration.inactive_days,
        )
        audiences = {
            "inactive_students": "Todos os alunos inativos no período",
            "never_approved": "Alunos que nunca tiveram atividade aprovada",
            "previously_active": "Alunos que já participaram e ficaram inativos",
        }
        audience = st.selectbox(
            "Público-alvo",
            list(audiences),
            index=list(audiences).index(configuration.audience)
            if configuration.audience in audiences
            else 0,
            format_func=audiences.get,
        )
        subject = st.text_input("Assunto", value=configuration.subject_template)
        body = st.text_area(
            "Modelo da mensagem",
            value=configuration.body_template,
            height=180,
            help="Variáveis disponíveis: {name}, {username} e {email}.",
        )
        submitted = st.form_submit_button("Salvar configuração", type="primary")
    if submitted:
        try:
            save_reminder_configuration(
                session,
                actor=actor,
                enabled=enabled,
                frequency=frequency,
                weekday=int(weekday),
                send_hour=int(send_hour),
                timezone_name=timezone_name,
                inactive_days=int(inactive_days),
                subject_template=subject,
                body_template=body,
                audience=audience,
            )
            session.commit()
        except Exception as error:
            session.rollback()
            show_operation_error("save_reminders", error)
        else:
            st.success("Configuração de lembretes salva.")

    with st.expander("Pré-visualização"):
        preview_subject, preview_body = render_reminder(configuration, actor)
        st.write(f"**Assunto:** {preview_subject}")
        st.text(preview_body)
        if st.button("Gerar envio de teste para mim"):
            try:
                attempt = send_test_reminder(session, actor=actor, settings=settings)
                session.commit()
            except Exception as error:
                session.rollback()
                show_operation_error("test_reminder", error)
            else:
                st.success(f"Teste registrado com estado: {attempt.status.value}.")

    attempts = list(
        session.scalars(
            select(EmailAttempt).order_by(EmailAttempt.created_at.desc()).limit(50)
        ).all()
    )
    st.subheader("Últimas tentativas")
    if not attempts:
        st.info("Nenhuma tentativa registrada.")
    else:
        st.dataframe(
            [
                {
                    "Data": attempt.created_at,
                    "Destinatário": attempt.recipient_email,
                    "Estado": attempt.status.value,
                    "Dry-run": attempt.dry_run,
                    "Tentativas": attempt.attempt_count,
                }
                for attempt in attempts
            ],
            width="stretch",
            hide_index=True,
        )


def ledger_view(session, actor: User, settings: Settings | None = None) -> None:
    st.header("Ledger e sincronização")
    if settings is not None and settings.google_sheets_auto_sync:
        sheet_url = (
            "https://docs.google.com/spreadsheets/d/"
            f"{settings.google_sheets_spreadsheet_id}/edit"
        )
        status, sync_action, open_action = st.columns([4, 1, 1])
        status.success(
            "Espelho automático ativo: alterações confirmadas são enviadas "
            "ao Google Sheets."
        )
        if sync_action.button("Sincronizar agora", width="stretch"):
            persist_committed_changes(session, settings)
        open_action.link_button("Abrir planilha", sheet_url, width="stretch")
    else:
        st.info(
            "A sincronização automática com Google Sheets está desativada. "
            "Os downloads locais continuam disponíveis."
        )
    left, right = st.columns(2)
    start = left.date_input(
        "Início", value=date.today().replace(day=1), key="ledger_start"
    )
    end = right.date_input("Fim", value=date.today(), key="ledger_end")
    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    rows = admin_ledger_rows(session, actor=actor, start=start_dt, end=end_dt)
    st.dataframe(rows, width="stretch", hide_index=True)
    board = leaderboard_rows(session, start=start, end=end)
    c1, c2 = st.columns(2)
    c1.download_button(
        "Ledger XLSX",
        ledger_to_xlsx(rows),
        "ledger.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    c2.download_button(
        "Leaderboard XLSX",
        leaderboard_to_xlsx(board),
        "leaderboard.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    leaderboard_view(session, key_prefix="admin_ledger")
    st.subheader("Histórico individual")
    students = list(
        session.scalars(
            select(User).where(User.role == Role.STUDENT).order_by(User.display_name)
        ).all()
    )
    if students:
        selected_student = st.selectbox(
            "Aluno",
            [student.id for student in students],
            format_func=lambda value: next(
                student.display_name for student in students if student.id == value
            ),
            key="admin_history_student",
        )
        individual_ledger = admin_ledger_rows(
            session,
            actor=actor,
            student_id=selected_student,
            start=start_dt,
            end=end_dt,
        )
        st.dataframe(individual_ledger, width="stretch", hide_index=True)
        submissions = admin_student_submissions(
            session, actor=actor, student_id=selected_student
        )
        st.caption(
            f"{len(submissions)} submissão(ões) no histórico; detalhes em Envios."
        )

    st.subheader("Ajuste auditado de pontos")
    if students:
        with st.form("points_adjustment_form"):
            adjustment_student = st.selectbox(
                "Aluno do ajuste",
                [student.id for student in students],
                format_func=lambda value: next(
                    student.display_name for student in students if student.id == value
                ),
            )
            adjustment_points = st.number_input(
                "Pontos (use valor negativo para remover)",
                min_value=-10000,
                max_value=10000,
                value=0,
            )
            adjustment_confirm = st.checkbox("Confirmo o ajuste no ledger")
            adjustment_submit = st.form_submit_button("Registrar ajuste")
        if adjustment_submit:
            if not adjustment_confirm:
                st.error("Confirme o ajuste.")
            else:
                try:
                    create_points_adjustment(
                        session,
                        actor=actor,
                        student_id=adjustment_student,
                        points=int(adjustment_points),
                    )
                    session.commit()
                    if settings is not None:
                        persist_committed_changes(session, settings)
                except Exception as error:
                    session.rollback()
                    show_operation_error("points_adjustment", error)
                else:
                    st.success("Ajuste registrado como nova transação imutável.")


def admin_dashboard(session, actor: User | None = None) -> None:
    st.header("Visão geral")
    pending = (
        session.scalar(
            select(func.count(Submission.id)).where(
                Submission.status == SubmissionStatus.NEEDS_REVIEW
            )
        )
        or 0
    )
    configuration = get_goal_configuration(session)
    col1, col2 = st.columns(2)
    col1.metric("Fila de revisão", pending)
    col2.metric("Atividades recebidas na semana", weekly_submission_count(session))
    if actor is not None:
        _weekly_goal_form(session, actor, configuration)
    leaderboard_view(session, key_prefix="admin_dashboard")


def _weekly_goal_form(session, actor: User, configuration) -> None:
    goal = configuration.weekly_lesson_goal
    reached, total_students = students_meeting_weekly_goal(session, goal)
    with st.expander(f"Meta semanal: {goal} lições · {reached} de {total_students} na meta"):
        st.caption(
            "A meta orienta os alunos e aparece no painel deles. Ela não altera "
            "a pontuação: o ledger continua vindo apenas das aprovações."
        )
        with st.form("weekly_goal_form"):
            goal = st.number_input(
                "Lições por semana",
                min_value=1,
                max_value=200,
                value=configuration.weekly_lesson_goal,
                step=1,
            )
            submitted = st.form_submit_button("Salvar meta", type="primary")
        if submitted:
            try:
                save_goal_configuration(
                    session,
                    actor=actor,
                    weekly_lesson_goal=int(goal),
                )
                session.commit()
            except Exception as error:
                session.rollback()
                show_operation_error("save_goal_configuration", error)
            else:
                st.success("Meta semanal atualizada.")
                st.rerun()


def _browser_command_id() -> str | None:
    command = st.session_state.get(COMMAND_KEY)
    if not isinstance(command, dict):
        return None
    value = command.get("id")
    return value if isinstance(value, str) else None


def _mount_persistent_browser_session(settings: Settings) -> BrowserSessionSnapshot:
    """Monta o componente que guarda o refresh token entre visitas."""

    snapshot = mount_browser_session(st)
    # A component can return its previous payload for one run while a new
    # write/clear command is still awaiting ACK. Never let that stale token
    # overwrite the newly authenticated session.
    if snapshot.token and _browser_command_id() is None:
        remember_token(st, snapshot.token)
    return snapshot


def main() -> None:
    try:
        settings, factory = runtime(_schema_fingerprint())
    except Exception as error:
        st.error(f"Configuração inválida: {error}")
        st.stop()

    browser_session = _mount_persistent_browser_session(settings)
    command_before_auth = _browser_command_id()

    # A identidade é resolvida antes de abrir a sessão de banco: é ela que diz
    # sob qual usuário as consultas vão rodar. Fazer o contrário obrigaria a
    # abrir a transação como dono do banco — que ignora RLS — e só depois
    # descobrir de quem é a vez.
    sessao = None
    if not (
        st.session_state.get(SESSAO_KEY) is None and not browser_session.ready
    ):
        sessao = _sessao_viva(settings)

    if sessao is not None:
        escopo = session_scope(factory, user_id=sessao.user_id)
    else:
        # Sem sessão só a tela de login é renderizada, e ela não consulta o
        # domínio. O papel de serviço aqui não expõe dado de ninguém.
        escopo = session_scope(factory, servico=True)

    with escopo as session:
        auth_state = authenticate(
            session,
            settings,
            browser_session_ready=browser_session.ready,
            browser_storage_available=browser_session.storage_available,
        )
        command_after_auth = _browser_command_id()
        st.session_state.pop("post_login_ready", None)
        st.session_state.pop("post_login_route", None)
        registered_routes = _registered_routes(session, settings, auth_state)
        visible_routes = _visible_routes(session, settings, auth_state)
        _run_navigation(
            registered_routes,
            visible_routes=visible_routes,
            rerun_before_render=bool(
                command_after_auth and command_after_auth != command_before_auth
            ),
        )


if __name__ == "__main__":
    main()
