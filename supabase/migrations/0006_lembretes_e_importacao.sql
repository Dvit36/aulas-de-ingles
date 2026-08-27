-- Tabelas que o schema inicial não previu e a aplicação ainda usa.
--
-- Descobertas ao apontar os serviços para o PostgreSQL: a tela de lembretes
-- do administrador e a importação da planilha legada continuam no produto, e
-- sem estas tabelas as duas quebrariam na primeira consulta. Deixar de criá-las
-- seria remover funcionalidade em silêncio.
--
-- Todas são administrativas: nenhum aluno lê ou escreve nelas.

-- ``reminders_enabled`` mora no perfil porque é preferência do aluno sobre o
-- próprio contato, não configuração global.
alter table public.profiles
    add column if not exists reminders_enabled boolean not null default true;

-- ------------------------------------------------------------- lembretes
create table if not exists public.reminder_configuration (
    id               integer primary key default 1 check (id = 1),
    enabled          boolean     not null default false,
    frequency        text        not null default 'weekly'
                                 check (frequency in ('daily', 'weekly', 'monthly')),
    weekday          integer     not null default 1 check (weekday between 0 and 6),
    send_hour        integer     not null default 9 check (send_hour between 0 and 23),
    timezone_name    text        not null default 'America/Sao_Paulo',
    inactive_days    integer     not null default 7 check (inactive_days > 0),
    subject_template text        not null default 'Lembrete de atividades de inglês',
    body_template    text        not null default '',
    audience         text        not null default 'inactive_students',
    updated_by_id    uuid        references public.profiles (id) on delete set null,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

do $$
begin
    if not exists (select 1 from pg_type where typname = 'email_attempt_status') then
        create type email_attempt_status as enum ('pending', 'sent', 'failed', 'skipped');
    end if;
end;
$$;

create table if not exists public.email_attempts (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid references public.profiles (id) on delete set null,
    recipient_email text        not null default '',
    subject         text        not null,
    body            text        not null,
    status          email_attempt_status not null default 'pending',
    -- Chave de deduplicação: impede o mesmo lembrete sair duas vezes quando
    -- o agendador roda de novo depois de uma falha parcial.
    dedupe_key      text        not null unique,
    dry_run         boolean     not null default true,
    attempt_count   integer     not null default 0,
    last_error      text,
    scheduled_for   timestamptz not null,
    sent_at         timestamptz,
    created_at      timestamptz not null default now()
);

create index if not exists email_attempts_usuario_criado
    on public.email_attempts (user_id, created_at desc);
create index if not exists email_attempts_agendado
    on public.email_attempts (scheduled_for) where sent_at is null;

-- ------------------------------------------------------------ importação
create table if not exists public.import_runs (
    id                 uuid primary key default gen_random_uuid(),
    namespace          text        not null,
    source_path        text        not null,
    source_sha256      text        not null,
    started_at         timestamptz not null default now(),
    finished_at        timestamptz,
    imported_count     integer     not null default 0,
    skipped_count      integer     not null default 0,
    inconsistent_count integer     not null default 0,
    report_json        jsonb       not null default '{}'::jsonb
);

create index if not exists import_runs_namespace on public.import_runs (namespace);

-- ``external_key`` único é o que torna a importação idempotente: reimportar a
-- mesma planilha não duplica lançamento no ledger.
create table if not exists public.import_records (
    id                    uuid primary key default gen_random_uuid(),
    run_id                uuid not null references public.import_runs (id) on delete cascade,
    external_key          text not null unique,
    student_id            uuid not null references public.profiles (id) on delete cascade,
    ledger_transaction_id uuid not null unique
                               references public.ledger_transactions (id) on delete restrict,
    source_json           jsonb       not null default '{}'::jsonb,
    created_at            timestamptz not null default now()
);

-- ------------------------------------------------------------------- RLS
alter table public.reminder_configuration enable row level security;
alter table public.email_attempts         enable row level security;
alter table public.import_runs            enable row level security;
alter table public.import_records         enable row level security;

-- Nenhuma destas é do aluno. E-mails de terceiros e trilha de importação só
-- o administrador enxerga.
create policy lembretes_config_admin on public.reminder_configuration
    for all using (public.is_admin()) with check (public.is_admin());
create policy lembretes_tentativas_admin on public.email_attempts
    for all using (public.is_admin()) with check (public.is_admin());
create policy importacao_execucoes_admin on public.import_runs
    for all using (public.is_admin()) with check (public.is_admin());
create policy importacao_registros_admin on public.import_records
    for all using (public.is_admin()) with check (public.is_admin());

-- Privilégio é camada separada da RLS: sem GRANT o erro vem antes da política.
grant select, insert, update, delete on public.reminder_configuration to authenticated;
grant select, insert, update, delete on public.email_attempts to authenticated;
grant select, insert, update, delete on public.import_runs to authenticated;
grant select, insert, update, delete on public.import_records to authenticated;

revoke all on public.reminder_configuration, public.email_attempts,
              public.import_runs, public.import_records from anon;
