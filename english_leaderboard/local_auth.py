"""Local password authentication and revocable opaque sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import secrets
import string

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .config import Settings
from .models import AuthSession, Role, User, utcnow


PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
GENERIC_LOGIN_ERROR = "E-mail ou senha inválidos. Tente novamente."


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
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def validate_password(password: str) -> None:
    if len(password) < 10:
        raise ValueError("A senha deve ter ao menos 10 caracteres")
    if not any(character.isalpha() for character in password):
        raise ValueError("A senha deve conter letras")
    if not any(character.isdigit() for character in password):
        raise ValueError("A senha deve conter números")


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
    if length < 12:
        raise ValueError("Senha temporária deve ter ao menos 12 caracteres")
    alphabet = string.ascii_letters + string.digits + "!@#$%"
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        try:
            validate_password(candidate)
        except ValueError:
            continue
        return candidate


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
        and settings.bootstrap_admin_email
        and settings.bootstrap_admin_password
    ):
        return None
    normalized_email = settings.bootstrap_admin_email.strip().lower()
    if "@" not in normalized_email:
        raise ValueError("BOOTSTRAP_ADMIN_EMAIL inválido")
    admin = session.scalar(select(User).where(User.email == normalized_email))
    if admin is None:
        admin = User(
            email=normalized_email,
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
    session: Session, user: User, settings: Settings
) -> LoginResult:
    token = secrets.token_urlsafe(48)
    expires_at = utcnow() + timedelta(hours=settings.session_hours)
    auth_session = AuthSession(
        user_id=user.id,
        token_hash=sha256(token.encode("utf-8")).hexdigest(),
        session_version=user.session_version,
        expires_at=expires_at,
    )
    session.add(auth_session)
    session.flush()
    return LoginResult(user=user, token=token, expires_at=expires_at)


def login_with_password(
    session: Session,
    *,
    email: str,
    password: str,
    settings: Settings,
) -> LoginResult:
    normalized = email.strip().lower()
    user = session.scalar(select(User).where(func.lower(User.email) == normalized))
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


def resolve_auth_session(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    token_hash = sha256(token.encode("utf-8")).hexdigest()
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


def revoke_session(session: Session, token: str | None) -> None:
    if not token:
        return
    token_hash = sha256(token.encode("utf-8")).hexdigest()
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
    "AuthenticationError",
    "GENERIC_LOGIN_ERROR",
    "LoginResult",
    "bootstrap_initial_admin",
    "change_password",
    "create_auth_session",
    "generate_temporary_password",
    "hash_password",
    "login_with_password",
    "resolve_auth_session",
    "revoke_all_user_sessions",
    "revoke_session",
    "set_temporary_password",
    "validate_password",
    "verify_password",
]
