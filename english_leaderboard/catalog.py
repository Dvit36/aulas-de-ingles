from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import Activity, ReminderConfiguration, Resource, Role, User

CATALOG_SEED: tuple[dict[str, object], ...] = (
    {
        "code": "duolingo_beconfident",
        "name": "Duolingo/BeConfident — a cada 5 lições validadas",
        "points": 5,
        "unit_threshold": 5,
        "requires_images": True,
        "auto_approvable": True,
        "config_json": {"platforms": ["duolingo", "beconfident"]},
    },
    {
        "code": "impact_summary",
        "name": "Impact + resumo em português",
        "points": 10,
        "requires_images": True,
        "requires_summary": True,
        "requires_title_or_url": True,
        "summary_min_chars": 120,
        "content_review_required": True,
    },
    {
        "code": "video_fun_summary",
        "name": "Video FUN + resumo em português",
        "points": 10,
        "requires_images": True,
        "requires_summary": True,
        "requires_title_or_url": True,
        "summary_min_chars": 120,
        "content_review_required": True,
    },
    {
        "code": "youtube_lesson_notes",
        "name": "Videoaula do YouTube + anotações",
        "points": 12,
        "requires_images": True,
        "requires_summary": True,
        "requires_title_or_url": True,
        "summary_min_chars": 120,
        "content_review_required": True,
    },
    {"code": "cambridge_basic", "name": "Cambridge English Basic", "points": 10},
    {
        "code": "cambridge_independent",
        "name": "Cambridge English Independent",
        "points": 15,
    },
    {
        "code": "cambridge_proficient",
        "name": "Cambridge English Proficient",
        "points": 20,
    },
    {
        "code": "write_improve_beginner",
        "name": "Write & Improve Beginner",
        "points": 10,
    },
    {
        "code": "write_improve_intermediate",
        "name": "Write & Improve Intermediate",
        "points": 15,
    },
    {
        "code": "write_improve_advanced",
        "name": "Write & Improve Advanced",
        "points": 20,
    },
    {
        "code": "write_improve_fun",
        "name": "Write & Improve Just for Fun",
        "points": 7,
    },
    {
        "code": "write_improve_business",
        "name": "Write & Improve For Business",
        "points": 25,
    },
    {
        "code": "english_meeting",
        "name": "Reunião em inglês",
        "points": 30,
        "requires_images": False,
        "auto_approvable": False,
        "config_json": {},
    },
)


RESOURCE_SEED: tuple[dict[str, object], ...] = (
    {
        "title": "Cambridge Write & Improve",
        "url": "https://writeandimprove.com/",
        "description": (
            "Ajuda a escrever textos: a plataforma dá tópicos e ideias de redação "
            "e devolve correção, do nível básico ao intermediário."
        ),
    },
    {
        "title": "Cambridge English",
        "url": "https://www.cambridgeenglish.org/",
        "description": (
            "Várias atividades de listening, gramática, speaking e mais, "
            "disponíveis para os três níveis."
        ),
    },
    {
        "title": "British Council — Learn English",
        "url": "https://learnenglish.britishcouncil.org/",
        "description": (
            "Tem uma seção de recursos gratuitos, na mesma pegada do "
            "Cambridge English."
        ),
    },
    {
        "title": "Simulado Duolingo English Test",
        "url": "https://englishtest.duolingo.com/practice",
        "description": "Simulado oficial para praticar o formato da prova.",
    },
    {
        "title": "Exercícios de listening — ESL Lab",
        "url": "https://www.esl-lab.com/easy/",
        "description": (
            "Learn English Through Listening to Daily English Conversations: "
            "diálogos curtos do dia a dia com exercícios."
        ),
    },
    {
        "title": "Aulas no YouTube — Speak English With Vanessa",
        "url": "https://www.youtube.com/@SpeakEnglishWithVanessa/playlists",
        "description": "Playlists de aulas em vídeo organizadas por tema.",
    },
    {
        "title": "Redações do Impact — FIRST Impact Award",
        "url": "https://www.firstinspires.org/resources/library/frc/fia-resources",
        "description": (
            "Na página há as opções 20xx winners. Em cada uma estão as redações "
            "vencedoras de cada regional naquele ano."
        ),
    },
    {
        "title": "FUN — FIRST Updates Now",
        "url": "https://youtube.com/@funroboticsnetwork",
        "description": "Canal no YouTube onde eles falam dos robôs.",
    },
)


def seed_resources(session: Session) -> list[Resource]:
    """Cria a lista inicial de recursos sem sobrescrever edições da equipe."""

    existing = {
        resource.url
        for resource in session.scalars(select(Resource)).all()
    }
    created: list[Resource] = []
    for position, definition in enumerate(RESOURCE_SEED, start=1):
        url = str(definition["url"])
        if url in existing:
            continue
        resource = Resource(position=position, **definition)
        session.add(resource)
        created.append(resource)
    session.flush()
    return created


def seed_catalog(session: Session) -> list[Activity]:
    existing = {
        activity.code: activity
        for activity in session.scalars(select(Activity)).all()
    }
    created: list[Activity] = []
    for definition in CATALOG_SEED:
        code = str(definition["code"])
        if code in existing:
            continue
        activity = Activity(**definition)
        session.add(activity)
        created.append(activity)
    session.flush()
    return created


def seed_demo_users(session: Session, settings: Settings) -> list[User]:
    if not settings.demo_auth_enabled:
        return []
    definitions: Iterable[tuple[str, str, Role]] = (
        (settings.demo_student_username, "Aluno Demo", Role.STUDENT),
        (settings.demo_admin_username, "Administrador Demo", Role.ADMIN),
    )
    created: list[User] = []
    for username, name, role in definitions:
        user = session.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(
                username=username, display_name=name, role=role, active=True
            )
            session.add(user)
            created.append(user)
    session.flush()
    return created


def seed_database(session: Session, settings: Settings) -> None:
    seed_catalog(session)
    seed_resources(session)
    seed_demo_users(session, settings)
    from .local_auth import bootstrap_initial_admin

    bootstrap_initial_admin(session, settings)
    if settings.seed_fake_data:
        from .synthetic_data import seed_fake_students

        seed_fake_students(session)
    if session.get(ReminderConfiguration, 1) is None:
        session.add(ReminderConfiguration(id=1, enabled=False))
    session.flush()
