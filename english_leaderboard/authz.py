from __future__ import annotations

from .models import Role, Submission, User


class AuthorizationError(PermissionError):
    pass


def require_active(user: User) -> None:
    if not user.active or user.archived_at is not None:
        raise AuthorizationError("Usuário inativo")


def require_admin(user: User) -> None:
    require_active(user)
    if user.role != Role.ADMIN:
        raise AuthorizationError("Ação restrita a administrador")


def can_view_submission(user: User, submission: Submission) -> bool:
    return bool(
        user.active
        and user.archived_at is None
        and (user.role == Role.ADMIN or submission.student_id == user.id)
    )


def require_submission_access(user: User, submission: Submission) -> None:
    if not can_view_submission(user, submission):
        raise AuthorizationError("Submissão não pertence ao usuário")


def require_self_or_admin(actor: User, student_id: str) -> None:
    require_active(actor)
    if actor.role != Role.ADMIN and actor.id != student_id:
        raise AuthorizationError("Usuário não pode agir por outro aluno")
