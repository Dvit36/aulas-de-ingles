"""Local password authentication and revocable opaque sessions."""

from __future__ import annotations

import re
import secrets
import string
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .browser_session import valid_session_token
from .config import Settings
from .models import AuthSession, Role, User, utcnow

PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
GENERIC_LOGIN_ERROR = "E-mail ou senha inválidos. Tente novamente."
SessionAudience = Literal["local", "demo"]


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class LoginResult:
    user: User
    token: str
    expires_at: datetime


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def validate_password(password: str) -> None:
    """Aceita qualquer senha escolhida pela pessoa, desde que exista uma.

    Não há exigência de tamanho, letras ou números: a equipe decidiu que o
    aluno escolhe a senha que quiser. A única recusa é a senha vazia, que não
    é uma escolha e deixaria a conta sem credencial nenhuma.
    """

    if not password:
        raise ValueError("Informe uma senha")


def hash_password(password: str) -> str:
    validate_password(password)
    return PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        return False
    try:
        return bool(PASSWORD_HASHER.verify(password_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def generate_temporary_password(length: int = 16) -> str:
    """Senha temporária gerada pelo sistema, entregue uma única vez.

    Continua longa e aleatória mesmo sem exigência para a senha escolhida
    depois: esta aqui trafega até a pessoa e vale até a primeira troca.
    """

    if length < 12:
        raise ValueError("Senha temporária deve ter ao menos 12 caracteres")
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(length))


USERNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._@-]{2,149}")


def normalize_username(value: str) -> str:
    """Normaliza e valida um nome de usuário de login.

    Aceita letras, dígitos e ``. _ - @`` em minúsculas. O arroba continua
    permitido para que contas migradas do antigo campo de e-mail sigam
    funcionando sem intervenção manual.
    """

    normalized = (value or "").strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Usuário deve ter de 3 a 150 caracteres e usar apenas letras, "
            "números, ponto, hífen, sublinhado ou arroba"
        )
    return normalized


def bootstrap_initial_admin(session: Session, settings: Settings) -> User | None:
    """Create the first administrator once, without ever overwriting it."""

    existing_admin = session.scalar(
        select(User)
        .where(User.role == Role.ADMIN, User.password_hash.is_not(None))
        .order_by(User.created_at)
    )
    if existing_admin is not None:
        return existing_admin
    if not (
        settings.bootstrap_admin_name
        and settings.bootstrap_admin_username
        and settings.bootstrap_admin_password
    ):
        return None
    try:
        normalized_username = normalize_username(settings.bootstrap_admin_username)
    except ValueError as error:
        raise ValueError(f"BOOTSTRAP_ADMIN_USERNAME inválido: {error}") from error
    admin = session.scalar(select(User).where(User.username == normalized_username))
    if admin is None:
        admin = User(
            username=normalized_username,
            display_name=settings.bootstrap_admin_name.strip(),
            role=Role.ADMIN,
            active=True,
            password_hash=hash_password(settings.bootstrap_admin_password),
            must_change_password=True,
        )
        session.add(admin)
    else:
        admin.role = Role.ADMIN
        admin.active = True
        admin.archived_at = None
        admin.password_hash = hash_password(settings.bootstrap_admin_password)
        admin.must_change_password = True
    session.flush()
    return admin


def create_auth_session(
    session: Session,
    user: User,
    settings: Settings,
    *,
    audience: SessionAudience = "local",
) -> LoginResult:
    token = secrets.token_urlsafe(48)
    expires_at = utcnow() + timedelta(hours=settings.session_hours)
    auth_session = AuthSession(
        user_id=user.id,
        token_hash=_session_token_hash(token, audience),
        session_version=user.session_version,
        expires_at=expires_at,
    )
    session.add(auth_session)
    session.flush()
    return LoginResult(user=user, token=token, expires_at=expires_at)


def login_with_password(
    session: Session,
    *,
    username: str,
    password: str,
    settings: Settings,
) -> LoginResult:
    normalized = (username or "").strip().lower()
    user = session.scalar(select(User).where(func.lower(User.username) == normalized))
    now = utcnow()
    if user is None:
        # Argon2 work reduces user-enumeration timing differences.
        PASSWORD_HASHER.hash(password or "invalid-password-0")
        raise AuthenticationError(GENERIC_LOGIN_ERROR)
    locked_until = _aware(user.locked_until)
    if locked_until is not None and locked_until > now:
        raise AuthenticationError(GENERIC_LOGIN_ERROR)
    valid = verify_password(user.password_hash, password)
    if not valid or not user.active or user.archived_at is not None:
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= settings.login_max_attempts:
            user.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
            user.failed_login_attempts = 0
        session.flush()
        raise AuthenticationError(GENERIC_LOGIN_ERROR)
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    if PASSWORD_HASHER.check_needs_rehash(user.password_hash or ""):
        user.password_hash = PASSWORD_HASHER.hash(password)
    return create_auth_session(session, user, settings)


def _session_token_hash(token: str, audience: SessionAudience) -> str:
    if audience not in {"local", "demo"}:
        raise ValueError("Audiência de sessão inválida")
    material = token if audience == "local" else f"demo:{token}"
    return sha256(material.encode("utf-8")).hexdigest()


def resolve_auth_session(
    session: Session,
    token: str | None,
    *,
    audience: SessionAudience = "local",
) -> User | None:
    normalized_token = valid_session_token(token)
    if normalized_token is None:
        return None
    token_hash = _session_token_hash(normalized_token, audience)
    auth_session = session.scalar(
        select(AuthSession).where(AuthSession.token_hash == token_hash)
    )
    if auth_session is None or auth_session.revoked_at is not None:
        return None
    now = utcnow()
    if (_aware(auth_session.expires_at) or now) <= now:
        auth_session.revoked_at = now
        return None
    user = session.get(User, auth_session.user_id)
    if (
        user is None
        or not user.active
        or user.archived_at is not None
        or auth_session.session_version != user.session_version
    ):
        auth_session.revoked_at = now
        return None
    auth_session.last_seen_at = now
    return user


def revoke_session(
    session: Session,
    token: str | None,
    *,
    audience: SessionAudience = "local",
) -> None:
    normalized_token = valid_session_token(token)
    if normalized_token is None:
        return
    token_hash = _session_token_hash(normalized_token, audience)
    session.execute(
        update(AuthSession)
        .where(AuthSession.token_hash == token_hash, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def revoke_all_user_sessions(session: Session, user: User) -> None:
    user.session_version += 1
    session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def change_password(
    session: Session,
    *,
    user: User,
    current_password: str,
    new_password: str,
) -> None:
    if not verify_password(user.password_hash, current_password):
        raise AuthenticationError("Não foi possível alterar a senha")
    if current_password == new_password:
        raise ValueError("A nova senha deve ser diferente da atual")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    revoke_all_user_sessions(session, user)
    session.flush()


def set_temporary_password(session: Session, user: User, password: str) -> None:
    user.password_hash = hash_password(password)
    user.must_change_password = True
    revoke_all_user_sessions(session, user)
    session.flush()


__all__ = [
    "GENERIC_LOGIN_ERROR",
    "AuthenticationError",
    "LoginResult",
    "bootstrap_initial_admin",
    "change_password",
    "create_auth_session",
    "generate_temporary_password",
    "hash_password",
    "login_with_password",
    "normalize_username",
    "resolve_auth_session",
    "revoke_all_user_sessions",
    "revoke_session",
    "set_temporary_password",
    "validate_password",
    "verify_password",
]
