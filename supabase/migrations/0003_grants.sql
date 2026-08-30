-- Privilégios de tabela. RLS filtra linhas, mas não concede acesso: sem GRANT
-- o papel authenticated recebe "permission denied" antes de qualquer política
-- ser avaliada. São camadas distintas e as duas precisam existir.

-- A aplicação é fechada: quem não autenticou não lê nada.
revoke all on all tables in schema public from anon;
revoke all on schema public from anon;

grant usage on schema public to authenticated;

-- O portão real é a RLS; o GRANT só habilita o verbo. Ledger fica de fora
-- porque tem regra própria logo abaixo.
do $$
declare
    t text;
begin
    foreach t in array array[
        'profiles', 'activities', 'resources', 'goal_configuration',
        'submissions', 'submission_files', 'rule_checks', 'duplicate_matches',
        'approved_evidence', 'lesson_units', 'lesson_batches',
        'lesson_batch_units', 'audit_logs', 'storage_orphans'
    ] loop
        execute format(
            'grant select, insert, update, delete on public.%I to authenticated', t
        );
    end loop;
end;
$$;

-- Ledger nunca recebe update nem delete, nem para administrador. Os gatilhos
-- já bloqueiam; a ausência do privilégio recusa antes, com erro mais claro.
grant select, insert on public.ledger_transactions to authenticated;
revoke update, delete on public.ledger_transactions from authenticated;

-- Tabela de controle das migrations não é da aplicação.
revoke all on public.schema_migrations from authenticated, anon;

-- Tabelas futuras nascem com o mesmo padrão em vez de depender de memória.
alter default privileges in schema public
    grant select, insert, update, delete on tables to authenticated;
