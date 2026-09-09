-- O aluno limpa a própria marca de senha temporária, com rastro.
--
-- `concluir_troca_de_senha` fazia duas escritas na mesma transação: limpar
-- `profiles.must_change_password` e gravar `user_password_changed` em
-- `audit_logs`. A primeira é permitida ao dono do perfil pela política
-- `perfil_proprio_edicao`; a segunda não é — `audit_logs` tem uma política só,
-- `auditoria_admin`, e um aluno não insere auditoria.
--
-- O efeito era o pior possível: a senha mudava no Supabase Auth, o insert da
-- auditoria era recusado, a transação inteira caía, e o rollback levava junto
-- a limpeza da marca. O aluno trocava a senha e continuava trancado, com a
-- senha nova valendo e a antiga não.
--
-- Três saídas foram consideradas. Dar ao aluno permissão de inserir auditoria
-- afrouxaria a tabela que existe justamente para não depender de confiança.
-- Largar a auditoria perderia o registro de quem trocou a senha. Esta é a
-- terceira: quem escreve é o banco, como dono, e o aluno não ganha permissão
-- nenhuma.
--
-- SECURITY DEFINER no molde de `is_admin()` e `contas_fora_de_sincronia()`,
-- com `search_path` fixo contra sequestro por tabela homônima.
--
-- A função **não recebe parâmetro**, e é isso que a torna segura: ela age
-- sobre `auth.uid()`, o próprio chamador. Não há como pedir a limpeza da marca
-- de outra pessoa, nem por engano nem de propósito.
create or replace function public.concluir_troca_de_senha()
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    ator uuid := auth.uid();
begin
    if ator is null then
        raise exception 'concluir_troca_de_senha exige usuário autenticado';
    end if;

    update public.profiles
       set must_change_password = false
     where id = ator;

    if not found then
        -- Mensagem por concatenação, e não pelo formatador do plpgsql: o
        -- sinal de porcentagem que ele usa como marcador é o mesmo que os
        -- drivers de banco tratam como parâmetro, e a migração precisa
        -- aplicar tanto por psql quanto pelo driver da aplicação.
        raise exception using message =
            'concluir_troca_de_senha: perfil não encontrado para ' || ator;
    end if;

    insert into public.audit_logs (actor_id, action, entity_type, entity_id)
    values (ator, 'user_password_changed', 'user', ator::text);
end;
$$;

revoke execute on function public.concluir_troca_de_senha() from public;
grant execute on function public.concluir_troca_de_senha() to authenticated;
