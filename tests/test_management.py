from __future__ import annotations

from uuid import uuid4

from inspect import signature

import pytest
from sqlalchemy import select

from english_leaderboard.schema import (
    Activity,
    AuditLog,
    Role,
    Submission,
    SubmissionStatus,
    User,
)
from english_leaderboard.services import (
    archive_or_delete_activity,
    archive_or_delete_user,
    count_activity_references,
    create_activity,
    create_user_account,
    reset_user_password,
    review_submission,
    save_activity_changes,
    save_user,
    set_activity_active,
)
from english_leaderboard.supabase_auth import AuthError


def test_administrative_operations_do_not_accept_reason_parameters() -> None:
    """Decidir não exige redigir: a auditoria já guarda ator, ação e antes/depois.

    `create_points_adjustment` saiu desta lista quando passou a exigir motivo,
    e a exceção é de natureza, não de conveniência. Nas operações acima o
    texto seria justificativa burocrática, lida por ninguém. No lançamento
    manual ele é **conteúdo**: aparece na tela do aluno, ao lado dos pontos que
    apareceram do nada. Ver `test_lancamento_manual.py`, que trava o outro
    lado — motivo em branco é recusado.
    """

    for operation in (
        review_submission,
        save_activity_changes,
        save_user,
        reset_user_password,
        archive_or_delete_user,
        archive_or_delete_activity,
        create_activity,
        create_user_account,
    ):
        assert "reason" not in signature(operation).parameters


class ContasFalsas:
    """Duplo do Supabase Auth: registra o que foi pedido, sem rede."""

    def __init__(self) -> None:
        self.criadas: dict[str, str] = {}
        self.desativadas: list[str] = []
        self.reativadas: list[str] = []
        self.removidas: list[str] = []
        self.senhas_redefinidas: list[str] = []
        self.renomeadas: list[tuple[str, str]] = []
        self.falhar_ao_renomear = False

    def criar(self, *, username: str, display_name: str) -> tuple[str, str]:
        identificador = str(uuid4())
        self.criadas[identificador] = username
        return identificador, "senha-temporaria-16x"

    def redefinir_senha(self, user_id: str) -> str:
        self.senhas_redefinidas.append(user_id)
        return "outra-senha-temporaria"

    def atualizar_username(self, user_id: str, username: str) -> str:
        if self.falhar_ao_renomear:
            raise AuthError("Supabase Auth recusou a troca de endereço")
        self.renomeadas.append((user_id, username))
        return f"{username}@exemplo.invalid"

    def desativar(self, user_id: str) -> None:
        self.desativadas.append(user_id)

    def reativar(self, user_id: str) -> None:
        self.reativadas.append(user_id)

    def remover(self, user_id: str) -> None:
        self.removidas.append(user_id)


def test_user_create_disable_reactivate_and_delete(session, users) -> None:
    admin = users[Role.ADMIN]
    contas = ContasFalsas()
    account, temporary_password = create_user_account(
        session,
        actor=admin,
        contas=contas,
        username="new.student",
        display_name="New Student",
    )
    session.commit()

    # O id do perfil é o da conta no Auth: é o que amarra os dois lados.
    assert account.id in contas.criadas
    assert contas.criadas[account.id] == "new.student"
    assert temporary_password == "senha-temporaria-16x"
    # A aplicação não guarda senha nem hash: essa autoridade é do Auth.
    assert not hasattr(account, "password_hash")

    # A sessão é criada com `expire_on_commit=False`, então o commit acima
    # deixaria `account` intacto na memória, com o enum ainda anexado a `role`.
    # Em produção quem edita é uma tela montada a partir de uma consulta nova, e
    # aí `role` volta do banco como `str`. Sem expirar, este teste passava por
    # cima de um AttributeError que derrubava a edição de usuário no app.
    session.expire_all()

    save_user(
        session,
        actor=admin,
        username=account.username,
        display_name=account.display_name,
        role=Role.STUDENT,
        active=False,
        user_id=account.id,
        contas=contas,
    )
    assert account.active is False
    # Desativar só o perfil deixaria a sessão em curso válida até expirar.
    assert contas.desativadas == [account.id]

    save_user(
        session,
        actor=admin,
        username=account.username,
        display_name=account.display_name,
        role=Role.STUDENT,
        active=True,
        user_id=account.id,
        contas=contas,
    )
    assert account.active is True
    assert (
        archive_or_delete_user(
            session,
            actor=admin,
            user_id=account.id,
            contas=contas,
        )
        == "deleted"
    )
    # Sem conta removida no Auth, o usuário continuaria conseguindo entrar.
    assert contas.removidas == [account.id]


def test_editar_usuario_carregado_do_banco_registra_o_papel_anterior(
    session, users
) -> None:
    """`User.role` é StrEnum sobre coluna `Text`: do banco ele vem como `str`.

    `save_user` lia `user.role.value` para montar o `before` da auditoria —
    uma leitura anterior a qualquer atribuição, ou seja, o valor que estava no
    banco. Com o objeto vindo de uma consulta isso é AttributeError, e a edição
    de usuário pela tela de gestão caía inteira.

    A suíte não pegava porque `create_session_factory` usa
    `expire_on_commit=False`: o objeto sobrevivia ao commit com o enum
    anexado, e `.value` funcionava. `expire_all` é o que força a releitura e
    coloca o teste do lado certo da fronteira onde o tipo muda.
    """

    admin = users[Role.ADMIN]
    alvo = users[Role.STUDENT]
    contas = ContasFalsas()
    session.commit()

    session.expire_all()
    recarregado = session.get(User, alvo.id)
    assert isinstance(recarregado.role, str)
    assert not hasattr(recarregado.role, "value"), (
        "o papel veio do banco como enum: o teste deixou de exercitar a "
        "fronteira onde o tipo muda, e o defeito passaria batido de novo"
    )

    save_user(
        session,
        actor=admin,
        username=recarregado.username,
        display_name=recarregado.display_name,
        role=Role.ADMIN,
        active=True,
        user_id=recarregado.id,
        contas=contas,
    )
    session.flush()

    log = session.scalar(
        select(AuditLog)
        .where(AuditLog.entity_type == "user", AuditLog.entity_id == alvo.id)
        .order_by(AuditLog.created_at.desc())
    )
    assert log.action == "user_updated"
    # A auditoria precisa dizer de onde para onde: um `before` perdido é uma
    # promoção a administrador sem rastro do papel anterior.
    assert log.before_json["role"] == "student"
    assert log.after_json["role"] == "admin"


def test_activity_delete_is_logical_when_history_exists(session, users) -> None:
    admin = users[Role.ADMIN]
    student = users[Role.STUDENT]
    activity = create_activity(
        session,
        actor=admin,
        code="custom_activity",
        name="Atividade personalizada",
        points=9,
    )
    session.flush()
    submission = Submission(
        student_id=student.id,
        activity_id=activity.id,
        status=SubmissionStatus.REJECTED,
        rule_snapshot_json={"activity_name": activity.name, "points": 9},
    )
    session.add(submission)
    session.commit()
    result = archive_or_delete_activity(
        session,
        actor=admin,
        activity_id=activity.id,
    )
    session.commit()
    assert result == "archived"
    assert activity.active is False
    assert activity.archived_at is not None
    historical = session.get(Submission, submission.id)
    assert historical.activity.name == "Atividade personalizada"


def test_unused_activity_can_be_deleted_and_inactive_can_be_reactivated(
    session, users
) -> None:
    admin = users[Role.ADMIN]
    activity = create_activity(
        session,
        actor=admin,
        code="temporary_activity",
        name="Atividade temporária",
        points=8,
    )
    set_activity_active(
        session,
        actor=admin,
        activity_id=activity.id,
        active=False,
    )
    assert activity.active is False
    set_activity_active(
        session,
        actor=admin,
        activity_id=activity.id,
        active=True,
    )
    assert activity.active is True
    assert (
        archive_or_delete_activity(
            session,
            actor=admin,
            activity_id=activity.id,
        )
        == "deleted"
    )
    session.flush()
    assert session.get(Activity, activity.id) is None


def test_core_lesson_points_and_threshold_are_not_editable(session, users) -> None:
    activity = session.scalar(
        select(Activity).where(Activity.code == "duolingo_beconfident")
    )
    with pytest.raises(ValueError, match="política fixa"):
        save_activity_changes(
            session,
            actor=users[Role.ADMIN],
            activity_id=activity.id,
            name=activity.name,
            points=99,
            unit_threshold=1,
            active=True,
        )


def test_core_lesson_activity_cannot_be_deleted(session, users) -> None:
    activity = session.scalar(
        select(Activity).where(Activity.code == "duolingo_beconfident")
    )
    with pytest.raises(ValueError, match="não pode ser excluída"):
        archive_or_delete_activity(
            session,
            actor=users[Role.ADMIN],
            activity_id=activity.id,
        )


def test_activity_reference_count_drives_the_delete_confirmation(
    session, users
) -> None:
    admin = users[Role.ADMIN]
    student = users[Role.STUDENT]
    unused = create_activity(
        session,
        actor=admin,
        code="unused_activity",
        name="Atividade sem uso",
        points=8,
    )
    used = create_activity(
        session,
        actor=admin,
        code="used_activity",
        name="Atividade com histórico",
        points=8,
    )
    session.flush()
    session.add(
        Submission(
            student_id=student.id,
            activity_id=used.id,
            status=SubmissionStatus.REJECTED,
            rule_snapshot_json={"activity_name": used.name, "points": 8},
        )
    )
    session.commit()
    assert count_activity_references(session, unused.id) == 0
    assert count_activity_references(session, used.id) == 1


def test_weekly_goal_is_admin_only_validated_and_audited(session, users) -> None:
    from english_leaderboard.authz import AuthorizationError
    from english_leaderboard.schema import AuditLog
    from english_leaderboard.services import (
        get_goal_configuration,
        save_goal_configuration,
    )

    admin = users[Role.ADMIN]
    student = users[Role.STUDENT]

    assert get_goal_configuration(session).weekly_lesson_goal == 5

    with pytest.raises(AuthorizationError):
        save_goal_configuration(session, actor=student, weekly_lesson_goal=10)
    session.rollback()

    for invalid in (0, -3, 201):
        with pytest.raises(ValueError):
            save_goal_configuration(session, actor=admin, weekly_lesson_goal=invalid)
        session.rollback()

    configuration = save_goal_configuration(
        session, actor=admin, weekly_lesson_goal=12
    )
    session.commit()

    assert configuration.weekly_lesson_goal == 12
    assert configuration.updated_by_id == admin.id
    assert get_goal_configuration(session).weekly_lesson_goal == 12
    entry = session.scalar(
        select(AuditLog).where(AuditLog.action == "goal_configuration_updated")
    )
    assert entry is not None
    assert entry.before_json == {"weekly_lesson_goal": 5}
    assert entry.after_json == {"weekly_lesson_goal": 12}


def test_resources_reject_dangerous_links_and_keep_admin_order(session, users) -> None:
    from english_leaderboard.authz import AuthorizationError
    from english_leaderboard.services import (
        list_resources,
        normalize_resource_url,
        replace_resources,
    )

    admin = users[Role.ADMIN]
    student = users[Role.STUDENT]

    # A lista é renderizada como HTML: só http/https podem passar.
    for hostile in ("javascript:alert(1)", "data:text/html,<script>", "  ", "ftp://x"):
        with pytest.raises(ValueError):
            normalize_resource_url(hostile)

    with pytest.raises(AuthorizationError):
        replace_resources(session, actor=student, entries=[])
    session.rollback()

    with pytest.raises(ValueError):
        replace_resources(
            session,
            actor=admin,
            entries=[{"title": "Sem link", "url": ""}],
        )
    session.rollback()

    with pytest.raises(ValueError):
        replace_resources(
            session,
            actor=admin,
            entries=[{"title": "", "url": "https://exemplo.org"}],
        )
    session.rollback()

    replace_resources(
        session,
        actor=admin,
        entries=[
            {"title": "Segundo", "url": "https://b.example", "description": "b"},
            {"title": "Primeiro", "url": "https://a.example", "description": "a"},
            {"title": "Oculto", "url": "https://c.example", "active": False},
        ],
    )
    session.commit()

    # A ordem recebida vira a posição; nada é reordenado por título.
    ativos = list_resources(session)
    assert [item.title for item in ativos] == ["Segundo", "Primeiro"]
    assert [item.position for item in ativos] == [1, 2]
    todos = list_resources(session, include_inactive=True)
    assert [item.title for item in todos] == ["Segundo", "Primeiro", "Oculto"]


def test_replacing_resources_is_a_full_rewrite(session, users) -> None:
    from english_leaderboard.schema import AuditLog
    from english_leaderboard.services import list_resources, replace_resources

    admin = users[Role.ADMIN]
    replace_resources(
        session,
        actor=admin,
        entries=[{"title": "Antigo", "url": "https://antigo.example"}],
    )
    session.commit()
    replace_resources(
        session,
        actor=admin,
        entries=[{"title": "Novo", "url": "https://novo.example"}],
    )
    session.commit()

    assert [item.title for item in list_resources(session)] == ["Novo"]
    entry = session.scalar(
        select(AuditLog)
        .where(AuditLog.action == "resources_replaced")
        .order_by(AuditLog.created_at.desc())
    )
    assert entry is not None
    assert entry.after_json["titles"] == ["Novo"]


# ------------------------------------------- username: perfil e Auth juntos

def test_renaming_a_user_also_moves_the_auth_account(session, users) -> None:
    """A tela lê de `profiles`; o login lê do Auth. Renomear só um separa os dois.

    Era o defeito: `save_user` gravava `profiles.username` e não tocava a
    conta, então o login continuava aceitando só o nome antigo.
    """

    admin = users[Role.ADMIN]
    contas = ContasFalsas()
    alvo = users[Role.STUDENT]

    save_user(
        session,
        actor=admin,
        user_id=alvo.id,
        username="nome.novo",
        display_name=alvo.display_name,
        role=Role.STUDENT,
        active=True,
        contas=contas,
    )
    session.commit()

    assert contas.renomeadas == [(alvo.id, "nome.novo")]
    assert session.get(User, alvo.id).username == "nome.novo"


def test_editing_without_touching_the_username_leaves_the_auth_alone(
    session, users
) -> None:
    """Só o nome de exibição mudou: não há motivo para falar com o Auth."""

    admin = users[Role.ADMIN]
    contas = ContasFalsas()
    alvo = users[Role.STUDENT]

    save_user(
        session,
        actor=admin,
        user_id=alvo.id,
        username=alvo.username,
        display_name="Outro Nome",
        role=Role.STUDENT,
        active=True,
        contas=contas,
    )
    session.commit()

    assert contas.renomeadas == []


def test_a_rename_the_auth_refuses_does_not_stick_in_the_profile(
    session, users
) -> None:
    """O rollback é a compensação: sem ele, voltaríamos à divergência.

    O perfil muda dentro da transação e o Auth logo depois. Se o Auth recusa,
    a exceção sobe e o `rollback` de quem chama desfaz o perfil — não existe
    escrita compensatória que possa, ela mesma, falhar.
    """

    admin = users[Role.ADMIN]
    contas = ContasFalsas()
    contas.falhar_ao_renomear = True
    alvo = users[Role.STUDENT]
    antes = alvo.username

    with pytest.raises(AuthError):
        save_user(
            session,
            actor=admin,
            user_id=alvo.id,
            username="nome.que.nao.vai.valer",
            display_name=alvo.display_name,
            role=Role.STUDENT,
            active=True,
            contas=contas,
        )
    session.rollback()

    assert session.get(User, alvo.id).username == antes
    assert contas.renomeadas == []


def test_reactivating_a_user_lifts_the_ban_at_the_auth(session, users) -> None:
    """Desativar bane no Auth; reativar tem de levantar o ban.

    Sem o par, o perfil volta a `active`, a tela mostra o aluno ativo e o
    login segue recusando — a mesma divergência do username, mas sem contorno:
    não há nome antigo com que entrar.
    """

    admin = users[Role.ADMIN]
    contas = ContasFalsas()
    alvo = users[Role.STUDENT]

    def salvar(ativo: bool) -> None:
        save_user(
            session,
            actor=admin,
            user_id=alvo.id,
            username=alvo.username,
            display_name=alvo.display_name,
            role=Role.STUDENT,
            active=ativo,
            contas=contas,
        )
        session.commit()

    salvar(False)
    assert contas.desativadas == [alvo.id]
    assert contas.reativadas == []

    salvar(True)
    assert contas.reativadas == [alvo.id]
    assert session.get(User, alvo.id).active is True


def test_a_role_change_alone_does_not_touch_the_ban(session, users) -> None:
    """Quem já estava ativo não precisa ser desbanido ao virar admin."""

    admin = users[Role.ADMIN]
    contas = ContasFalsas()
    alvo = users[Role.STUDENT]

    save_user(
        session,
        actor=admin,
        user_id=alvo.id,
        username=alvo.username,
        display_name=alvo.display_name,
        role=Role.ADMIN,
        active=True,
        contas=contas,
    )
    session.commit()

    assert contas.reativadas == []
    assert contas.desativadas == []


def test_renaming_without_the_accounts_facade_fails_loudly_on_postgres(
    session, users, monkeypatch
) -> None:
    """Sem a fachada não há como mover a conta; gravar só o perfil divergiria.

    O chamador de hoje sempre passa `contas`, mas um futuro não tem como saber
    disso — e o modo de falha seria silencioso, que é justamente o que tornou
    estes defeitos difíceis de achar.
    """

    import english_leaderboard.services as servicos

    monkeypatch.setattr(servicos, "exige_conta_no_auth", lambda _s: True)
    alvo = users[Role.STUDENT]
    antes = alvo.username

    with pytest.raises(ValueError, match="fachada de contas"):
        save_user(
            session,
            actor=users[Role.ADMIN],
            user_id=alvo.id,
            username="nome.sem.fachada",
            display_name=alvo.display_name,
            role=Role.STUDENT,
            active=True,
        )
    session.rollback()

    assert session.get(User, alvo.id).username == antes


def test_renaming_without_the_facade_is_fine_where_there_is_no_auth(
    session, users
) -> None:
    """No SQLite não existe schema `auth`: o perfil solto é legítimo.

    Exigir a fachada aqui quebraria desenvolvimento e a suíte para proteger um
    caso que só existe no PostgreSQL.
    """

    alvo = users[Role.STUDENT]

    save_user(
        session,
        actor=users[Role.ADMIN],
        user_id=alvo.id,
        username="nome.local",
        display_name=alvo.display_name,
        role=Role.STUDENT,
        active=True,
    )
    session.commit()

    assert session.get(User, alvo.id).username == "nome.local"
