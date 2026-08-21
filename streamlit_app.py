from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import logging
from pathlib import Path
from uuid import uuid4

import streamlit as st
from sqlalchemy import func, select

from english_leaderboard.authz import AuthorizationError
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
from english_leaderboard.models import (
    Activity,
    AuditLog,
    DuplicateMatch,
    EmailAttempt,
    LedgerTransaction,
    Role,
    Submission,
    SubmissionStatus,
    User,
    utcnow,
)
from english_leaderboard.ocr import create_ocr_engine
from english_leaderboard.scoring import (
    leaderboard_rows,
    ledger_rows,
    lesson_progress,
    student_total,
)
from english_leaderboard.services import (
    UploadPayload,
    admin_ledger_rows,
    admin_student_submissions,
    archive_or_delete_activity,
    archive_or_delete_user,
    create_activity,
    create_points_adjustment,
    create_user_account,
    get_submission_file_for_user,
    list_submissions,
    reset_user_password,
    resolve_oidc_user,
    review_submission,
    save_activity_changes,
    save_user,
    submit_evidence,
)
from english_leaderboard.browser_session import (
    forget_token,
    remember_token,
    render_cookie_bridge,
    request_cookie,
)
from english_leaderboard.local_auth import (
    AuthenticationError,
    change_password,
    login_with_password,
    resolve_auth_session,
    revoke_session,
)
from english_leaderboard.reminders import (
    get_reminder_configuration,
    render_reminder,
    save_reminder_configuration,
    send_test_reminder,
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
    oidc_logged_in: bool = False
    oidc_available: bool = True
    local_token: str | None = None


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


@st.cache_resource
def runtime():
    settings = Settings.from_env()
    settings.ensure_directories()
    engine = create_database_engine(settings.database_url)
    initialize_database(engine)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        seed_database(session, settings)
    return settings, factory


@st.cache_resource(show_spinner="Carregando OCR local…")
def cached_ocr_engine():
    """Uma única instância ONNX por processo Streamlit."""

    return create_ocr_engine()


def _oidc_value(name: str) -> str | None:
    try:
        value = getattr(st.user, name, None)
    except Exception:
        value = None
    if value is None:
        try:
            value = st.user.get(name)
        except Exception:
            return None
    return str(value) if value else None


def _oidc_login_state() -> bool | None:
    """Return the Streamlit login state, or ``None`` when OIDC is absent."""

    try:
        value = getattr(st.user, "is_logged_in")
    except (AttributeError, KeyError):
        try:
            value = st.user.get("is_logged_in")
        except Exception:
            return None
    except Exception:
        return None
    if value is None:
        return None
    return bool(value)


def authenticate(session, settings: Settings) -> AuthenticationState:
    """Resolve an existing session without implicitly logging anyone in."""

    if settings.demo_auth_enabled:
        demo_user_id = st.session_state.get("demo_user_id")
        if not demo_user_id:
            return AuthenticationState(actor=None)
        actor = session.get(User, str(demo_user_id))
        if actor is None or not actor.active:
            st.session_state.pop("demo_user_id", None)
            return AuthenticationState(
                actor=None,
                error="A identidade demo selecionada não está mais disponível.",
            )
        return AuthenticationState(actor=actor)

    if settings.local_auth_enabled:
        if st.session_state.get("clear_local_auth_cookie"):
            return AuthenticationState(actor=None)
        token = request_cookie(st)
        actor = resolve_auth_session(session, token)
        if actor is None:
            if token:
                forget_token(st)
            return AuthenticationState(actor=None)
        remember_token(st, token or "")
        return AuthenticationState(actor=actor, local_token=token)

    oidc_login_state = _oidc_login_state()
    if oidc_login_state is None:
        return AuthenticationState(
            actor=None,
            error="Login indisponível: autenticação Google não configurada.",
            oidc_available=False,
        )
    if not oidc_login_state:
        return AuthenticationState(actor=None)

    email = _oidc_value("email")
    name = _oidc_value("name") or _oidc_value("given_name")
    issuer = _oidc_value("iss")
    subject = _oidc_value("sub")
    email_verified = (_oidc_value("email_verified") or "").lower() == "true"
    if not email:
        return AuthenticationState(
            actor=None,
            error="O provedor OIDC não retornou um e-mail.",
            oidc_logged_in=True,
        )
    try:
        user = resolve_oidc_user(
            session,
            email=email,
            display_name=name,
            settings=settings,
            issuer=issuer,
            subject=subject,
            email_verified=email_verified,
        )
    except AuthorizationError as error:
        return AuthenticationState(
            actor=None,
            error=str(error),
            oidc_logged_in=True,
        )
    return AuthenticationState(actor=user, oidc_logged_in=True)


def _flush_browser_session_bridge(
    bridge_slot,
    settings: Settings,
    auth_state: AuthenticationState,
) -> None:
    """Apply a pending cookie change without shifting the page's delta tree."""

    if st.session_state.get("clear_local_auth_cookie"):
        render_cookie_bridge(bridge_slot, clear=True, secure=settings.is_production)
        st.session_state.pop("clear_local_auth_cookie", None)
        st.session_state.pop("local_auth_expires_at", None)
        return
    expires_value = st.session_state.get("local_auth_expires_at")
    if auth_state.local_token and expires_value:
        render_cookie_bridge(
            bridge_slot,
            token=auth_state.local_token,
            expires_at=datetime.fromisoformat(str(expires_value)),
            secure=settings.is_production,
        )
        st.session_state.pop("local_auth_expires_at", None)


def login_view(
    session,
    settings: Settings,
    *,
    auth_error: str | None = None,
    oidc_logged_in: bool = False,
    oidc_available: bool = True,
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

        if settings.demo_auth_enabled:
            st.warning("Modo demo local ativo")
            users = list(
                session.scalars(
                    select(User)
                    .where(User.active.is_(True))
                    .order_by(User.display_name)
                ).all()
            )
            if not users:
                st.error("Nenhum usuário demo foi criado.")
                return
            with st.form("demo_login_form"):
                selected_id = st.selectbox(
                    "Entrar como",
                    [user.id for user in users],
                    format_func=lambda value: next(
                        f"{user.display_name} ({user.role.value})"
                        for user in users
                        if user.id == value
                    ),
                    key="demo_login_user_selection",
                )
                submitted = st.form_submit_button("Entrar", type="primary")
            if submitted:
                st.session_state["demo_user_id"] = selected_id
                st.rerun()
            return

        if settings.local_auth_enabled:
            admin_exists = bool(
                session.scalar(
                    select(func.count(User.id)).where(
                        User.role == Role.ADMIN,
                        User.password_hash.is_not(None),
                    )
                )
            )
            if not admin_exists:
                st.error(
                    "Nenhum administrador inicial foi configurado. Defina "
                    "BOOTSTRAP_ADMIN_NAME, BOOTSTRAP_ADMIN_EMAIL e "
                    "BOOTSTRAP_ADMIN_PASSWORD e reinicie a aplicação."
                )
                return
            with st.form("local_login_form"):
                email = st.text_input("E-mail", autocomplete="email")
                password = st.text_input(
                    "Senha", type="password", autocomplete="current-password"
                )
                submitted = st.form_submit_button("Entrar", type="primary")
            if submitted:
                try:
                    result = login_with_password(
                        session,
                        email=email,
                        password=password,
                        settings=settings,
                    )
                    session.commit()
                except AuthenticationError as error:
                    session.commit()
                    st.error(str(error))
                else:
                    remember_token(st, result.token)
                    st.session_state["local_auth_expires_at"] = (
                        result.expires_at.isoformat()
                    )
                    st.rerun()
            return

        if not oidc_available:
            st.info(
                "O administrador deve configurar o provedor Google ou ativar "
                "explicitamente o modo demo em um ambiente não produtivo."
            )
            return

        if oidc_logged_in:
            st.write("A sessão atual não pôde ser autorizada.")
            if st.button("Sair e tentar novamente", type="primary"):
                st.logout()
            return

        st.write("Entre com uma conta Google autorizada pela equipe.")
        if st.button("Entrar com Google", type="primary"):
            st.login("google")


def account_view(session, actor: User, settings: Settings) -> None:
    st.header("Minha conta")
    role_label = "Administrador" if actor.role == Role.ADMIN else "Aluno"
    with st.container(border=True, key="account_card"):
        st.write(f"**Nome:** {actor.display_name}")
        st.write(f"**E-mail:** {actor.email}")
        st.write(f"**Papel:** {role_label}")
        if actor.last_login_at:
            st.write(f"**Último acesso:** {actor.last_login_at:%d/%m/%Y %H:%M}")

        if settings.demo_auth_enabled:
            st.caption("Sessão local de demonstração")
            if st.button("Sair do modo demo", type="primary"):
                st.session_state.pop("demo_user_id", None)
                st.session_state.pop("demo_login_user_selection", None)
                st.rerun()
            return

        if settings.local_auth_enabled:
            with st.expander("Alterar minha senha"):
                with st.form("account_password_change"):
                    current_password = st.text_input(
                        "Senha atual", type="password", key="account_current_password"
                    )
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
                    else:
                        try:
                            change_password(
                                session,
                                user=actor,
                                current_password=current_password,
                                new_password=new_password,
                            )
                            session.commit()
                        except Exception as error:
                            session.rollback()
                            show_operation_error("change_password", error)
                        else:
                            forget_token(st)
                            st.success("Senha alterada. Entre novamente.")
            if st.button("Sair", type="primary"):
                revoke_session(session, request_cookie(st))
                session.commit()
                forget_token(st)
                st.rerun()
            return

        if st.button("Sair", type="primary"):
            st.logout()


def forced_password_change_view(session, actor: User, settings: Settings) -> None:
    st.header("Crie sua nova senha")
    st.warning("A senha temporária deve ser substituída antes de continuar.")
    with st.container(border=True, key="account_card"):
        with st.form("forced_password_change"):
            current_password = st.text_input(
                "Senha temporária", type="password", autocomplete="current-password"
            )
            new_password = st.text_input(
                "Nova senha", type="password", autocomplete="new-password"
            )
            confirmation = st.text_input(
                "Confirme a nova senha", type="password", autocomplete="new-password"
            )
            submitted = st.form_submit_button("Alterar senha", type="primary")
        if submitted:
            if new_password != confirmation:
                st.error("As novas senhas não coincidem.")
                return
            try:
                change_password(
                    session,
                    user=actor,
                    current_password=current_password,
                    new_password=new_password,
                )
                session.commit()
            except Exception as error:
                session.rollback()
                show_operation_error("change_password", error)
                return
            forget_token(st)
            st.success("Senha alterada. Entre novamente com a nova senha.")


def _password_change_route(
    session, actor: User | None, settings: Settings
) -> PageRoute:
    return PageRoute(
        "Trocar senha",
        "change-password",
        ":material/password:",
        lambda: forced_password_change_view(session, actor, settings),
    )


def _run_navigation(
    routes: Sequence[PageRoute],
    *,
    visible_routes: Sequence[PageRoute] | None = None,
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
        for route in visible_routes or routes:
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
                oidc_logged_in=auth_state.oidc_logged_in,
                oidc_available=auth_state.oidc_available,
            ),
        ),
    ]


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
            lambda: admin_dashboard(session),
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
        PageRoute(
            "Relatórios",
            "ledger",
            ":material/receipt_long:",
            lambda: ledger_view(session, actor, settings),
        ),
        PageRoute(
            "Lembretes",
            "reminders",
            ":material/mail:",
            lambda: reminders_view(session, actor, settings),
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
            lambda: leaderboard_view(session, key_prefix="student_page"),
        ),
    ]


def _render_login_state(
    session,
    settings: Settings,
    auth_state: AuthenticationState,
) -> None:
    login_view(
        session,
        settings,
        auth_error=auth_state.error,
        oidc_logged_in=auth_state.oidc_logged_in,
        oidc_available=auth_state.oidc_available,
    )


def _root_view(
    session,
    settings: Settings,
    auth_state: AuthenticationState,
) -> None:
    actor = auth_state.actor
    if actor is None:
        _render_login_state(session, settings, auth_state)
    elif settings.local_auth_enabled and actor.must_change_password:
        forced_password_change_view(session, actor, settings)
    elif actor.role == Role.ADMIN:
        admin_dashboard(session)
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
        if settings.local_auth_enabled and actor.must_change_password:
            forced_password_change_view(session, actor, settings)
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
    password_route = _password_change_route(session, actor, settings)
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
    registered.append(
        _guarded_route(
            account_route,
            session=session,
            settings=settings,
            auth_state=auth_state,
            allowed_roles=frozenset({Role.ADMIN, Role.STUDENT}),
        )
    )

    def render_password_page() -> None:
        current_actor = auth_state.actor
        if current_actor is None:
            _render_login_state(session, settings, auth_state)
        elif settings.local_auth_enabled and current_actor.must_change_password:
            forced_password_change_view(session, current_actor, settings)
        else:
            account_view(session, current_actor, settings)

    registered.append(
        PageRoute(
            password_route.label,
            password_route.url_path,
            password_route.icon,
            render_password_page,
        )
    )
    return registered


def _visible_routes(
    session,
    settings: Settings,
    auth_state: AuthenticationState,
) -> list[PageRoute]:
    actor = auth_state.actor
    if actor is None:
        return _public_routes(session, settings, auth_state)
    if settings.local_auth_enabled and actor.must_change_password:
        return [
            PageRoute(
                "Trocar senha",
                "root",
                ":material/password:",
                lambda: forced_password_change_view(session, actor, settings),
            )
        ]
    routes = (
        _admin_routes(session, actor, settings)
        if actor.role == Role.ADMIN
        else _student_routes(session, actor, settings)
    )
    routes.append(_account_route(session, actor, settings))
    return routes


def leaderboard_view(session, *, key_prefix: str = "leaderboard") -> None:
    st.subheader("Leaderboard")
    use_period = st.checkbox("Filtrar por período", key=f"{key_prefix}_period")
    start = end = None
    if use_period:
        left, right = st.columns(2)
        start = left.date_input("Início", value=date.today().replace(day=1), key=f"{key_prefix}_start")
        end = right.date_input("Fim", value=date.today(), key=f"{key_prefix}_end")
        if start > end:
            st.error("A data inicial deve ser anterior à final.")
            return
    rows = leaderboard_rows(session, start=start, end=end)
    if not rows:
        st.info("Ainda não há alunos no leaderboard.")
        return
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    podium = [row for row in rows if int(row["position"]) <= 3]
    columns = st.columns(max(1, len(podium)))
    for column, row in zip(columns, podium, strict=True):
        with column.container(border=True):
            column.markdown(f"## {medals.get(int(row['position']), '🏅')}")
            column.subheader(str(row["student"]))
            column.metric("Pontos", int(row["points"]))
            column.caption(f"Posição #{row['position']}")
    remaining = [row for row in rows if int(row["position"]) > 3]
    if remaining:
        st.markdown("#### Classificação geral")
        for row in remaining:
            with st.container(border=True):
                left, right = st.columns([4, 1])
                left.write(f"**#{row['position']} · {row['student']}**")
                right.write(f"**{row['points']} pts**")


def student_dashboard(session, actor: User) -> None:
    first_name = actor.display_name.strip().split()[0]
    st.header(f"Olá, {first_name}")
    board = leaderboard_rows(session)
    own = next((row for row in board if row["student_id"] == actor.id), None)
    progress, threshold = lesson_progress(session, actor.id)
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
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Pontuação", student_total(session, actor.id))
    col2.metric("Posição", f"#{own['position']}" if own else "—")
    col3.metric("Progresso de lições", f"{progress} de {threshold}")
    col4.metric("Pendências", pending)
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
        requirements.append(f"resumo/anotações ({activity.summary_min_chars}+ caracteres)")
    if activity.requires_title_or_url:
        requirements.append("título ou URL")
    st.caption("Campos exigidos: " + (", ".join(requirements) or "nenhum campo adicional"))

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
        st.error(
            f"Envie no máximo {settings.max_upload_files} arquivos por submissão."
        )
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
                if any(Path(file.name).suffix.casefold() in image_extensions for file in files)
                else None
            )
            result = submit_evidence(
                session,
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
            sync_google_sheets_snapshot(session, settings)
        except Exception as error:
            session.rollback()
            show_operation_error("submission_processing", error)
            return
    if result.status in {SubmissionStatus.APPROVED_AUTO, SubmissionStatus.APPROVED_MANUAL}:
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


def _submission_filters(session, *, prefix: str, include_students: bool = False):
    students = list(
        session.scalars(
            select(User)
            .where(User.role == Role.STUDENT)
            .order_by(User.display_name)
        ).all()
    )
    activities = list(session.scalars(select(Activity).order_by(Activity.name)).all())
    columns = st.columns(4 if include_students else 3)
    query = st.query_params
    offset = 0
    student_id = None
    if include_students:
        student_options = [None, *[student.id for student in students]]
        requested_student = query.get(f"{prefix}_student") or None
        student_id = columns[0].selectbox(
            "Aluno",
            student_options,
            index=student_options.index(requested_student)
            if requested_student in student_options
            else 0,
            format_func=lambda value: "Todos os alunos"
            if value is None
            else next(item.display_name for item in students if item.id == value),
            key=f"{prefix}_student",
        )
        offset = 1
    status_options = [None, *list(SubmissionStatus)]
    requested_status = query.get(f"{prefix}_status") or None
    requested_status_value = (
        SubmissionStatus(requested_status)
        if requested_status in {status.value for status in SubmissionStatus}
        else None
    )
    status_value = columns[offset].selectbox(
        "Estado",
        status_options,
        index=status_options.index(requested_status_value),
        format_func=lambda value: "Todos os estados"
        if value is None
        else STATUS_VISUAL[value][1],
        key=f"{prefix}_status",
    )
    activity_options = [None, *[activity.id for activity in activities]]
    requested_activity = query.get(f"{prefix}_activity") or None
    activity_id = columns[offset + 1].selectbox(
        "Atividade",
        activity_options,
        index=activity_options.index(requested_activity)
        if requested_activity in activity_options
        else 0,
        format_func=lambda value: "Todas as atividades"
        if value is None
        else next(item.name for item in activities if item.id == value),
        key=f"{prefix}_activity",
    )
    period_options = [0, 7, 30, 90]
    try:
        requested_period = int(query.get(f"{prefix}_period", "0"))
    except (TypeError, ValueError):
        requested_period = 0
    period = columns[offset + 2].selectbox(
        "Período",
        period_options,
        index=period_options.index(requested_period)
        if requested_period in period_options
        else 0,
        format_func=lambda value: "Todo o período" if value == 0 else f"Últimos {value} dias",
        key=f"{prefix}_period",
    )
    if include_students:
        query[f"{prefix}_student"] = student_id or ""
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
    if submission.files:
        for index, stored_file in enumerate(submission.files, start=1):
            icon = {"image": "🖼️", "pdf": "📕", "docx": "📘", "txt": "📄"}.get(
                stored_file.file_kind, "📎"
            )
            label = stored_file.client_filename or f"Arquivo {index}"
            try:
                authorized_file, path = get_submission_file_for_user(
                    session,
                    actor=actor,
                    file_id=stored_file.id,
                    settings=settings,
                )
            except LookupError:
                st.warning(f"{icon} {label} não está disponível no armazenamento.")
                continue
            if authorized_file.file_kind == "image":
                st.image(str(path), caption=label, width="stretch")
            st.download_button(
                f"{icon} Abrir/baixar {label}",
                data=path.read_bytes(),
                file_name=label,
                mime=authorized_file.mime_type,
                key=f"download_{submission.id}_{stored_file.id}",
            )
        return
    # Compatibility with images created before the generic file migration.
    for image in submission.images:
        path = settings.upload_dir / image.storage_key
        if path.is_file():
            st.image(
                str(path),
                caption=image.client_filename or "Imagem enviada",
                width="stretch",
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
        icon, label, _ = STATUS_VISUAL[submission.status]
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
                if matches:
                    st.warning(
                        f"{len(matches)} possível(is) duplicidade(s) exata(s) ou visual(is)."
                    )
                if submission.status == SubmissionStatus.NEEDS_REVIEW:
                    with st.form(f"review_{submission.id}"):
                        units = st.number_input(
                            "Unidades reconhecidas",
                            min_value=0,
                            max_value=max(1, len(submission.images)),
                            value=max(1, submission.recognized_units),
                            disabled=submission.activity.code != "duolingo_beconfident",
                        )
                        reason = st.text_area("Justificativa administrativa")
                        approve = st.form_submit_button("Aprovar", type="primary")
                        reject = st.form_submit_button("Rejeitar")
                    if approve or reject:
                        try:
                            result = review_submission(
                                session,
                                actor=actor,
                                submission_id=submission.id,
                                approve=approve,
                                reason=reason,
                                recognized_units=int(units) if approve else None,
                            )
                            session.commit()
                            if settings is not None:
                                sync_google_sheets_snapshot(session, settings)
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
    st.header("Histórico de envios")
    student_id, status_value, activity_id, start = _submission_filters(
        session, prefix="admin_submissions", include_students=True
    )
    submissions = list_submissions(
        session,
        actor=actor,
        student_id=student_id,
        status=status_value,
        activity_id=activity_id,
        start=start,
    )
    pending = sum(
        item.status == SubmissionStatus.NEEDS_REVIEW for item in submissions
    )
    if pending:
        st.warning(f"{pending} envio(s) aguardando revisão nos filtros atuais.")
    _render_submission_cards(
        session,
        actor,
        submissions,
        settings,
        admin_mode=True,
    )


def users_view(session, actor: User, settings: Settings | None = None) -> None:
    st.header("Alunos e administradores")
    users = list(session.scalars(select(User).order_by(User.display_name)).all())
    active_count = sum(user.active and user.archived_at is None for user in users)
    inactive_count = sum(not user.active and user.archived_at is None for user in users)
    archived_count = sum(user.archived_at is not None for user in users)
    summary = st.columns(3)
    summary[0].metric("Ativos", active_count)
    summary[1].metric("Inativos", inactive_count)
    summary[2].metric("Arquivados", archived_count)
    st.dataframe(
        [
            {
                "Nome": user.display_name,
                "E-mail": user.email,
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
        st.success("Senha temporária gerada. Copie-a agora; ela não será exibida novamente.")
        st.code(generated, language=None)
    with st.expander("Criar nova conta"):
        with st.form("new_user_form", clear_on_submit=True):
            new_name = st.text_input("Nome")
            new_email = st.text_input("E-mail")
            new_role = st.selectbox(
                "Papel", [Role.STUDENT.value, Role.ADMIN.value]
            )
            new_reason = st.text_input("Motivo", value="Cadastro administrativo")
            create_submitted = st.form_submit_button("Criar e gerar senha temporária")
        if create_submitted:
            try:
                _, temporary_password = create_user_account(
                    session,
                    actor=actor,
                    email=new_email,
                    display_name=new_name,
                    role=new_role,
                    reason=new_reason,
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
        email = st.text_input("E-mail", value=current.email)
        role = st.selectbox(
            "Papel",
            [Role.STUDENT.value, Role.ADMIN.value],
            index=1 if current.role == Role.ADMIN else 0,
        )
        active = st.checkbox("Conta ativa", value=current.active)
        reminders_enabled = st.checkbox(
            "Receber lembretes", value=current.reminders_enabled
        )
        reason = st.text_input("Motivo da alteração")
        submitted = st.form_submit_button("Salvar alterações", type="primary")
    if submitted:
        try:
            save_user(
                session,
                actor=actor,
                email=email,
                display_name=name,
                role=role,
                active=active,
                user_id=current.id,
                reason=reason,
                reminders_enabled=reminders_enabled,
            )
            session.commit()
            if settings is not None:
                sync_google_sheets_snapshot(session, settings)
            st.success("Usuário salvo.")
        except Exception as error:
            session.rollback()
            show_operation_error("save_user", error)

    with st.expander("Redefinir senha"):
        with st.form("reset_password_form"):
            reset_reason = st.text_input("Motivo da redefinição")
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
                        user_id=current.id,
                        reason=reset_reason,
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
            delete_reason = st.text_input("Motivo da exclusão/arquivamento")
            confirmation = st.text_input(
                f'Digite "{current.email}" para confirmar'
            )
            delete_submitted = st.form_submit_button("Confirmar exclusão")
        if delete_submitted:
            if confirmation.strip().lower() != current.email.lower():
                st.error("A confirmação não corresponde ao e-mail da conta.")
            else:
                try:
                    result = archive_or_delete_user(
                        session,
                        actor=actor,
                        user_id=current.id,
                        reason=delete_reason,
                    )
                    session.commit()
                except Exception as error:
                    session.rollback()
                    show_operation_error("delete_user", error)
                else:
                    st.success("Conta arquivada." if result == "archived" else "Conta excluída.")
                    st.rerun()


def catalog_view(session, actor: User, settings: Settings | None = None) -> None:
    st.header("Catálogo")
    activities = list(session.scalars(select(Activity).order_by(Activity.name)).all())
    st.dataframe(
        [
            {
                "Atividade": item.name,
                "Código": item.code,
                "Pontos futuros": item.points,
                "Unidades": item.unit_threshold,
                "Estado": "Arquivada"
                if item.archived_at
                else "Ativa"
                if item.active
                else "Inativa",
            }
            for item in activities
        ],
        width="stretch",
        hide_index=True,
    )
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
            new_reason = st.text_input("Motivo", value="Criação administrativa")
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
                    reason=new_reason,
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
        format_func=lambda value: next(item.name for item in editable if item.id == value),
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
        minimum = st.number_input("Mínimo de caracteres", min_value=0, max_value=10000, value=activity.summary_min_chars)
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
        requires_images = st.checkbox("Exige comprovação", value=activity.requires_images)
        requires_summary = st.checkbox("Exige resumo/anotações", value=activity.requires_summary)
        requires_title = st.checkbox("Exige título ou URL", value=activity.requires_title_or_url)
        content_review = st.checkbox("Exige revisão humana de conteúdo", value=activity.content_review_required)
        auto_approvable = st.checkbox("Pode ser autoaprovada", value=activity.auto_approvable)
        active = st.checkbox("Ativa", value=activity.active)
        reason = st.text_input("Motivo da alteração")
        submitted = st.form_submit_button("Salvar", type="primary")
    if submitted:
        try:
            save_activity_changes(
                session,
                actor=actor,
                activity_id=activity.id,
                name=name,
                points=int(points),
                active=active,
                summary_min_chars=int(minimum),
                unit_threshold=int(threshold),
                requires_images=requires_images,
                requires_summary=requires_summary,
                requires_title_or_url=requires_title,
                content_review_required=content_review,
                auto_approvable=auto_approvable,
                reason=reason,
            )
            session.commit()
            if settings is not None:
                sync_google_sheets_snapshot(session, settings)
            st.success("Catálogo atualizado. Transações históricas permaneceram intactas.")
        except Exception as error:
            session.rollback()
            show_operation_error("save_activity", error)
    with st.expander("Excluir ou arquivar atividade"):
        st.warning(
            "Atividades com histórico serão arquivadas. Atividades nunca usadas "
            "podem ser excluídas permanentemente."
        )
        with st.form("delete_activity_form"):
            delete_reason = st.text_input("Motivo da exclusão/arquivamento")
            confirmation = st.text_input(
                f'Digite "{activity.code}" para confirmar'
            )
            delete_submitted = st.form_submit_button("Confirmar exclusão")
        if delete_submitted:
            if confirmation.strip() != activity.code:
                st.error("A confirmação não corresponde ao código da atividade.")
            else:
                try:
                    result = archive_or_delete_activity(
                        session,
                        actor=actor,
                        activity_id=activity.id,
                        reason=delete_reason,
                    )
                    session.commit()
                except Exception as error:
                    session.rollback()
                    show_operation_error("delete_activity", error)
                else:
                    st.success(
                        "Atividade arquivada."
                        if result == "archived"
                        else "Atividade excluída."
                    )
                    st.rerun()


def reminders_view(session, actor: User, settings: Settings) -> None:
    st.header("Lembretes por e-mail")
    configuration = get_reminder_configuration(session)
    mode_label = "Dry-run: nenhum e-mail será enviado" if settings.reminder_dry_run else "Envio SMTP real habilitado"
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
            "Horário (hora cheia)", min_value=0, max_value=23, value=configuration.send_hour
        )
        timezone_name = st.text_input(
            "Fuso horário", value=configuration.timezone_name
        )
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
            help="Variáveis disponíveis: {name} e {email}.",
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
                attempt = send_test_reminder(
                    session, actor=actor, settings=settings
                )
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
            sync_google_sheets_snapshot(session, settings)
        open_action.link_button("Abrir planilha", sheet_url, width="stretch")
    else:
        st.info(
            "A sincronização automática com Google Sheets está desativada. "
            "Os downloads locais continuam disponíveis."
        )
    left, right = st.columns(2)
    start = left.date_input("Início", value=date.today().replace(day=1), key="ledger_start")
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
            format_func=lambda value: next(student.display_name for student in students if student.id == value),
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
        st.caption(f"{len(submissions)} submissão(ões) no histórico; detalhes em Envios.")

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
            adjustment_reason = st.text_area("Motivo obrigatório")
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
                        reason=adjustment_reason,
                    )
                    session.commit()
                    if settings is not None:
                        sync_google_sheets_snapshot(session, settings)
                except Exception as error:
                    session.rollback()
                    show_operation_error("points_adjustment", error)
                else:
                    st.success("Ajuste registrado como nova transação imutável.")


def admin_dashboard(session) -> None:
    st.header("Visão geral")
    pending = session.scalar(select(func.count(Submission.id)).where(Submission.status == SubmissionStatus.NEEDS_REVIEW)) or 0
    students = session.scalar(select(func.count(User.id)).where(User.role == Role.STUDENT, User.active.is_(True))) or 0
    transactions = session.scalar(select(func.count(LedgerTransaction.id))) or 0
    col1, col2, col3 = st.columns(3)
    col1.metric("Fila de revisão", pending)
    col2.metric("Alunos ativos", students)
    col3.metric("Transações no ledger", transactions)
    leaderboard_view(session, key_prefix="admin_dashboard")


def main() -> None:
    try:
        settings, factory = runtime()
    except Exception as error:
        st.error(f"Configuração inválida: {error}")
        st.stop()
    with session_scope(factory) as session:
        # This placeholder must exist at the same root delta path on every run.
        # Filling an occasional iframe before the navigation shifts Streamlit's
        # element tree and can leave old login forms attached after authentication.
        browser_session_bridge = st.empty()
        auth_state = authenticate(session, settings)
        st.session_state.pop("post_login_ready", None)
        st.session_state.pop("post_login_route", None)
        registered_routes = _registered_routes(session, settings, auth_state)
        visible_routes = _visible_routes(session, settings, auth_state)
        _run_navigation(registered_routes, visible_routes=visible_routes)
        _flush_browser_session_bridge(browser_session_bridge, settings, auth_state)


if __name__ == "__main__":
    main()
