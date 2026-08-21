from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from english_leaderboard.config import Settings
from english_leaderboard.database import (
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from english_leaderboard.local_auth import (
    AuthenticationError,
    bootstrap_initial_admin,
    change_password,
    create_auth_session,
    hash_password,
    login_with_password,
    resolve_auth_session,
    revoke_session,
)
from english_leaderboard.models import AuthSession, Role, User, utcnow
from english_leaderboard.services import save_user


def test_bootstrap_admin_is_idempotent_and_never_overwrites(tmp_path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    initialize_database(engine)
    factory = create_session_factory(engine)
    settings = Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'bootstrap.db'}",
        upload_dir=tmp_path / "uploads",
        bootstrap_admin_name="Primeira Admin",
        bootstrap_admin_email="admin@example.org",
        bootstrap_admin_password="SenhaInicial123!",
    )
    with factory() as session:
        first = bootstrap_initial_admin(session, settings)
        session.commit()
        first_hash = first.password_hash
        second = bootstrap_initial_admin(
            session,
            replace(settings, bootstrap_admin_password="OutraSenha456!"),
        )
        session.commit()
        assert first.id == second.id
        assert second.password_hash == first_hash
        assert session.scalar(select(func.count(User.id))) == 1
    engine.dispose()


def test_login_persistent_session_password_change_logout_and_expiry(
    session, users, settings
) -> None:
    user = users[Role.STUDENT]
    user.password_hash = hash_password("SenhaTemporaria123!")
    user.must_change_password = True
    session.commit()

    with pytest.raises(AuthenticationError):
        login_with_password(
            session,
            email=user.email,
            password="errada",
            settings=settings,
        )
    result = login_with_password(
        session,
        email=user.email,
        password="SenhaTemporaria123!",
        settings=settings,
    )
    session.commit()
    assert resolve_auth_session(session, result.token).id == user.id

    change_password(
        session,
        user=user,
        current_password="SenhaTemporaria123!",
        new_password="NovaSenhaSegura456!",
    )
    session.commit()
    assert user.must_change_password is False
    assert resolve_auth_session(session, result.token) is None

    renewed = login_with_password(
        session,
        email=user.email,
        password="NovaSenhaSegura456!",
        settings=settings,
    )
    session.commit()
    revoke_session(session, renewed.token)
    session.commit()
    assert resolve_auth_session(session, renewed.token) is None

    expired = login_with_password(
        session,
        email=user.email,
        password="NovaSenhaSegura456!",
        settings=settings,
    )
    auth_session = session.scalar(
        select(AuthSession).where(AuthSession.token_hash.is_not(None)).order_by(
            AuthSession.created_at.desc()
        )
    )
    auth_session.expires_at = utcnow() - timedelta(seconds=1)
    session.commit()
    assert resolve_auth_session(session, expired.token) is None


def test_unbounded_or_malformed_session_tokens_are_rejected(session) -> None:
    assert resolve_auth_session(session, "short") is None
    assert resolve_auth_session(session, "x" * 10_000) is None
    revoke_session(session, "x" * 10_000)


def test_demo_and_local_sessions_cannot_cross_authentication_modes(
    session,
    settings,
    users,
) -> None:
    user = users[Role.ADMIN]
    local_result = create_auth_session(session, user, settings)
    demo_result = create_auth_session(session, user, settings, audience="demo")

    assert resolve_auth_session(session, local_result.token).id == user.id
    assert resolve_auth_session(session, local_result.token, audience="demo") is None
    assert resolve_auth_session(session, demo_result.token) is None
    assert (
        resolve_auth_session(session, demo_result.token, audience="demo").id
        == user.id
    )


def test_last_active_admin_cannot_be_disabled(session, users) -> None:
    admin = users[Role.ADMIN]
    with pytest.raises(ValueError, match="último administrador"):
        save_user(
            session,
            actor=admin,
            email=admin.email,
            display_name=admin.display_name,
            role=Role.ADMIN,
            active=False,
            user_id=admin.id,
            reason="Teste de proteção",
        )
