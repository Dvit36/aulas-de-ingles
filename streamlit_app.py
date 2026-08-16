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
    leaderboard_to_csv,
    leaderboard_to_xlsx,
    ledger_to_csv,
    ledger_to_xlsx,
)
from english_leaderboard.google_sheets import sync_leaderboard_and_ledger
from english_leaderboard.models import (
    Activity,
    DuplicateMatch,
    LedgerTransaction,
    Role,
    Submission,
    SubmissionImage,
    SubmissionStatus,
    User,
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
    get_submission_for_user,
    list_review_queue,
    record_meeting,
    resolve_oidc_user,
    review_submission,
    save_activity_changes,
    save_user,
    submit_evidence,
)
from english_leaderboard.ui_styles import render_global_styles


APP_ROOT = Path(__file__).resolve().parent
BRAND_ASSETS = APP_ROOT / "assets" / "brand"
BRAND_MARK = BRAND_ASSETS / "logo-mark.png"
BRAND_WORDMARK = BRAND_ASSETS / "logo-wordmark.png"
BRAND_FULL = BRAND_ASSETS / "logo-full.png"


st.set_page_config(
    page_title="English Activities",
    page_icon=str(BRAND_MARK),
    layout="wide",
    initial_sidebar_state="collapsed",
)
render_global_styles(st)


STATUS_LABELS = {
    SubmissionStatus.PROCESSING: "Processando",
    SubmissionStatus.APPROVED_AUTO: "Aprovada automaticamente",
    SubmissionStatus.NEEDS_REVIEW: "Aguardando revisão",
    SubmissionStatus.APPROVED_MANUAL: "Aprovada manualmente",
    SubmissionStatus.REJECTED: "Rejeitada",
    SubmissionStatus.CANCELLED: "Cancelada",
}
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


def public_home_view() -> None:
    st.header("English Activities")
    with st.container(border=True, key="public_hero"):
        visual, content = st.columns([1, 2], vertical_alignment="center")
        visual.image(str(BRAND_FULL), width="stretch")
        content.markdown(
            '<span class="hero-kicker">Temporada 2026</span>',
            unsafe_allow_html=True,
        )
        content.subheader("Aprender, comprovar e evoluir")
        content.write(
            "Envie atividades de inglês, acompanhe seu progresso e veja a "
            "classificação da equipe Robonáticos #7565 em um único lugar."
        )
        st.info("Use a opção **Entrar** no menu superior para acessar sua conta.")


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


def account_view(actor: User, settings: Settings) -> None:
    st.header("Minha conta")
    role_label = "Administrador" if actor.role == Role.ADMIN else "Aluno"
    with st.container(border=True, key="account_card"):
        st.write(f"**Nome:** {actor.display_name}")
        st.write(f"**E-mail:** {actor.email}")
        st.write(f"**Papel:** {role_label}")

        if settings.demo_auth_enabled:
            st.caption("Sessão local de demonstração")
            if st.button("Sair do modo demo", type="primary"):
                st.session_state.pop("demo_user_id", None)
                st.session_state.pop("demo_login_user_selection", None)
                st.rerun()
            return

        if st.button("Sair", type="primary"):
            st.logout()


def _run_navigation(routes: Sequence[PageRoute]) -> None:
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
        for page, route in zip(pages, routes, strict=True):
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
            "Início",
            "home",
            ":material/home:",
            public_home_view,
        ),
        PageRoute(
            "Entrar",
            "login",
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


def _account_route(actor: User, settings: Settings) -> PageRoute:
    return PageRoute(
        "Minha conta",
        "account",
        ":material/account_circle:",
        lambda: account_view(actor, settings),
    )


def _admin_routes(session, actor: User, settings: Settings) -> list[PageRoute]:
    return [
        PageRoute(
            "Visão geral",
            "overview",
            ":material/dashboard:",
            lambda: admin_dashboard(session),
        ),
        PageRoute(
            "Revisões",
            "review-queue",
            ":material/rate_review:",
            lambda: review_queue_view(session, actor, settings),
        ),
        PageRoute(
            "Reuniões",
            "meeting",
            ":material/groups:",
            lambda: meeting_view(session, actor, settings),
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
    ]


def _student_routes(session, actor: User, settings: Settings) -> list[PageRoute]:
    return [
        PageRoute(
            "Início",
            "home",
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
    st.dataframe(
        [{"Posição": row["position"], "Aluno": row["student"], "Pontos": row["points"]} for row in rows],
        width="stretch",
        hide_index=True,
    )


def student_dashboard(session, actor: User) -> None:
    first_name = actor.display_name.strip().split()[0]
    st.header(f"Olá, {first_name}")
    board = leaderboard_rows(session)
    own = next((row for row in board if row["student_id"] == actor.id), None)
    progress, threshold = lesson_progress(session, actor.id)
    col1, col2, col3 = st.columns(3)
    col1.metric("Pontuação", student_total(session, actor.id))
    col2.metric("Posição", f"#{own['position']}" if own else "—")
    col3.metric("Progresso de lições", f"{progress} de {threshold}")
    st.caption(
        "O login identifica quem enviou, mas um print sem nome não prova de forma absoluta quem realizou a atividade."
    )

    st.subheader("Catálogo ativo")
    activities = list(
        session.scalars(
            select(Activity)
            .where(Activity.active.is_(True), Activity.code != "english_meeting")
            .order_by(Activity.name)
        ).all()
    )
    st.dataframe(
        [
            {
                "Atividade": activity.name,
                "Pontos": (
                    f"{activity.points} a cada {activity.unit_threshold} unidades"
                    if activity.unit_threshold > 1
                    else str(activity.points)
                ),
                "Revisão de conteúdo": "Sim" if activity.content_review_required else "Não",
            }
            for activity in activities
        ],
        width="stretch",
        hide_index=True,
    )
    leaderboard_view(session, key_prefix="student_dashboard")


def submission_form(session, actor: User, settings: Settings) -> None:
    st.header("Enviar atividade")
    activities = list(
        session.scalars(
            select(Activity)
            .where(Activity.active.is_(True), Activity.code != "english_meeting")
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
        requirements.append("imagem")
    if activity.requires_summary:
        requirements.append(f"resumo/anotações ({activity.summary_min_chars}+ caracteres)")
    if activity.requires_title_or_url:
        requirements.append("título ou URL")
    st.caption("Campos exigidos: " + (", ".join(requirements) or "nenhum campo adicional"))

    with st.form("submission_form", clear_on_submit=True):
        title = st.text_input("Título (quando aplicável)", max_chars=500)
        url = st.text_input("URL (quando aplicável)", max_chars=2048)
        summary = st.text_area("Resumo ou anotações", height=180)
        files = st.file_uploader(
            "Comprovação",
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True,
            help=f"Até {settings.max_upload_bytes // (1024 * 1024)} MB por arquivo.",
        )
        submitted = st.form_submit_button("Enviar e analisar", type="primary")
    if not submitted:
        return
    payloads = [UploadPayload(file.name, file.getvalue()) for file in files]
    with st.spinner("Validando arquivo e executando OCR local…"):
        try:
            engine = cached_ocr_engine() if payloads else None
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


def _submission_rows(submissions: list[Submission]) -> list[dict[str, object]]:
    return [
        {
            "Recebida": submission.received_at,
            "Atividade": submission.activity.name,
            "Estado": STATUS_LABELS[submission.status],
            "Plataforma": submission.detected_platform or "—",
            "Unidades": submission.recognized_units,
            "Pontos gerados": submission.points_awarded,
            "Motivo": submission.admin_reason or "—",
            "ID": submission.id,
        }
        for submission in submissions
    ]


def student_history(session, actor: User, settings: Settings) -> None:
    st.header("Minhas submissões")
    submissions = list(
        session.scalars(
            select(Submission)
            .where(Submission.student_id == actor.id)
            .order_by(Submission.received_at.desc())
        ).all()
    )
    # Load authorized detail through the service to avoid bypassing access rules.
    detailed = [get_submission_for_user(session, actor=actor, submission_id=item.id) for item in submissions]
    st.dataframe(_submission_rows(detailed), width="stretch", hide_index=True)
    if not detailed:
        return
    selected = st.selectbox(
        "Ver análise",
        [submission.id for submission in detailed],
        format_func=lambda value: next(
            f"{item.received_at:%d/%m %H:%M} · {item.activity.name}"
            for item in detailed
            if item.id == value
        ),
    )
    submission = next(item for item in detailed if item.id == selected)
    st.write(f"**Estado:** {STATUS_LABELS[submission.status]}")
    if submission.admin_reason:
        st.info(submission.admin_reason)
    with st.expander("Resultados das verificações"):
        st.dataframe(
            [
                {
                    "Regra": check.rule_name,
                    "Resultado": check.outcome.value,
                    "Obrigatória": check.required,
                    "Confiança": check.score,
                    "Mensagem": check.message,
                }
                for check in submission.checks
            ],
            hide_index=True,
            width="stretch",
        )


def review_queue_view(session, actor: User, settings: Settings) -> None:
    st.header("Fila de revisão")
    queue = list_review_queue(session, actor=actor)
    if not queue:
        st.success("Fila vazia.")
        return
    selected = st.selectbox(
        "Submissão",
        [submission.id for submission in queue],
        format_func=lambda value: next(
            f"{item.student.display_name} · {item.activity.name} · {item.received_at:%d/%m %H:%M}"
            for item in queue
            if item.id == value
        ),
    )
    submission = next(item for item in queue if item.id == selected)
    left, right = st.columns([2, 1])
    with left:
        st.write(f"**Aluno:** {submission.student.display_name}")
        st.write(f"**Atividade declarada:** {submission.activity.name}")
        st.write(f"**Plataforma detectada:** {submission.detected_platform or 'não conclusiva'}")
        st.write(f"**Confiança:** {submission.confidence:.0%}")
        if submission.title:
            st.write(f"**Título:** {submission.title}")
        if submission.url:
            st.write(f"**URL:** {submission.url}")
        if submission.summary:
            st.text_area("Resumo/anotações", submission.summary, height=150, disabled=True)
        st.text_area("OCR extraído", submission.ocr_text or "(vazio)", height=180, disabled=True)
    with right:
        for image in submission.images:
            path = settings.upload_dir / image.storage_key
            if path.is_file():
                st.image(
                    str(path),
                    caption=f"{image.image_format} · {image.width}×{image.height}",
                    width="stretch",
                )
    st.subheader("Regras")
    st.dataframe(
        [
            {
                "Regra": check.rule_name,
                "Resultado": check.outcome.value,
                "Obrigatória": check.required,
                "Confiança": check.score,
                "Mensagem": check.message,
            }
            for check in submission.checks
        ],
        width="stretch",
        hide_index=True,
    )
    image_ids = [image.id for image in submission.images]
    matches = list(
        session.scalars(
            select(DuplicateMatch).where(DuplicateMatch.image_id.in_(image_ids))
        ).all()
    ) if image_ids else []
    if matches:
        st.warning("Foram encontradas correspondências exatas ou visuais.")
        st.dataframe(
            [
                {
                    "Tipo": match.kind.value,
                    "Distância pHash": match.distance,
                    "Mesmo aluno": match.same_student,
                    "Imagem comparada": match.matched_image_id,
                }
                for match in matches
            ],
            hide_index=True,
            width="stretch",
        )
        with st.expander("Comparar imagens semelhantes"):
            for match in matches[:5]:
                current_image = session.get(SubmissionImage, match.image_id)
                matched_image = session.get(SubmissionImage, match.matched_image_id)
                columns = st.columns(2)
                if current_image:
                    current_path = settings.upload_dir / current_image.storage_key
                    if current_path.is_file():
                        columns[0].image(
                            str(current_path),
                            caption="Imagem desta submissão",
                            width="stretch",
                        )
                if matched_image:
                    matched_path = settings.upload_dir / matched_image.storage_key
                    if matched_path.is_file():
                        columns[1].image(
                            str(matched_path),
                            caption=f"Correspondência {match.kind.value}",
                            width="stretch",
                        )

    with st.form(f"review_{submission.id}"):
        units = st.number_input(
            "Unidades reconhecidas",
            min_value=0,
            max_value=100,
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
            sync_google_sheets_snapshot(session, settings)
        except Exception as error:
            session.rollback()
            show_operation_error("review_submission", error)
            return
        st.success(
            f"Decisão salva: {STATUS_LABELS[result.status]}. Pontos gerados: {result.points_created}."
        )


def users_view(session, actor: User, settings: Settings | None = None) -> None:
    st.header("Alunos e administradores")
    users = list(session.scalars(select(User).order_by(User.display_name)).all())
    st.dataframe(
        [
            {"Nome": user.display_name, "E-mail": user.email, "Papel": user.role.value, "Ativo": user.active}
            for user in users
        ],
        width="stretch",
        hide_index=True,
    )
    choices = ["__new__", *[user.id for user in users]]
    selected = st.selectbox(
        "Editar",
        choices,
        format_func=lambda value: "Novo usuário" if value == "__new__" else next(user.display_name for user in users if user.id == value),
    )
    current = next((user for user in users if user.id == selected), None)
    with st.form("user_form"):
        name = st.text_input("Nome", value=current.display_name if current else "")
        email = st.text_input("E-mail", value=current.email if current else "")
        role = st.selectbox("Papel", [Role.STUDENT.value, Role.ADMIN.value], index=1 if current and current.role == Role.ADMIN else 0)
        active = st.checkbox("Ativo", value=current.active if current else True)
        submitted = st.form_submit_button("Salvar")
    if submitted:
        try:
            save_user(session, actor=actor, email=email, display_name=name, role=role, active=active, user_id=current.id if current else None)
            session.commit()
            if settings is not None:
                sync_google_sheets_snapshot(session, settings)
            st.success("Usuário salvo.")
        except Exception as error:
            session.rollback()
            show_operation_error("save_user", error)


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
                "Ativa": item.active,
            }
            for item in activities
        ],
        width="stretch",
        hide_index=True,
    )
    selected = st.selectbox("Atividade para editar", [item.id for item in activities], format_func=lambda value: next(item.name for item in activities if item.id == value))
    activity = next(item for item in activities if item.id == selected)
    with st.form("activity_form"):
        name = st.text_input("Nome", value=activity.name)
        points = st.number_input("Pontos para futuras aprovações", min_value=1, max_value=1000, value=activity.points)
        minimum = st.number_input("Mínimo de caracteres", min_value=0, max_value=10000, value=activity.summary_min_chars)
        threshold = st.number_input("Unidades por premiação", min_value=1, max_value=100, value=activity.unit_threshold)
        requires_images = st.checkbox("Exige imagem", value=activity.requires_images)
        requires_summary = st.checkbox("Exige resumo/anotações", value=activity.requires_summary)
        requires_title = st.checkbox("Exige título ou URL", value=activity.requires_title_or_url)
        content_review = st.checkbox("Exige revisão humana de conteúdo", value=activity.content_review_required)
        auto_approvable = st.checkbox("Pode ser autoaprovada", value=activity.auto_approvable)
        active = st.checkbox("Ativa", value=activity.active)
        submitted = st.form_submit_button("Salvar")
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
            )
            session.commit()
            if settings is not None:
                sync_google_sheets_snapshot(session, settings)
            st.success("Catálogo atualizado. Transações históricas permaneceram intactas.")
        except Exception as error:
            session.rollback()
            show_operation_error("save_activity", error)


def meeting_view(session, actor: User, settings: Settings | None = None) -> None:
    st.header("Registrar reunião em inglês")
    students = list(
        session.scalars(
            select(User).where(User.role == Role.STUDENT, User.active.is_(True)).order_by(User.display_name)
        ).all()
    )
    if not students:
        st.info("Cadastre um aluno ativo primeiro.")
        return
    with st.form("meeting_form", clear_on_submit=True):
        student_id = st.selectbox("Aluno", [student.id for student in students], format_func=lambda value: next(student.display_name for student in students if student.id == value))
        meeting_date = st.date_input("Data", value=date.today())
        description = st.text_input("Reunião ou descrição")
        submitted = st.form_submit_button("Confirmar e conceder pontos", type="primary")
    if submitted:
        try:
            meeting = record_meeting(session, actor=actor, student_id=student_id, meeting_date=meeting_date, description=description)
            session.commit()
            if settings is not None:
                sync_google_sheets_snapshot(session, settings)
            st.success(f"Reunião registrada com auditoria. ID {meeting.id}")
        except Exception as error:
            session.rollback()
            show_operation_error("record_meeting", error)


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
    c1, c2, c3, c4 = st.columns(4)
    c1.download_button("Ledger CSV", ledger_to_csv(rows), "ledger.csv", "text/csv")
    c2.download_button("Ledger XLSX", ledger_to_xlsx(rows), "ledger.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    c3.download_button("Leaderboard CSV", leaderboard_to_csv(board), "leaderboard.csv", "text/csv")
    c4.download_button("Leaderboard XLSX", leaderboard_to_xlsx(board), "leaderboard.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
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
        st.dataframe(_submission_rows(submissions), width="stretch", hide_index=True)


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
        auth_state = authenticate(session, settings)
        actor = auth_state.actor
        if actor is None:
            _run_navigation(_public_routes(session, settings, auth_state))
            return
        if actor.role == Role.ADMIN:
            routes = _admin_routes(session, actor, settings)
        else:
            routes = _student_routes(session, actor, settings)
        routes.append(_account_route(actor, settings))
        _run_navigation(routes)


if __name__ == "__main__":
    main()
