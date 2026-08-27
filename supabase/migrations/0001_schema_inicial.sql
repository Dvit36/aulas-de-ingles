-- Schema oficial: identidade no Supabase Auth, dados no PostgreSQL,
-- binários no Supabase Storage (aqui só a referência e os metadados).
--
-- Nada de senha, hash, refresh token ou sessão própria: essas coisas
-- pertencem exclusivamente ao Supabase Auth.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- identidade
-- O UUID de auth.users é a identidade estável. O perfil guarda apenas o que
-- é do domínio; o login por nome de usuário é resolvido pela aplicação, que
-- monta usuario@dominio antes de falar com o Auth.
create table public.profiles (
    id            uuid primary key references auth.users (id) on delete cascade,
    username      text        not null,
    display_name  text        not null,
    role          text        not null default 'student'
                              check (role in ('student', 'admin')),
    active        boolean     not null default true,
    archived_at   timestamptz,
    last_login_at timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    constraint profiles_username_formato
        check (username ~ '^[a-z0-9][a-z0-9._@-]{2,149}$')
);

create unique index profiles_username_unico on public.profiles (lower(username));
create index profiles_role_ativo on public.profiles (role) where active;

-- ---------------------------------------------------------------- catálogo
create table public.activities (
    id                      uuid primary key default gen_random_uuid(),
    code                    text not null unique,
    name                    text not null,
    points                  integer not null check (points > 0),
    unit_threshold          integer not null default 1 check (unit_threshold > 0),
    requires_images         boolean not null default true,
    requires_summary        boolean not null default false,
    requires_title_or_url   boolean not null default false,
    summary_min_chars       integer not null default 0,
    content_review_required boolean not null default true,
    auto_approvable         boolean not null default false,
    active                  boolean not null default true,
    archived_at             timestamptz,
    config_json             jsonb not null default '{}'::jsonb,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now()
);

create table public.resources (
    id          uuid primary key default gen_random_uuid(),
    title       text not null,
    url         text not null check (url ~ '^https?://'),
    description text not null default '',
    position    integer not null default 0,
    active      boolean not null default true,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index resources_ordem on public.resources (position, title);

create table public.goal_configuration (
    id                 integer primary key default 1 check (id = 1),
    weekly_lesson_goal integer not null default 5 check (weekly_lesson_goal > 0),
    updated_by_id      uuid references public.profiles (id) on delete set null,
    updated_at         timestamptz not null default now()
);

-- ---------------------------------------------------------------- submissões
create type public.submission_status as enum (
    'processing', 'approved_auto', 'needs_review',
    'approved_manual', 'rejected', 'cancelled'
);

create table public.submissions (
    id                uuid primary key default gen_random_uuid(),
    student_id        uuid not null references public.profiles (id) on delete cascade,
    activity_id       uuid not null references public.activities (id) on delete restrict,
    status            public.submission_status not null default 'processing',
    received_at       timestamptz not null default now(),
    processed_at      timestamptz,
    decided_at        timestamptz,
    decided_by_id     uuid references public.profiles (id) on delete set null,
    title             text,
    url               text,
    summary           text,
    ocr_text          text,
    detected_platform text,
    confidence        double precision not null default 0,
    declared_units    integer not null default 0 check (declared_units >= 0),
    recognized_units  integer not null default 0 check (recognized_units >= 0),
    points_awarded    integer not null default 0,
    rule_snapshot     jsonb not null default '{}'::jsonb,
    review_note       text,
    version           integer not null default 1,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);
create index submissions_aluno_recebido on public.submissions (student_id, received_at desc);
create index submissions_status on public.submissions (status);

-- Metadados de arquivo. O binário vive no Supabase Storage; aqui fica só a
-- referência. Os três campos de storage deixam o provedor explícito em vez de
-- implícito no nome da coluna, para uma troca futura não exigir migração de
-- dados de novo.
create table public.submission_files (
    id               uuid primary key default gen_random_uuid(),
    submission_id    uuid not null references public.submissions (id) on delete cascade,
    student_id       uuid not null references public.profiles (id) on delete cascade,
    filename         text not null,
    storage_provider text not null default 'supabase',
    storage_bucket   text not null default 'student-files',
    storage_key      text not null,
    content_type     text not null,
    file_size        bigint not null check (file_size > 0),
    checksum_sha256  char(64) not null,
    phash            text,
    width            integer,
    height           integer,
    page_count       integer,
    ocr_text         text,
    position         integer not null default 0,
    created_at       timestamptz not null default now(),
    unique (storage_provider, storage_bucket, storage_key),
    -- A chave física precisa começar pelo dono: impede que uma linha aponte
    -- para a pasta de outro aluno mesmo se a aplicação errar. Espelha a
    -- política de storage.objects, que checa o mesmo segmento do path.
    constraint submission_files_prefixo_do_dono
        check (storage_key like 'students/' || student_id::text || '/%'),
    constraint submission_files_categoria_valida
        check (
            storage_key ~ ('^students/' || student_id::text ||
                           '/(uploads|activities|documents|processed)/[^/]+$')
        )
);
create index submission_files_aluno on public.submission_files (student_id);
create index submission_files_submissao on public.submission_files (submission_id, position);
create index submission_files_checksum on public.submission_files (checksum_sha256);

-- Objetos enviados ao Storage cujo registro falhou: fila de reconciliação
-- para não deixar órfão em silêncio.
create table public.storage_orphans (
    id               uuid primary key default gen_random_uuid(),
    storage_provider text not null default 'supabase',
    storage_bucket   text not null default 'student-files',
    storage_key      text not null,
    student_id       uuid references public.profiles (id) on delete set null,
    reason           text not null,
    resolved_at      timestamptz,
    created_at       timestamptz not null default now()
);
create index storage_orphans_pendentes on public.storage_orphans (created_at)
    where resolved_at is null;

create table public.rule_checks (
    id            uuid primary key default gen_random_uuid(),
    submission_id uuid not null references public.submissions (id) on delete cascade,
    name          text not null,
    outcome       text not null check (outcome in ('pass', 'fail', 'review')),
    required      boolean not null default true,
    score         double precision not null default 0,
    message       text,
    details       jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now()
);
create index rule_checks_submissao on public.rule_checks (submission_id);

create table public.duplicate_matches (
    id             uuid primary key default gen_random_uuid(),
    submission_id  uuid not null references public.submissions (id) on delete cascade,
    file_id        uuid references public.submission_files (id) on delete set null,
    matched_file_id uuid references public.submission_files (id) on delete set null,
    kind           text not null check (kind in ('exact', 'similar')),
    distance       integer,
    same_student   boolean not null default false,
    created_at     timestamptz not null default now()
);
create index duplicate_matches_submissao on public.duplicate_matches (submission_id);

-- Claim único de evidência aprovada: fecha corrida de reenvio idêntico.
create table public.approved_evidence (
    checksum_sha256 char(64) primary key,
    submission_id   uuid not null references public.submissions (id) on delete cascade,
    student_id      uuid not null references public.profiles (id) on delete cascade,
    created_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------- pontuação
create table public.lesson_units (
    id             uuid primary key default gen_random_uuid(),
    submission_id  uuid not null references public.submissions (id) on delete cascade,
    student_id     uuid not null references public.profiles (id) on delete cascade,
    activity_group text not null,
    unit_index     integer not null check (unit_index > 0),
    approved_at    timestamptz not null default now(),
    unique (submission_id, unit_index)
);
create index lesson_units_aluno_grupo on public.lesson_units (student_id, activity_group, approved_at);

create table public.lesson_batches (
    id             uuid primary key default gen_random_uuid(),
    student_id     uuid not null references public.profiles (id) on delete cascade,
    activity_group text not null,
    sequence       integer not null check (sequence > 0),
    created_at     timestamptz not null default now(),
    unique (student_id, activity_group, sequence)
);

create table public.lesson_batch_units (
    batch_id uuid not null references public.lesson_batches (id) on delete cascade,
    unit_id  uuid not null references public.lesson_units (id) on delete cascade,
    -- Uma unidade participa de no máximo um grupo premiado.
    primary key (unit_id)
);
create index lesson_batch_units_lote on public.lesson_batch_units (batch_id);

create type public.ledger_kind as enum (
    'direct_activity', 'lesson_batch', 'adjustment',
    'imported_daily_score', 'initial_balance', 'meeting'
);

create table public.ledger_transactions (
    id            uuid primary key default gen_random_uuid(),
    student_id    uuid not null references public.profiles (id) on delete restrict,
    points        integer not null,
    kind          public.ledger_kind not null,
    source_type   text not null,
    source_id     uuid,
    source_key    text not null unique,
    activity_id   uuid references public.activities (id) on delete set null,
    submission_id uuid references public.submissions (id) on delete set null,
    description   text,
    occurred_at   timestamptz not null default now(),
    created_by_id uuid references public.profiles (id) on delete set null,
    created_at    timestamptz not null default now()
);
create index ledger_aluno_data on public.ledger_transactions (student_id, occurred_at);

-- Imutabilidade do ledger: correção é lançamento compensatório, nunca
-- mutação silenciosa do histórico. Equivale aos gatilhos do SQLite legado.
create or replace function public.ledger_imutavel()
returns trigger
language plpgsql
as $$
begin
    raise exception 'ledger transactions are immutable';
end;
$$;

create trigger ledger_sem_update
    before update on public.ledger_transactions
    for each row execute function public.ledger_imutavel();

create trigger ledger_sem_delete
    before delete on public.ledger_transactions
    for each row execute function public.ledger_imutavel();

-- ---------------------------------------------------------------- auditoria
create table public.audit_logs (
    id          uuid primary key default gen_random_uuid(),
    actor_id    uuid references public.profiles (id) on delete set null,
    action      text not null,
    entity_type text not null,
    entity_id   text,
    before_json jsonb,
    after_json  jsonb,
    reason      text,
    created_at  timestamptz not null default now()
);
create index audit_logs_criado on public.audit_logs (created_at desc);
create index audit_logs_entidade on public.audit_logs (entity_type, entity_id);

-- updated_at automático onde a coluna existe.
create or replace function public.tocar_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

do $$
declare
    t text;
begin
    foreach t in array array[
        'profiles', 'activities', 'resources', 'goal_configuration', 'submissions'
    ] loop
        execute format(
            'create trigger %I_touch before update on public.%I
             for each row execute function public.tocar_updated_at()', t, t
        );
    end loop;
end;
$$;
