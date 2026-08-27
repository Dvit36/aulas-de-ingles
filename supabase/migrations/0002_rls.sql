-- Row Level Security. Defesa adicional: a camada de serviço também valida o
-- ator e o recurso. Aqui o banco garante que nem um bug na aplicação nem uma
-- consulta forjada alcancem dado de outro aluno.

-- is_admin() é SECURITY DEFINER de propósito: ler public.profiles de dentro de
-- uma política sobre public.profiles causaria recursão infinita de RLS.
-- search_path fixo impede sequestro por tabela homônima.
create or replace function public.is_admin()
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select exists (
        select 1
        from public.profiles
        where id = auth.uid()
          and role = 'admin'
          and active
          and archived_at is null
    );
$$;

revoke execute on function public.is_admin() from public;
grant execute on function public.is_admin() to authenticated;

-- O aluno não pode se promover. Papel e estado só mudam por administrador,
-- inclusive quando a linha é a dele mesmo.
create or replace function public.proteger_campos_de_perfil()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    if public.is_admin() then
        return new;
    end if;
    if new.role is distinct from old.role then
        raise exception 'somente administrador altera o papel';
    end if;
    if new.active is distinct from old.active
       or new.archived_at is distinct from old.archived_at then
        raise exception 'somente administrador altera o estado da conta';
    end if;
    if new.id is distinct from old.id then
        raise exception 'identidade do perfil é imutável';
    end if;
    return new;
end;
$$;

create trigger profiles_protege_papel
    before update on public.profiles
    for each row execute function public.proteger_campos_de_perfil();

alter table public.profiles            enable row level security;
alter table public.activities          enable row level security;
alter table public.resources           enable row level security;
alter table public.goal_configuration  enable row level security;
alter table public.submissions         enable row level security;
alter table public.submission_files    enable row level security;
alter table public.rule_checks         enable row level security;
alter table public.duplicate_matches   enable row level security;
alter table public.approved_evidence   enable row level security;
alter table public.lesson_units        enable row level security;
alter table public.lesson_batches      enable row level security;
alter table public.lesson_batch_units  enable row level security;
alter table public.ledger_transactions enable row level security;
alter table public.audit_logs          enable row level security;
alter table public.storage_orphans     enable row level security;

-- ------------------------------------------------------------------ perfis
-- O aluno enxerga o próprio perfil. O leaderboard usa uma view controlada.
create policy perfil_proprio_leitura on public.profiles
    for select using (id = auth.uid() or public.is_admin());
create policy perfil_proprio_edicao on public.profiles
    for update using (id = auth.uid() or public.is_admin())
    with check (id = auth.uid() or public.is_admin());
create policy perfil_admin_insere on public.profiles
    for insert with check (public.is_admin());
create policy perfil_admin_remove on public.profiles
    for delete using (public.is_admin());

-- ---------------------------------------------------------------- catálogo
-- Conteúdo comum: todo autenticado lê, só administrador escreve.
create policy atividades_leitura on public.activities
    for select using (auth.uid() is not null);
create policy atividades_escrita on public.activities
    for all using (public.is_admin()) with check (public.is_admin());

create policy recursos_leitura on public.resources
    for select using (auth.uid() is not null and (active or public.is_admin()));
create policy recursos_escrita on public.resources
    for all using (public.is_admin()) with check (public.is_admin());

create policy meta_leitura on public.goal_configuration
    for select using (auth.uid() is not null);
create policy meta_escrita on public.goal_configuration
    for all using (public.is_admin()) with check (public.is_admin());

-- -------------------------------------------------------------- submissões
-- with check impede o clássico: inserir linha carimbando outro student_id.
create policy submissoes_leitura on public.submissions
    for select using (student_id = auth.uid() or public.is_admin());
create policy submissoes_insercao on public.submissions
    for insert with check (student_id = auth.uid());
create policy submissoes_edicao_admin on public.submissions
    for update using (public.is_admin()) with check (public.is_admin());
create policy submissoes_cancelamento on public.submissions
    for update using (student_id = auth.uid() and status in ('processing', 'needs_review'))
    with check (student_id = auth.uid());

create policy arquivos_leitura on public.submission_files
    for select using (student_id = auth.uid() or public.is_admin());
create policy arquivos_insercao on public.submission_files
    for insert with check (student_id = auth.uid());
create policy arquivos_admin on public.submission_files
    for all using (public.is_admin()) with check (public.is_admin());

-- Tabelas derivadas seguem o dono da submissão.
create policy checks_leitura on public.rule_checks
    for select using (
        public.is_admin() or exists (
            select 1 from public.submissions s
            where s.id = rule_checks.submission_id and s.student_id = auth.uid()
        )
    );
create policy checks_admin on public.rule_checks
    for all using (public.is_admin()) with check (public.is_admin());

create policy duplicatas_leitura on public.duplicate_matches
    for select using (
        public.is_admin() or exists (
            select 1 from public.submissions s
            where s.id = duplicate_matches.submission_id and s.student_id = auth.uid()
        )
    );
create policy duplicatas_admin on public.duplicate_matches
    for all using (public.is_admin()) with check (public.is_admin());

-- Evidência aprovada é global por checksum: só administrador enxerga, senão
-- um aluno descobriria o que outro enviou testando hashes.
create policy evidencia_admin on public.approved_evidence
    for all using (public.is_admin()) with check (public.is_admin());

-- --------------------------------------------------------------- pontuação
create policy unidades_leitura on public.lesson_units
    for select using (student_id = auth.uid() or public.is_admin());
create policy unidades_admin on public.lesson_units
    for all using (public.is_admin()) with check (public.is_admin());

create policy lotes_leitura on public.lesson_batches
    for select using (student_id = auth.uid() or public.is_admin());
create policy lotes_admin on public.lesson_batches
    for all using (public.is_admin()) with check (public.is_admin());

create policy lote_unidades_leitura on public.lesson_batch_units
    for select using (
        public.is_admin() or exists (
            select 1 from public.lesson_units u
            where u.id = lesson_batch_units.unit_id and u.student_id = auth.uid()
        )
    );
create policy lote_unidades_admin on public.lesson_batch_units
    for all using (public.is_admin()) with check (public.is_admin());

-- O leaderboard precisa da pontuação de todos; por isso a leitura do ledger
-- é ampla para autenticados. Escrita nunca: só administrador lança, e os
-- gatilhos impedem update e delete inclusive para ele.
create policy ledger_leitura on public.ledger_transactions
    for select using (auth.uid() is not null);
create policy ledger_insercao_admin on public.ledger_transactions
    for insert with check (public.is_admin());

-- ------------------------------------------------------- auditoria e órfãos
create policy auditoria_admin on public.audit_logs
    for all using (public.is_admin()) with check (public.is_admin());
create policy orfaos_admin on public.storage_orphans
    for all using (public.is_admin()) with check (public.is_admin());
